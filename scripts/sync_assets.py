"""Stage only built public UI files into the distributable Python package."""
from pathlib import Path
import shutil
import stat

root = Path(__file__).resolve().parents[1]
source = root / "frontend/dist"
target = root / "src/agent_manager/resources/ui"
if not (source / "index.html").is_file():
    raise SystemExit("Build frontend first: npm run build --prefix frontend")
for parent in (target, *target.parents):
    if parent.exists() and (parent.is_symlink() or getattr(parent.lstat(), "st_file_attributes", 0) & 0x400):
        raise SystemExit("Refusing to stage resources through a directory link")
if not target.resolve().is_relative_to(root.resolve()):
    raise SystemExit("Resource destination escaped the project")
if target.exists():
    shutil.rmtree(target)
shutil.copytree(source, target)
