"""Copy the license files of the installed Python packages to DEST/<package>/
(from their metadata: the files named LICENSE*, LICENCE*, COPYING*, NOTICE*),
with Python's own license.

    python3 image/python-licenses.py DEST
"""
import importlib.metadata as md
import shutil
import sys
import sysconfig
from pathlib import Path

dest = Path(sys.argv[1])
for dist in md.distributions():
    name = dist.metadata["Name"]
    if name in ("pip", "gitops-check"):       # pip is removed; ours is /licenses/gitops-scanner
        continue
    for f in dist.files or []:
        if Path(f).name.upper().startswith(("LICENSE", "LICENCE", "COPYING", "NOTICE")):
            (dest / name).mkdir(parents=True, exist_ok=True)
            shutil.copyfile(f.locate(), dest / name / Path(f).name)
shutil.copyfile(Path(sysconfig.get_paths()["stdlib"]) / "LICENSE.txt", dest / "Python-LICENSE.txt")
