import ctypes
from ctypes import wintypes
import errno
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import agent_manager_core as core


class _MockWindowsLockError(PermissionError):
    def __init__(self, winerror: int = 5):
        super().__init__(errno.EACCES, "synthetic Windows destination lock")
        self.winerror = winerror


class WindowsOverlayAtomicWriteTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "requires a real Windows non-delete-sharing handle")
    def test_real_windows_destination_handle_is_retried_until_release(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "runtime-configuration-overlay.json"
            target.write_text('{"generation":"old"}\n', encoding="utf-8")
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateFileW.argtypes = [
                wintypes.LPCWSTR,
                wintypes.DWORD,
                wintypes.DWORD,
                ctypes.c_void_p,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.HANDLE,
            ]
            kernel32.CreateFileW.restype = wintypes.HANDLE
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            handle = kernel32.CreateFileW(
                str(target),
                0x80000000,  # GENERIC_READ
                0x00000001 | 0x00000002,  # share read/write, deliberately deny delete
                None,
                3,  # OPEN_EXISTING
                0x00000080,  # FILE_ATTRIBUTE_NORMAL
                None,
            )
            if handle == ctypes.c_void_p(-1).value:
                raise ctypes.WinError(ctypes.get_last_error())
            real_replace = os.replace
            observed_errors = []

            def tracked_replace(source, destination):
                try:
                    return real_replace(source, destination)
                except OSError as exc:
                    observed_errors.append(getattr(exc, "winerror", None))
                    raise

            def release_destination():
                time.sleep(0.09)
                kernel32.CloseHandle(handle)

            releaser = threading.Thread(target=release_destination)
            releaser.start()
            try:
                with patch.object(core.os, "replace", side_effect=tracked_replace):
                    core.atomic_write_json(target, {"generation": "new"})
            finally:
                releaser.join(timeout=5)

            self.assertFalse(releaser.is_alive())
            self.assertTrue(set(observed_errors).intersection({5, 32, 33}))
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"generation": "new"})
            self.assertEqual(list(target.parent.glob(f".{target.name}.*.tmp")), [])

    @unittest.skipUnless(os.name == "nt", "Windows replace retry policy")
    def test_transient_windows_access_denied_is_retried_and_committed(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "runtime-configuration-overlay.json"
            target.write_text('{"generation":"old"}\n', encoding="utf-8")
            real_replace = os.replace
            calls = []

            def flaky_replace(source, destination):
                calls.append((Path(source), Path(destination)))
                if len(calls) < 3:
                    raise _MockWindowsLockError(5)
                return real_replace(source, destination)

            with (
                patch.object(core.os, "replace", side_effect=flaky_replace),
                patch.object(core.time, "sleep") as sleep,
            ):
                core.atomic_write_json(target, {"generation": "new"})

            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"generation": "new"})
            self.assertEqual(len(calls), 3)
            self.assertEqual(
                [call.args[0] for call in sleep.call_args_list],
                [core._atomic_retry_delay(0), core._atomic_retry_delay(1)],
            )
            self.assertEqual(list(target.parent.glob(f".{target.name}.*.tmp")), [])

    @unittest.skipUnless(os.name == "nt", "Windows replace retry policy")
    def test_runtime_overlay_applied_hash_survives_a_transient_journal_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory)
            target = codex_home / "config.toml"
            overlay = codex_home / "agent-manager" / "runtime-configuration-overlay.json"
            target.write_text('model = "new"\n', encoding="utf-8")
            payload = {
                "schemaVersion": 1,
                "sessionId": "transient-lock",
                "ownerPid": os.getpid(),
                "codexHome": str(codex_home),
                "files": {"config.toml": {"appliedHash": None}},
                "environment": {},
            }
            core.atomic_write_json(overlay, payload)
            real_replace = os.replace
            overlay_attempts = 0

            def flaky_overlay_replace(source, destination):
                nonlocal overlay_attempts
                if Path(destination) == overlay:
                    overlay_attempts += 1
                    if overlay_attempts < 3:
                        raise _MockWindowsLockError(5)
                return real_replace(source, destination)

            with (
                patch.object(core, "CODEX_HOME", codex_home),
                patch.object(core, "RUNTIME_OVERLAY_FILE", overlay),
                patch.object(core.os, "replace", side_effect=flaky_overlay_replace),
                patch.object(core.time, "sleep"),
            ):
                core._runtime_overlay_record_applied(paths=[target])

            stored = json.loads(overlay.read_text(encoding="utf-8"))
            self.assertEqual(stored["files"]["config.toml"]["appliedHash"], core._overlay_value_hash(target.read_bytes()))
            self.assertEqual(overlay_attempts, 3)
            self.assertEqual(list(overlay.parent.glob(f".{overlay.name}.*.tmp")), [])

    @unittest.skipUnless(os.name == "nt", "Windows replace retry policy")
    def test_persistent_windows_lock_preserves_original_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "runtime-configuration-overlay.json"
            original = b'{"generation":"old"}\n'
            target.write_bytes(original)

            with (
                patch.object(core.os, "replace", side_effect=_MockWindowsLockError(32)) as replace,
                patch.object(core.time, "sleep") as sleep,
            ):
                with self.assertRaises(_MockWindowsLockError):
                    core.atomic_write_bytes(target, b'{"generation":"new"}\n')

            self.assertEqual(target.read_bytes(), original)
            self.assertEqual(replace.call_count, core._ATOMIC_REPLACE_ATTEMPTS)
            self.assertEqual(sleep.call_count, core._ATOMIC_REPLACE_ATTEMPTS - 1)
            self.assertEqual(list(target.parent.glob(f".{target.name}.*.tmp")), [])
            self.assertEqual(core._ATOMIC_WRITE_LOCKS, {})

    def test_non_lock_replace_error_is_not_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "runtime-configuration-overlay.json"
            target.write_bytes(b"old")
            failure = OSError(errno.ENOSPC, "synthetic disk full")

            with (
                patch.object(core.os, "replace", side_effect=failure) as replace,
                patch.object(core.time, "sleep") as sleep,
            ):
                with self.assertRaises(OSError) as raised:
                    core.atomic_write_bytes(target, b"new")

            self.assertIs(raised.exception, failure)
            replace.assert_called_once()
            sleep.assert_not_called()
            self.assertEqual(target.read_bytes(), b"old")
            self.assertEqual(list(target.parent.glob(f".{target.name}.*.tmp")), [])

    def test_same_path_threads_serialize_without_partial_json_or_temp_leaks(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "runtime-configuration-overlay.json"
            real_replace = os.replace
            replace_state_lock = threading.Lock()
            start = threading.Barrier(12)
            active = 0
            maximum_active = 0

            def tracked_replace(source, destination):
                nonlocal active, maximum_active
                with replace_state_lock:
                    active += 1
                    maximum_active = max(maximum_active, active)
                try:
                    time.sleep(0.003)
                    return real_replace(source, destination)
                finally:
                    with replace_state_lock:
                        active -= 1

            def writer(generation):
                start.wait()
                core.atomic_write_json(target, {"generation": generation, "body": "x" * 4096})

            with patch.object(core.os, "replace", side_effect=tracked_replace):
                with ThreadPoolExecutor(max_workers=12) as pool:
                    futures = [pool.submit(writer, generation) for generation in range(12)]
                    for future in futures:
                        future.result(timeout=10)

            payload = json.loads(target.read_text(encoding="utf-8"))
            self.assertIn(payload["generation"], range(12))
            self.assertEqual(payload["body"], "x" * 4096)
            self.assertEqual(maximum_active, 1)
            self.assertEqual(list(target.parent.glob(f".{target.name}.*.tmp")), [])
            self.assertEqual(core._ATOMIC_WRITE_LOCKS, {})

    def test_runtime_overlay_read_modify_write_is_thread_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory)
            overlay = codex_home / "agent-manager" / "runtime-configuration-overlay.json"
            targets = [codex_home / f"agent-{index}.toml" for index in range(8)]
            for index, target in enumerate(targets):
                target.write_text(f"generation = {index}\n", encoding="utf-8")
            payload = {
                "schemaVersion": 1,
                "sessionId": "threaded-record",
                "ownerPid": os.getpid(),
                "codexHome": str(codex_home),
                "files": {
                    target.name: {"appliedHash": None}
                    for target in targets
                },
                "environment": {},
            }
            core.atomic_write_json(overlay, payload)
            start = threading.Barrier(len(targets))

            def record(target):
                start.wait()
                core._runtime_overlay_record_applied(paths=[target])

            with (
                patch.object(core, "CODEX_HOME", codex_home),
                patch.object(core, "RUNTIME_OVERLAY_FILE", overlay),
            ):
                with ThreadPoolExecutor(max_workers=len(targets)) as pool:
                    futures = [pool.submit(record, target) for target in targets]
                    for future in futures:
                        future.result(timeout=10)

            stored = json.loads(overlay.read_text(encoding="utf-8"))
            for target in targets:
                self.assertEqual(
                    stored["files"][target.name]["appliedHash"],
                    core._overlay_value_hash(target.read_bytes()),
                )
            self.assertEqual(list(overlay.parent.glob(f".{overlay.name}.*.tmp")), [])

    def test_different_paths_do_not_share_a_global_write_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            targets = [Path(directory) / "one.json", Path(directory) / "two.json"]
            real_replace = os.replace
            replace_state_lock = threading.Lock()
            start = threading.Barrier(2)
            active = 0
            maximum_active = 0

            def tracked_replace(source, destination):
                nonlocal active, maximum_active
                with replace_state_lock:
                    active += 1
                    maximum_active = max(maximum_active, active)
                try:
                    time.sleep(0.05)
                    return real_replace(source, destination)
                finally:
                    with replace_state_lock:
                        active -= 1

            def writer(target):
                start.wait()
                core.atomic_write_json(target, {"path": target.name})

            with patch.object(core.os, "replace", side_effect=tracked_replace):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    futures = [pool.submit(writer, target) for target in targets]
                    for future in futures:
                        future.result(timeout=10)

            self.assertEqual(maximum_active, 2)
            for target in targets:
                self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"path": target.name})

    def test_processes_can_atomically_replace_the_overlay_concurrently(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "runtime-configuration-overlay.json"
            script = (
                "import sys\n"
                "from pathlib import Path\n"
                "import agent_manager_core as core\n"
                "target = Path(sys.argv[1])\n"
                "worker = int(sys.argv[2])\n"
                "for generation in range(20):\n"
                "    core.atomic_write_json(target, "
                "{'worker': worker, 'generation': generation, 'body': 'x' * 4096})\n"
            )
            processes = [
                subprocess.Popen(
                    [sys.executable, "-c", script, str(target), str(worker)],
                    cwd=Path(__file__).parent,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for worker in range(4)
            ]
            for process in processes:
                stdout, stderr = process.communicate(timeout=30)
                self.assertEqual(process.returncode, 0, msg=f"stdout={stdout}\nstderr={stderr}")

            payload = json.loads(target.read_text(encoding="utf-8"))
            self.assertIn(payload["worker"], range(4))
            self.assertEqual(payload["generation"], 19)
            self.assertEqual(payload["body"], "x" * 4096)
            self.assertEqual(list(target.parent.glob(f".{target.name}.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
