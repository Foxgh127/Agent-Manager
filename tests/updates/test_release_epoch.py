import hashlib
import json
from unittest.mock import patch
import agent_manager.updates.service as updates
from tests.updates.test_app_update_service import Stream


class Fetcher:
    epoch=1
    version='1.0.1'
    def open(self,url,hosts,**kwargs):
        base=f'https://github.com/Owner/App/releases/download/v{self.version}/'
        name=f'AgentManager-{self.version}.exe';digest=hashlib.sha256(b'MZ fixture').hexdigest()
        if url.endswith('app-update-manifest.json'):
            payload={'appId':updates.APP_ID,'version':self.version,'releaseEpoch':self.epoch,
                     'assets':[{'name':name,'platform':'windows-x64','size':10,'sha256':digest}]}
        else:
            payload={'tag_name':'v'+self.version,'assets':[
                {'name':name,'size':10,'digest':'sha256:'+digest,'browser_download_url':base+name},
                {'name':'app-update-manifest.json','browser_download_url':base+'app-update-manifest.json'}]}
        return Stream(json.dumps(payload).encode())


def test_new_series_accepts_matching_generation_and_case_insensitive_repo(tmp_path):
    fetch=Fetcher();service=updates.AppUpdateService('1.0.0',tmp_path/'config.json',tmp_path/'downloads',fetcher=fetch,release_epoch=1)
    service.configure({'kind':'github','repository':'owner/app','assetName':'AgentManager-{version}.exe'})
    try:
        state=service.check();assert state['state']=='update_available',state.get('error')
        assert state['latestRelease']['version']=='1.0.1'
    finally:service.close()


def test_retired_nine_x_release_is_not_a_new_update(tmp_path):
    fetch=Fetcher();fetch.epoch=0;fetch.version='9.13.0'
    service=updates.AppUpdateService('1.0.0',tmp_path/'config.json',tmp_path/'downloads',fetcher=fetch,release_epoch=1)
    service.configure({'kind':'github','repository':'owner/app'})
    try:
        state=service.check();assert state['errorCode']=='release_epoch_mismatch'
        assert not state['canDownload']
    finally:service.close()


def test_previous_generation_cache_is_not_presented_as_new_release(tmp_path):
    source={'kind':'github','repository':'owner/app','assetName':'','channel':'stable','schemaVersion':1}
    service=updates.AppUpdateService('1.0.0',tmp_path/'config.json',tmp_path/'downloads',release_epoch=1)
    service.configure(source)
    service._source_id=updates._fingerprint(updates.validate_source(source))
    service._latest={'version':'9.13.0'}
    try:assert service.status()['latestRelease'] is None
    finally:service.close()


def test_prerelease_list_selects_current_generation_before_numeric_version(tmp_path):
    service=updates.AppUpdateService('1.0.0',tmp_path/'config.json',tmp_path/'downloads',release_epoch=1)
    service.configure({'kind':'github','repository':'owner/app','channel':'prerelease'})
    digest=hashlib.sha256(b'MZ fixture').hexdigest()
    def entry(version):
        base=f'https://github.com/owner/app/releases/download/v{version}/'
        name=f'AgentManager-{version}.exe'
        return {'tag_name':'v'+version,'assets':[
            {'name':name,'size':10,'digest':'sha256:'+digest,'browser_download_url':base+name},
            {'name':'app-update-manifest.json','browser_download_url':base+'app-update-manifest.json'}]}
    def remote(url,hosts):
        if '/releases?' in url: return [entry('9.13.0'),entry('1.0.2'),entry('1.0.1')]
        version=url.split('/download/v')[1].split('/')[0]
        return {'appId':updates.APP_ID,'version':version,'releaseEpoch':0 if version.startswith('9.') else 1,
                'assets':[{'name':f'AgentManager-{version}.exe','platform':'windows-x64','size':10,'sha256':digest}]}
    try:
        with patch.object(service,'_json_remote',side_effect=remote): status=service.check()
        assert status['state']=='update_available',status.get('error')
        assert status['latestRelease']['version']=='1.0.2'
    finally: service.close()
