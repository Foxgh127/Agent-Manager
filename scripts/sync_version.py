"""Derive all distribution versions from the package's single version source."""
from pathlib import Path
import ast
import json
import re

ROOT = Path(__file__).resolve().parents[1]
module = ast.parse((ROOT / "src/agent_manager/_version.py").read_text(encoding="utf-8"))
values = {node.targets[0].id:ast.literal_eval(node.value) for node in module.body if isinstance(node,ast.Assign)}
version=values["VERSION"]; epoch=values["RELEASE_EPOCH"]
if not re.fullmatch(r"\d+\.\d+\.\d+",version):raise SystemExit("Expected a stable x.y.z product version")
for name in ("package.json","package-lock.json"):
    path=ROOT/"frontend"/name;data=json.loads(path.read_text(encoding="utf-8"));data["version"]=version
    if "packages" in data:data["packages"][""]["version"]=version
    path.write_text(json.dumps(data,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
(ROOT/"frontend/src/version.js").write_text(f'export const APP_VERSION = "{version}";\n',encoding="utf-8")
resource=ROOT/"src/agent_manager/resources/version.json"
resource.write_text(json.dumps({"version":version,"releaseEpoch":epoch},indent=2)+"\n",encoding="utf-8")
parts=tuple(int(p) for p in version.split('.'))+(0,)
(ROOT/"packaging/version_info.txt").write_text(f'''VSVersionInfo(
  ffi=FixedFileInfo(filevers={parts}, prodvers={parts}, mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[StringFileInfo([StringTable('040904B0', [
    StringStruct('CompanyName', 'Personal learning project'),
    StringStruct('FileDescription', 'Agent Manager'),
    StringStruct('FileVersion', '{version}'),
    StringStruct('ProductVersion', '{version}'),
    StringStruct('ProductName', 'Agent Manager'),
    StringStruct('OriginalFilename', 'AgentManager.exe'),
    StringStruct('ReleaseEpoch', '{epoch}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])])
''',encoding="utf-8")
print(version)
