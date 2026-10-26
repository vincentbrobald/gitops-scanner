// Command argocd-render renders Applications and generates ApplicationSets
// with Argo CD's own code, in process: the repo-server's manifest generation
// and the ApplicationSet generators, as the API server calls them, without
// an Argo CD server or a cluster. gitops-check starts one per run.
//
// Protocol, one JSON object per line: the first line on stdin is the Config;
// each following line is a request, {"render": Application} or
// {"generate": ApplicationSet}, answered on stdout by one line:
// {"manifests": [...]}, {"applications": [...]} or {"error": "..."}.
//
// It is built inside a checkout of Argo CD at the clusters' version (see the
// Containerfile), with that version's kustomize and helm on PATH, so that
// what renders here renders on the clusters.
package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"strings"
	"time"

	kubecache "github.com/argoproj/argo-cd/gitops-engine/pkg/cache"
	"github.com/argoproj/argo-cd/gitops-engine/pkg/diff"
	"github.com/argoproj/argo-cd/gitops-engine/pkg/utils/kube"
	"github.com/argoproj/argo-cd/gitops-engine/pkg/utils/tracing"
	"github.com/go-logr/logr"
	log "github.com/sirupsen/logrus"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	k8sfake "k8s.io/client-go/kubernetes/fake"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"k8s.io/client-go/rest"
	"sigs.k8s.io/controller-runtime/pkg/client"
	crfake "sigs.k8s.io/controller-runtime/pkg/client/fake"

	appsettemplate "github.com/argoproj/argo-cd/v3/applicationset/controllers/template"
	"github.com/argoproj/argo-cd/v3/applicationset/generators"
	appsetutils "github.com/argoproj/argo-cd/v3/applicationset/utils"
	"github.com/argoproj/argo-cd/v3/common"
	"github.com/argoproj/argo-cd/v3/pkg/apis/application/v1alpha1"
	"github.com/argoproj/argo-cd/v3/reposerver/apiclient"
	reposervercache "github.com/argoproj/argo-cd/v3/reposerver/cache"
	"github.com/argoproj/argo-cd/v3/reposerver/metrics"
	"github.com/argoproj/argo-cd/v3/reposerver/repository"
	"github.com/argoproj/argo-cd/v3/util/argo"
	cacheutil "github.com/argoproj/argo-cd/v3/util/cache"
	"github.com/argoproj/argo-cd/v3/util/git"
	"github.com/argoproj/argo-cd/v3/util/settings"
)

// Cluster is a destination, as its cluster Secret would describe it.
type Cluster struct {
	Name        string            `json:"name"`
	Server      string            `json:"server"`
	Labels      map[string]string `json:"labels"`
	Annotations map[string]string `json:"annotations"`
}

// Config is the first line on stdin.
type Config struct {
	Namespace string            `json:"namespace"` // Argo CD's namespace, where Applications live
	Repos     map[string]string `json:"repos"`     // repository URL -> local clone fetched instead
	Clusters  []Cluster         `json:"clusters"`
	ArgoCDCM  map[string]string `json:"argocdCM"` // argocd-cm data, e.g. kustomize.buildOptions
	// What Helm sees as .Capabilities; empty and running in a pod: discovered
	// from that cluster, as the API server does for the destination.
	KubeVersion string   `json:"kubeVersion"`
	APIVersions []string `json:"apiVersions"`
}

// argoSettings are the argocd-cm settings a manifest request carries, read
// once through Argo CD's SettingsManager so that its defaults apply.
type argoSettings struct {
	labelKey, tracking, installationID string
	kustomize                          *v1alpha1.KustomizeOptions
	helm                               *v1alpha1.HelmOptions
	sourceTypes                        map[string]bool
	sensitive                          map[string]bool
}

type renderer struct {
	cfg      Config
	svc      *repository.Service
	settings argoSettings
	client   client.Client // the cluster Secrets and project "default", for the generators
	gens     map[string]generators.Generator
}

func newRenderer(ctx context.Context, cfg Config, root string) (*renderer, error) {
	if cfg.Namespace == "" {
		cfg.Namespace = "argocd"
	}
	if cfg.KubeVersion == "" && len(cfg.APIVersions) == 0 {
		if err := discover(&cfg); err != nil {
			return nil, err
		}
	}
	s, err := readSettings(ctx, cfg)
	if err != nil {
		return nil, err
	}
	// The repo-server's defaults (argocd-repo-server's flags).
	svc := repository.NewService(metrics.NewMetricsServer(),
		reposervercache.NewCache(cacheutil.NewCache(cacheutil.NewInMemoryCache(24*time.Hour)), 24*time.Hour, 3*time.Minute, 10*time.Second),
		repository.RepoServerInitConstants{
			OCIMediaTypes: []string{"application/vnd.oci.image.layer.v1.tar", "application/vnd.oci.image.layer.v1.tar+gzip",
				"application/vnd.cncf.helm.chart.content.v1.tar+gzip"},
			SubmoduleEnabled:                  true,
			MaxCombinedDirectoryManifestsSize: resource.MustParse("10M"),
			StreamedManifestMaxExtractedSize:  1e9,
			StreamedManifestMaxTarSize:        1e8,
			HelmManifestMaxExtractedSize:      1e9,
			HelmRegistryMaxIndexSize:          1e9,
			OCIManifestMaxExtractedSize:       1e9,
			EnableBuiltinGitConfig:            true,
		}, git.NoopCredsStore{}, root)
	if err := svc.Init(); err != nil {
		return nil, fmt.Errorf("repo-server init: %w", err)
	}
	c, err := fakeClient(cfg)
	if err != nil {
		return nil, err
	}
	r := &renderer{cfg: cfg, svc: svc, settings: s, client: c}
	// Only the generators that need nothing but the cluster Secrets and git:
	// SCM provider, pull request, plugin and duck-type ones have no client here
	// and fail when used.
	r.gens = generators.GetGenerators(ctx, c, nil, cfg.Namespace, gitRepos{r}, nil, generators.SCMConfig{}, nil)
	return r, nil
}

// discover sets the Kubernetes version and API versions of the cluster this
// runs in, as the API server's GetManifests gets them; outside a pod, Helm's
// defaults apply.
func discover(cfg *Config) error {
	rc, err := rest.InClusterConfig()
	if errors.Is(err, rest.ErrNotInCluster) {
		log.Warn("not in a cluster: Helm's default Kubernetes version and API versions apply")
		return nil
	} else if err != nil {
		return fmt.Errorf("in-cluster config: %w", err)
	}
	k := &kube.KubectlCmd{Log: logr.Discard(), Tracer: tracing.NopTracer{}}
	if cfg.KubeVersion, err = k.GetServerVersion(rc); err != nil {
		return err
	}
	res, err := k.GetAPIResources(rc, false, kubecache.NewNoopSettings())
	if err != nil {
		return err
	}
	cfg.APIVersions = argo.APIResourcesToStrings(res, true)
	return nil
}

// readSettings reads cfg.ArgoCDCM as Argo CD would read its argocd-cm.
func readSettings(ctx context.Context, cfg Config) (argoSettings, error) {
	cm := &corev1.ConfigMap{ObjectMeta: metav1.ObjectMeta{Name: common.ArgoCDConfigMapName, Namespace: cfg.Namespace,
		Labels: map[string]string{"app.kubernetes.io/part-of": "argocd"}}, Data: cfg.ArgoCDCM}
	mgr := settings.NewSettingsManager(ctx, k8sfake.NewClientset(cm), cfg.Namespace)
	var s argoSettings
	var err error
	if s.labelKey, err = mgr.GetAppInstanceLabelKey(); err != nil {
		return s, err
	}
	if s.tracking, err = mgr.GetTrackingMethod(); err != nil {
		return s, err
	}
	if s.installationID, err = mgr.GetInstallationID(); err != nil {
		return s, err
	}
	if s.kustomize, err = mgr.GetKustomizeSettings(); err != nil {
		return s, err
	}
	if s.helm, err = mgr.GetHelmSettings(); err != nil {
		return s, err
	}
	if s.sourceTypes, err = mgr.GetEnabledSourceTypes(); err != nil {
		return s, err
	}
	s.sensitive = mgr.GetSensitiveAnnotations()
	return s, nil
}

// fakeClient holds what the generators read from the cluster: a cluster
// Secret per cluster (as the argo-cd chart's clusterCredentials makes them)
// and the project "default" (the git generator reads the template's project).
func fakeClient(cfg Config) (client.Client, error) {
	scheme := runtime.NewScheme()
	if err := clientgoscheme.AddToScheme(scheme); err != nil {
		return nil, err
	}
	if err := v1alpha1.AddToScheme(scheme); err != nil {
		return nil, err
	}
	objs := []client.Object{&v1alpha1.AppProject{ObjectMeta: metav1.ObjectMeta{Name: "default", Namespace: cfg.Namespace}}}
	for _, c := range cfg.Clusters {
		labels := map[string]string{common.LabelKeySecretType: common.LabelValueSecretTypeCluster}
		for k, v := range c.Labels {
			labels[k] = v
		}
		objs = append(objs, &corev1.Secret{
			ObjectMeta: metav1.ObjectMeta{Name: "cluster-" + c.Name, Namespace: cfg.Namespace, Labels: labels,
				Annotations: c.Annotations},
			Data: map[string][]byte{"name": []byte(c.Name), "server": []byte(c.Server), "config": []byte("{}")},
		})
	}
	return crfake.NewClientBuilder().WithScheme(scheme).WithObjects(objs...).Build(), nil
}

// getRepository is the repository Argo CD would fetch url from: the local
// clone for the repositories in cfg.Repos, else url itself, without credentials.
func (r *renderer) getRepository(_ context.Context, url, _ string) (*v1alpha1.Repository, error) {
	for u, path := range r.cfg.Repos {
		if git.SameURL(u, url) {
			return &v1alpha1.Repository{Repo: "file://" + path}, nil
		}
	}
	return &v1alpha1.Repository{Repo: url}, nil
}

// checkDestination refuses a destination that names no known cluster, as the
// API server's validation does on create.
func (r *renderer) checkDestination(d v1alpha1.ApplicationDestination) error {
	for _, c := range r.cfg.Clusters {
		if (d.Server != "" && d.Server == c.Server) || (d.Name != "" && d.Name == c.Name) {
			return nil
		}
	}
	return fmt.Errorf("application destination server '%s' and namespace '%s' do not match any of the allowed destinations",
		d.Server, d.Namespace)
}

// render is the API server's GetManifests: one manifest request per source,
// ref sources resolved, Secret values hidden.
func (r *renderer) render(ctx context.Context, app *v1alpha1.Application) ([]json.RawMessage, error) {
	if err := r.checkDestination(app.Spec.Destination); err != nil {
		return nil, err
	}
	sources := v1alpha1.ApplicationSources{app.Spec.GetSource()}
	if app.Spec.HasMultipleSources() {
		sources = app.Spec.GetSources()
	}
	refSources, err := argo.GetRefSources(ctx, sources, app.Spec.Project, r.getRepository, []string{})
	if err != nil {
		return nil, fmt.Errorf("failed to get ref sources: %w", err)
	}
	out := []json.RawMessage{}
	for _, src := range sources {
		repo, _ := r.getRepository(ctx, src.RepoURL, app.Spec.Project)
		res, err := r.svc.GenerateManifest(ctx, &apiclient.ManifestRequest{
			Repo:                            repo,
			Revision:                        src.TargetRevision,
			AppLabelKey:                     r.settings.labelKey,
			AppName:                         app.InstanceName(r.cfg.Namespace),
			Namespace:                       app.Spec.Destination.Namespace,
			ApplicationSource:               &src,
			KustomizeOptions:                r.settings.kustomize,
			KubeVersion:                     r.cfg.KubeVersion,
			ApiVersions:                     r.cfg.APIVersions,
			HelmOptions:                     r.settings.helm,
			TrackingMethod:                  r.settings.tracking,
			EnabledSourceTypes:              r.settings.sourceTypes,
			ProjectName:                     app.Spec.Project,
			ProjectSourceRepos:              []string{"*"},
			HasMultipleSources:              app.Spec.HasMultipleSources(),
			RefSources:                      refSources,
			AnnotationManifestGeneratePaths: app.GetAnnotation(v1alpha1.AnnotationKeyManifestGeneratePaths),
			InstallationID:                  r.settings.installationID,
		})
		if err != nil {
			return nil, fmt.Errorf("error generating manifests: %w", err)
		}
		for _, m := range res.Manifests {
			obj := &unstructured.Unstructured{}
			if err := json.Unmarshal([]byte(m), obj); err != nil {
				return nil, fmt.Errorf("error unmarshaling manifest into unstructured: %w", err)
			}
			if obj.GetKind() == kube.SecretKind && obj.GroupVersionKind().Group == "" {
				if obj, _, err = diff.HideSecretData(obj, nil, r.settings.sensitive); err != nil {
					return nil, fmt.Errorf("error hiding secret data: %w", err)
				}
			}
			data, err := json.Marshal(obj)
			if err != nil {
				return nil, err
			}
			out = append(out, data)
		}
	}
	return out, nil
}

// generate is the API server's ApplicationSet Generate, validation included.
func (r *renderer) generate(appset *v1alpha1.ApplicationSet) ([]v1alpha1.Application, error) {
	if strings.Contains(appset.Spec.Template.Spec.Project, "{{") {
		return nil, errors.New("the Argo CD API does not currently support creating ApplicationSets with templated `project` fields")
	}
	if err := appsetutils.CheckInvalidGenerators(appset); err != nil {
		return nil, err
	}
	logs := bytes.NewBuffer(nil)
	logger := log.New()
	logger.SetOutput(logs)
	apps, _, err := appsettemplate.GenerateApplications(logger.WithField("applicationset", appset.Name), *appset,
		r.gens, &appsetutils.Render{}, r.client)
	if err != nil {
		return nil, fmt.Errorf("unable to generate Applications of ApplicationSet: %w\n%s", err, logs.String())
	}
	return apps, nil
}

// gitRepos is the git generator's access to repositories: the repo-server's
// GetGitFiles and GetGitDirectories, as services.NewArgoCDService calls them.
type gitRepos struct{ r *renderer }

func (g gitRepos) GetFiles(ctx context.Context, repoURL, revision, project, pattern string, noRevisionCache bool,
	si *v1alpha1.SourceIntegrity,
) (map[string][]byte, error) {
	repo, _ := g.r.getRepository(ctx, repoURL, project)
	res, err := g.r.svc.GetGitFiles(ctx, &apiclient.GitFilesRequest{
		Repo: repo, SubmoduleEnabled: true, Revision: revision, Path: pattern, NoRevisionCache: noRevisionCache,
		SourceIntegrity: si, VerifyCommit: si != nil, //nolint:staticcheck // as Argo CD sets it
	})
	if err != nil {
		return nil, fmt.Errorf("error retrieving Git files: %w", err)
	}
	return res.GetMap(), nil
}

func (g gitRepos) GetDirectories(ctx context.Context, repoURL, revision, project string, noRevisionCache bool,
	si *v1alpha1.SourceIntegrity,
) ([]string, error) {
	repo, _ := g.r.getRepository(ctx, repoURL, project)
	res, err := g.r.svc.GetGitDirectories(ctx, &apiclient.GitDirectoriesRequest{
		Repo: repo, SubmoduleEnabled: true, Revision: revision, NoRevisionCache: noRevisionCache,
		SourceIntegrity: si, VerifyCommit: si != nil, //nolint:staticcheck // as Argo CD sets it
	})
	if err != nil {
		return nil, fmt.Errorf("error retrieving Git Directories: %w", err)
	}
	return res.GetPaths(), nil
}

// request is one line on stdin after the Config.
type request struct {
	Render   *v1alpha1.Application    `json:"render"`
	Generate *v1alpha1.ApplicationSet `json:"generate"`
}

// serve answers each request line of in on out until in ends.
func serve(ctx context.Context, r *renderer, in *bufio.Scanner, out *json.Encoder) error {
	for in.Scan() {
		var req request
		var resp map[string]any
		err := json.Unmarshal(in.Bytes(), &req)
		switch {
		case err != nil:
		case req.Render != nil:
			var m []json.RawMessage
			if m, err = r.render(ctx, req.Render); err == nil {
				resp = map[string]any{"manifests": m}
			}
		case req.Generate != nil:
			var apps []v1alpha1.Application
			if apps, err = r.generate(req.Generate); err == nil {
				resp = map[string]any{"applications": apps}
			}
		default:
			err = errors.New(`neither "render" nor "generate"`)
		}
		if err != nil {
			resp = map[string]any{"error": err.Error()}
		}
		if err := out.Encode(resp); err != nil {
			return err
		}
	}
	return in.Err()
}

func run() error {
	// Argo CD's loggers take their level from ARGOCD_LOG_LEVEL; at info, its
	// exec tracing logs every git command.
	if os.Getenv(common.EnvLogLevel) == "" {
		os.Setenv(common.EnvLogLevel, "warn")
	}
	if level, err := log.ParseLevel(os.Getenv(common.EnvLogLevel)); err == nil {
		log.SetLevel(level)
	}
	ctx := context.Background()
	in := bufio.NewScanner(os.Stdin)
	in.Buffer(make([]byte, 1<<20), 64<<20)
	if !in.Scan() {
		return fmt.Errorf("no configuration line: %w", in.Err())
	}
	var cfg Config
	if err := json.Unmarshal(in.Bytes(), &cfg); err != nil {
		return fmt.Errorf("configuration: %w", err)
	}
	root, err := os.MkdirTemp("", "argocd-render-")
	if err != nil {
		return err
	}
	defer os.RemoveAll(root)
	r, err := newRenderer(ctx, cfg, root)
	if err != nil {
		return err
	}
	return serve(ctx, r, in, json.NewEncoder(os.Stdout))
}

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, "argocd-render:", err)
		os.Exit(1)
	}
}
