from pathlib import Path
from unittest.mock import patch
import agent_manager.paths as paths
import agent_manager.application as app


def test_custom_codex_home_does_not_depend_on_working_directory(tmp_path,monkeypatch):
    home=tmp_path/'User name';home.mkdir();a=tmp_path/'a';b=tmp_path/'b';a.mkdir();b.mkdir()
    monkeypatch.chdir(a);first=paths.codex_home({'CODEX_HOME':'custom codex'},home)
    monkeypatch.chdir(b);second=paths.codex_home({'CODEX_HOME':'custom codex'},home)
    assert first==second==(home/'custom codex').resolve()
    assert paths.codex_home({},home)==(home/'.codex').resolve()


def test_frozen_resources_use_bundle_not_executable_or_cwd(tmp_path,monkeypatch):
    bundle=tmp_path/'unpack 中文';bundle.mkdir()
    monkeypatch.setattr(paths.sys,'frozen',True,raising=False)
    monkeypatch.setattr(paths.sys,'_MEIPASS',str(bundle),raising=False)
    assert paths.resource_root()==bundle/'agent_manager/resources'


def test_new_generation_restart_cannot_pick_retired_nine_x_binary(tmp_path):
    current=tmp_path/'AgentManager.exe';next_file=tmp_path/'AgentManager-1.0.1.exe';old=tmp_path/'AgentManager-9.13.0.exe'
    for p in (current,next_file,old):p.write_bytes(b'fixture not executed')
    versions={current:(1,0,0,0),next_file:(1,0,1,0),old:(9,13,0,0)}
    with patch.object(app.sys,'frozen',True,create=True),patch.object(app.sys,'executable',str(current)), \
         patch.object(app,'_executable_version',side_effect=lambda p:versions[Path(p)]), \
         patch.object(app,'executable_release_epoch',side_effect=lambda p:0 if Path(p)==old else 1):
        command,_=app._manager_restart_command('fixture')
    assert Path(command[0])==next_file


def test_source_restart_uses_module_entry(tmp_path):
    with patch.object(app.sys,'frozen',False,create=True):
        command,_=app._manager_restart_command('fixture')
    assert command[1:3]==['-m','agent_manager']


def test_missing_webview_dependencies_are_detected_without_loading_dotnet():
    from agent_manager.platform.webview import available
    class Missing:
        HKEY_CURRENT_USER='user';HKEY_LOCAL_MACHINE='machine'
        def OpenKey(self,*args):raise FileNotFoundError()
    assert available(Missing()) is False


def test_per_user_webview_installation_is_supported():
    from contextlib import nullcontext
    from agent_manager.platform.webview import available
    class Registry:
        HKEY_CURRENT_USER='user';HKEY_LOCAL_MACHINE='machine'
        def OpenKey(self,hive,path):
            if 'NET Framework' in path:return nullcontext('dotnet')
            if hive=='user' and 'F3017226' in path:return nullcontext('webview')
            raise FileNotFoundError()
        def QueryValueEx(self,key,name):return (528040 if key=='dotnet' else '140.0.3485.1',None)
    assert available(Registry()) is True
