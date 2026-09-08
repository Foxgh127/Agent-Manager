import json
import threading
import time
import app_update_service as updates
import test_app_update_service_v912 as fixtures


def make_service(tmp_path):
    return updates.AppUpdateService('9.11.2',tmp_path/'config.json',tmp_path/'downloads',fetcher=fixtures.Fetcher(),install_supported=True)


def test_download_callback_runs_after_verified_file_and_lock_release(tmp_path):
    service=make_service(tmp_path);done=threading.Event();outcome=[]
    def ready():
        outcome.append(service.verified_download_path().read_bytes())
        assert service._operation.acquire(blocking=False)
        service._operation.release();done.set()
    try:
        service.start_download(fixtures.ready(service),on_ready=ready)
        assert done.wait(3)
        assert outcome == [fixtures.PAYLOAD]
    finally: service.close()


def test_failed_verification_never_invokes_install(tmp_path):
    service=make_service(tmp_path);called=[]
    token=fixtures.ready(service);service.fetcher.body=b'bad'
    try:
        service.start_download(token,on_ready=lambda:called.append(True))
        assert fixtures.wait_download(service)['download']['state']=='failed'
        assert not called
    finally: service.close()


def test_restart_recovers_installation_error_backup_and_restart_evidence(tmp_path):
    install_id='b'*32;root=tmp_path/'app-update-install';directory=root/('a'*24);directory.mkdir(parents=True)
    path=directory/'result.json'
    path.write_text(json.dumps({'installId':install_id,'state':'failed','message':'launch failed',
        'backup':'old.exe','restart':{'ready':True}}))
    (root/'latest.json').write_text(json.dumps({'installId':install_id,'path':str(path)}))
    service=make_service(tmp_path)
    try:
        result=service.status()['installation']
        assert result['state']=='failed' and result['backup']=='old.exe' and result['restart']['ready']
    finally: service.close()


def test_install_result_reference_cannot_read_outside_its_directory(tmp_path):
    root=tmp_path/'app-update-install';root.mkdir()
    outside=tmp_path/'result.json';outside.write_text(json.dumps({'installId':'b'*32,'state':'complete'}))
    (root/'latest.json').write_text(json.dumps({'installId':'b'*32,'path':str(outside)}))
    service=make_service(tmp_path)
    try: assert service.status()['installation']['state']=='idle'
    finally: service.close()


def test_monitor_failure_releases_lock_and_is_visible(tmp_path):
    service=make_service(tmp_path);install_id='b'*32;directory=tmp_path/'app-update-install'/('a'*24);directory.mkdir(parents=True)
    path=directory/'result.json';path.write_text(json.dumps({'installId':install_id,'state':'failed','message':'restore blocked'}))
    assert service._operation.acquire(False)
    try:
        service.monitor_installation(path,install_id)
        deadline=time.monotonic()+3
        while service._operation.locked() and time.monotonic()<deadline: time.sleep(.01)
        assert not service._operation.locked()
        assert service.status()['installation']['message']=='restore blocked'
    finally: service.close()
