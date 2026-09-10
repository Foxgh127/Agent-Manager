from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import agent_manager.core as core
import agent_manager.core.catalog as catalog


class StartupCatalogCacheTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.exe = self.root / "codex.exe"
        self.exe.write_bytes(b"test executable")
        for name, value in {
            "STATE_DIR": self.root,
            "MODELS_CACHE_FILE": self.root / "native.json",
            "MODEL_CACHE": {"at": 0, "raw": None},
            "CODEX_VERSION_CACHE": {"at": 0, "value": None},
        }.items():
            p = patch.object(core, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(core, "codex_prefix", return_value=[str(self.exe)])
        p.start()
        self.addCleanup(p.stop)
        catalog._MODEL_PROBE_FAILURE.clear()
        self.payload = {"models": [{"slug": "local"}]}
        self.result = subprocess.CompletedProcess([], 0, json.dumps(self.payload), "")

    def cold_memory(self):
        core.MODEL_CACHE.update({"at": 0, "raw": None})
        core.CODEX_VERSION_CACHE.update({"at": 0, "value": None})

    def test_process_restart_reuses_compatible_persistent_cache_without_cli(self):
        with patch.object(core, "run_codex_capture", return_value=self.result) as run:
            self.assertEqual(core._raw_local_model_catalog(), self.payload)
            run.assert_called_once_with(["debug", "models", "--bundled"], timeout=30)
        self.cold_memory()
        started = time.monotonic()
        with patch.object(core, "run_codex_capture", side_effect=AssertionError("CLI should not run")):
            self.assertEqual(core._raw_local_model_catalog(), self.payload)
        self.assertLess(time.monotonic() - started, 0.5)

    def test_force_identity_change_and_expiry_each_require_a_real_probe(self):
        with patch.object(core, "run_codex_capture", return_value=self.result) as run:
            core._raw_local_model_catalog()
            core._raw_local_model_catalog(force=True)
            self.assertEqual(run.call_count, 2)
            self.exe.write_bytes(b"different installation")
            self.cold_memory()
            core._raw_local_model_catalog()
            self.assertEqual(run.call_count, 3)
            cache = self.root / "codex-models-probe-cache.json"
            payload = json.loads(cache.read_text(encoding="utf-8"))
            payload["at"] -= catalog._PERSISTED_PROBE_TTL + 1
            cache.write_text(json.dumps(payload), encoding="utf-8")
            self.cold_memory()
            core._raw_local_model_catalog()
            self.assertEqual(run.call_count, 4)

    def test_concurrent_cold_reads_share_one_probe(self):
        entered = threading.Event()
        release = threading.Event()
        def probe(*args, **kwargs):
            entered.set()
            if not release.wait(2):
                raise AssertionError("probe not released")
            return self.result
        with patch.object(core, "run_codex_capture", side_effect=probe) as run:
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = [pool.submit(core._raw_local_model_catalog) for _ in range(4)]
                self.assertTrue(entered.wait(1))
                release.set()
                for future in futures:
                    self.assertEqual(future.result(timeout=2), self.payload)
            self.assertEqual(run.call_count, 1)

    def test_failed_probe_has_30_second_backoff_but_force_can_retry(self):
        with patch.object(core, "run_codex_capture", side_effect=TimeoutError("probe timeout")) as run:
            for _ in range(3):
                with self.assertRaisesRegex(core.ManagerError, "probe timeout"):
                    core._raw_local_model_catalog()
            self.assertEqual(run.call_count, 1)
            with self.assertRaises(core.ManagerError):
                core._raw_local_model_catalog(force=True)
            self.assertEqual(run.call_count, 2)
            catalog._MODEL_PROBE_FAILURE["at"] -= 31
            with self.assertRaises(core.ManagerError):
                core._raw_local_model_catalog()
            self.assertEqual(run.call_count, 3)

    def test_version_cache_is_installation_bound_and_invalidation_clears_disk(self):
        result = subprocess.CompletedProcess([], 0, "codex 0.153.4", "")
        with patch.object(core, "run_codex_capture", return_value=result) as run:
            self.assertEqual(core.codex_version(), "codex 0.153.4")
            self.cold_memory()
            self.assertEqual(core.codex_version(), "codex 0.153.4")
            self.assertEqual(run.call_count, 1)
            core.invalidate_codex_version_cache()
            self.assertFalse((self.root / "codex-version-probe-cache.json").exists())
            self.assertEqual(core.codex_version(), "codex 0.153.4")
            self.assertEqual(run.call_count, 2)
