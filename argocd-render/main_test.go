package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"testing"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	"github.com/argoproj/argo-cd/v3/pkg/apis/application/v1alpha1"
)

const repoURL = "ssh://git@forge.example/org/gitops.git"

// testRenderer returns a renderer over a new git repository holding files,
// and the repository's commit. The Argo CD checkout's hack/ provides the
// git-verify-wrapper.sh the repo-server runs (the image's /usr/local/bin).
func testRenderer(t *testing.T, files map[string]string) (*renderer, string) {
	t.Helper()
	hack, err := filepath.Abs("../../hack")
	if err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", hack+string(os.PathListSeparator)+os.Getenv("PATH"))
	dir := t.TempDir()
	for name, text := range files {
		p := filepath.Join(dir, name)
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte(text), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	for _, args := range [][]string{{"init", "-q", "-b", "main"}, {"add", "."},
		{"-c", "user.name=t", "-c", "user.email=t@example.org", "commit", "-q", "-m", "x"}} {
		if out, err := exec.Command("git", append([]string{"-C", dir}, args...)...).CombinedOutput(); err != nil {
			t.Fatalf("git %v: %v\n%s", args, err, out)
		}
	}
	rev, err := exec.Command("git", "-C", dir, "rev-parse", "HEAD").Output()
	if err != nil {
		t.Fatal(err)
	}
	r, err := newRenderer(context.Background(), Config{
		Namespace: "argocd",
		Repos:     map[string]string{repoURL: dir},
		Clusters: []Cluster{{Name: "lab", Server: "https://kubernetes.default.svc",
			Labels: map[string]string{"env": "lab"}}},
		KubeVersion: "v1.33.0", // set: no discovery
	}, t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	return r, strings.TrimSpace(string(rev))
}

func app(name, rev, path string) *v1alpha1.Application {
	a := &v1alpha1.Application{}
	a.Name, a.Namespace = name, "argocd"
	a.Spec.Project = "default"
	a.Spec.Source = &v1alpha1.ApplicationSource{RepoURL: repoURL, TargetRevision: rev, Path: path}
	a.Spec.Destination = v1alpha1.ApplicationDestination{Server: "https://kubernetes.default.svc", Namespace: "web"}
	return a
}

func TestRender(t *testing.T) {
	r, rev := testRenderer(t, map[string]string{
		"web/cm.yaml":     "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: c}\ndata: {k: v}\n",
		"web/secret.yaml": "apiVersion: v1\nkind: Secret\nmetadata: {name: s}\nstringData: {password: hunter2}\n",
	})
	out, err := r.render(context.Background(), app("web", rev, "web"))
	if err != nil {
		t.Fatal(err)
	}
	kinds := map[string]map[string]any{}
	for _, m := range out {
		var obj map[string]any
		if err := json.Unmarshal(m, &obj); err != nil {
			t.Fatal(err)
		}
		kinds[obj["kind"].(string)] = obj
	}
	md := kinds["ConfigMap"]["metadata"].(map[string]any)
	if got := md["annotations"].(map[string]any)["argocd.argoproj.io/tracking-id"]; got != "web:/ConfigMap:web/c" {
		t.Errorf("tracking id: %v", got)
	}
	if bytes.Contains(out[0], []byte("hunter2")) || bytes.Contains(out[1], []byte("hunter2")) {
		t.Error("a Secret value is in clear")
	}

	if _, err := r.render(context.Background(), app("web", rev, "missing")); err == nil {
		t.Error("a missing path renders")
	}
	a := app("web", rev, "web")
	a.Spec.Destination.Server = "https://elsewhere"
	if _, err := r.render(context.Background(), a); err == nil || !strings.Contains(err.Error(), "allowed destinations") {
		t.Errorf("unknown destination: %v", err)
	}
}

func TestGenerate(t *testing.T) {
	r, rev := testRenderer(t, map[string]string{"envs/a/x.yaml": "{}", "envs/b/x.yaml": "{}"})
	set := &v1alpha1.ApplicationSet{}
	set.Name, set.Namespace = "set", "argocd"
	set.Spec.GoTemplate = true
	set.Spec.Generators = []v1alpha1.ApplicationSetGenerator{{Matrix: &v1alpha1.MatrixGenerator{
		Generators: []v1alpha1.ApplicationSetNestedGenerator{
			{Clusters: &v1alpha1.ClusterGenerator{Selector: metav1.LabelSelector{MatchLabels: map[string]string{"env": "lab"}}}},
			{Git: &v1alpha1.GitGenerator{RepoURL: repoURL, Revision: rev,
				Directories: []v1alpha1.GitDirectoryGeneratorItem{{Path: "envs/*"}}}},
		}}}}
	set.Spec.Template.Name = "{{ .name }}-{{ .path.basename }}"
	set.Spec.Template.Spec = app("x", rev, "{{ .path.path }}").Spec
	apps, err := r.generate(set)
	if err != nil {
		t.Fatal(err)
	}
	var names []string
	for _, a := range apps {
		names = append(names, a.Name)
	}
	sort.Strings(names)
	if strings.Join(names, ",") != "lab-a,lab-b" {
		t.Errorf("generated %v", names)
	}

	set.Spec.Template.Spec.Project = "{{ .name }}"
	if _, err := r.generate(set); err == nil || !strings.Contains(err.Error(), "templated `project`") {
		t.Errorf("templated project: %v", err)
	}
}

func TestServe(t *testing.T) {
	r, _ := testRenderer(t, map[string]string{"x.yaml": "{}"})
	var out bytes.Buffer
	in := bufio.NewScanner(strings.NewReader("not json\n{}\n"))
	if err := serve(context.Background(), r, in, json.NewEncoder(&out)); err != nil {
		t.Fatal(err)
	}
	lines := strings.Split(strings.TrimSpace(out.String()), "\n")
	if len(lines) != 2 || !strings.HasPrefix(lines[0], `{"error":`) || !strings.Contains(lines[1], "neither") {
		t.Errorf("answers: %q", lines)
	}
}
