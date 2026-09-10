"""Verify distribution metadata and non-mutating startup from another location."""
from pathlib import Path
import argparse
import ast
import hashlib
import json
import shutil
import subprocess
import tempfile
from PyInstaller.archive.readers import CArchiveReader

parser=argparse.ArgumentParser()
parser.add_argument('executable',type=Path)
args=parser.parse_args()
def require(condition,message):
    if not condition:raise RuntimeError(message)
root=Path(__file__).resolve().parents[1]
version_source=ast.parse((root/'src/agent_manager/_version.py').read_text(encoding='utf-8'))
version={n.targets[0].id:ast.literal_eval(n.value) for n in version_source.body if isinstance(n,ast.Assign)}
exe=args.executable.resolve(); archive=CArchiveReader(str(exe))
keys={name.replace('\\','/'):name for name in archive.toc}
for name in ('version.json','app-update-source.json','ui/index.html','ui/app-icon.png'):
    require('agent_manager/resources/'+name in keys,'Missing packaged resource: '+name)
require(archive.extract(keys['agent_manager/resources/ui/app-icon.png']) == (root/'frontend/public/app-icon.png').read_bytes(),
        'Packaged tray artwork differs from current app artwork')
icon_names=[name for name in keys if name.startswith('agent_manager/resources/ui/assets/app-icon-') and name.endswith('.png')]
require(len(icon_names)==1 and archive.extract(keys[icon_names[0]]) == (root/'packaging/app-icon.png').read_bytes(),
        'Packaged UI icon differs from the current artwork')
packaged=json.loads(archive.extract(keys['agent_manager/resources/version.json']))
require(packaged=={'version':version['VERSION'],'releaseEpoch':version['RELEASE_EPOCH']},'Packaged version differs from source')
for name in ('agent_manager.core','agent_manager.application','agent_manager.config.backups','agent_manager.accounts.relay',
             'agent_manager.accounts.import_formats','agent_manager.gateway.service','agent_manager.gateway.scheduling',
             'agent_manager.usage.pricing','agent_manager.usage.capacity','agent_manager.usage.request_metadata',
             'agent_manager.integrations.radar_monitor','agent_manager.integrations.reset_history',
             'agent_manager.updates.installer','agent_manager.updates.cleanup','agent_manager.updates.location',
             'agent_manager.application.location','agent_manager.platform.notifications'):
    require(name in archive.open_embedded_archive('PYZ.pyz').toc,'Missing packaged module: '+name)
with tempfile.TemporaryDirectory(prefix='Agent Manager 中文 path ') as temporary:
    folder=Path(temporary).resolve(); relocated=folder/'Agent Manager.exe';shutil.copy2(exe,relocated)
    checked=subprocess.run([str(relocated),'--help'],cwd=str(folder.parent),timeout=45,capture_output=True,
                           creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    require(checked.returncode==0,'Packaged --help failed; no GUI/config activation was requested.')
print(json.dumps({'version':version['VERSION'],'releaseEpoch':version['RELEASE_EPOCH'],
                  'bytes':exe.stat().st_size,'sha256':hashlib.sha256(exe.read_bytes()).hexdigest(),
                  'relocatedStartup':'passed'},ensure_ascii=False))
