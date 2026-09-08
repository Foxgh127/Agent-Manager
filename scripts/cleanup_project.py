"""Reviewable, file-by-file cleanup of explicitly named generated directories."""
from pathlib import Path
import argparse
import json
import os
import stat

ROOT=Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser()
parser.add_argument('--apply',action='store_true')
parser.add_argument('--dependencies',action='store_true')
parser.add_argument('--legacy-release',action='store_true')
parser.add_argument('--generated-only',action='store_true',help='Only reproducible build/UI outputs; keep legacy archives and installed executables.')
parser.add_argument('--archives-only',action='store_true',help='Only retired audit/work directories; never remove installed executables or dependencies.')
args=parser.parse_args()
if args.generated_only and args.archives_only:parser.error('choose one cleanup scope')
targets=['audit','work','artifacts/build','artifacts/publish','__pycache__','.pytest_cache','.ruff_cache','.coverage','frontend/.preview','frontend/work']
if args.generated_only:targets=['artifacts/build','artifacts/publish','.pytest_cache','.ruff_cache','.coverage','frontend/.preview','frontend/work']
if args.archives_only:targets=['audit','work']
if args.dependencies and not args.archives_only:targets+=['frontend/node_modules','frontend/dist','src/agent_manager/resources/ui']
if args.legacy_release and not (args.generated_only or args.archives_only):targets+=['release']

def direct(path):
    if not path.absolute().is_relative_to(ROOT):return False
    for item in (path,*path.parents):
        if item==ROOT:break
        try:info=item.lstat()
        except FileNotFoundError:continue
        if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400:return False
    return True

files=[];folders=[];retained=[]
for name in targets:
    top=ROOT/name
    if not top.exists():continue
    if not direct(top):retained.append(name);continue
    if top.is_file():files.append(top);continue
    for current,dirs,names in os.walk(top,followlinks=False):
        base=Path(current);folders.append(base)
        dirs[:]=[d for d in dirs if direct(base/d)]
        files.extend(base/n for n in names if direct(base/n) and (base/n).is_file())
for tree in ('src','tests'):
    for current,dirs,names in os.walk(ROOT/tree,followlinks=False):
        base=Path(current);dirs[:]=[d for d in dirs if direct(base/d)]
        if base.name=='__pycache__':folders.append(base);files.extend(base/n for n in names if direct(base/n))
files=list(dict.fromkeys(files));folders=list(dict.fromkeys(folders))
snapshot=[(p,p.stat().st_ino,p.stat().st_size,p.stat().st_mtime_ns) for p in files]
summary={'targets':targets,'files':len(files),'bytes':sum(x[2] for x in snapshot),'applied':args.apply,'retained':retained}
if args.apply:
    removed=0
    for path,inode,size,modified in snapshot:
        try:
            now=path.lstat()
            if not direct(path) or (now.st_ino,now.st_size,now.st_mtime_ns)!=(inode,size,modified):
                retained.append(str(path.relative_to(ROOT)));continue
            if os.name == 'nt' and now.st_file_attributes & stat.FILE_ATTRIBUTE_READONLY:
                # Git pack files in retired audit checkouts are read-only on Windows.
                # Only clear that flag after checking the original file identity.
                os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
            path.unlink();removed+=1
        except OSError:retained.append(str(path.relative_to(ROOT)))
    for path in sorted(folders,key=lambda p:len(p.parts),reverse=True):
        try:
            if direct(path):path.rmdir() # Empty directories only; never recurse here.
        except OSError:pass
    for relative in ('design-system/agent-manager/pages', 'design-system/agent-manager', 'design-system'):
        path=ROOT/relative
        try:
            if direct(path):path.rmdir()  # Only remove the retired directory when empty.
        except OSError:pass
    summary['removedFiles']=removed
print(json.dumps(summary,ensure_ascii=False,indent=2))
