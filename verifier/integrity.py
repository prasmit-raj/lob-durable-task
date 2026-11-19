"""Run with `python -I -S` so nothing in site-packages executes before the check.

Fails if pytest/pluggy files differ from the pinned release, or if a .pth file
would run code at interpreter startup.
"""
import hashlib
import pathlib
import sys
import sysconfig

site = pathlib.Path(sys.argv[2] if len(sys.argv) > 2 else sysconfig.get_paths()["purelib"])
manifest = pathlib.Path(sys.argv[1])
ok = True

for line in manifest.read_text().splitlines():
    digest, rel = line.split(maxsplit=1)
    path = site / rel
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        print(f"modified: {rel}")
        ok = False

listed = {line.split(maxsplit=1)[1] for line in manifest.read_text().splitlines()}
for pkg in ("_pytest", "pytest", "pluggy"):
    for path in (site / pkg).rglob("*.py"):
        if str(path.relative_to(site)) not in listed:
            print(f"unexpected file: {path.relative_to(site)}")
            ok = False

ALLOWED_PTH = {"distutils-precedence.pth"}  # shipped by setuptools in the base image
for pth in site.glob("*.pth"):
    if pth.name not in ALLOWED_PTH and any(
            l.startswith(("import ", "import\t")) for l in pth.read_text().splitlines()):
        print(f"executable .pth: {pth.name}")
        ok = False

sys.exit(0 if ok else 1)
