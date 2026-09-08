import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import agent_manager_core as core


class RuntimeWrapperSafetyTests(unittest.TestCase):
    def test_verify_npm_wrapper_executes_node_and_javascript_entry_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wrapper = root / "npm" / "codex.cmd"
            javascript = root / "npm" / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
            node = root / "nodejs" / "node.exe"
            wrapper.parent.mkdir(parents=True)
            javascript.parent.mkdir(parents=True)
            node.parent.mkdir(parents=True)
            wrapper.write_text("@echo off", encoding="utf-8")
            javascript.write_text("// cli", encoding="utf-8")
            node.write_bytes(b"native")
            completed = subprocess.CompletedProcess([], 0, "codex-cli 0.147.0\n", "")
            with (
                patch.object(core, "_discover_node_npm_runtime", return_value={"node": str(node)}),
                patch.object(core.subprocess, "run", return_value=completed) as run,
            ):
                version = core._verify_codex_cli(wrapper)
            self.assertEqual(version, "codex-cli 0.147.0")
            command = run.call_args.args[0]
            self.assertEqual(command[:2], [str(node), str(javascript)])
            self.assertNotIn(str(wrapper), command)

    def test_unresolved_wrapper_is_rejected_instead_of_spawned(self):
        with tempfile.TemporaryDirectory() as temporary:
            wrapper = Path(temporary) / "codex.cmd"
            wrapper.write_text("@echo off", encoding="utf-8")
            with (
                patch.object(core, "_discover_node_npm_runtime", return_value=None),
                patch.object(core.subprocess, "run") as run,
            ):
                with self.assertRaisesRegex(core.ManagerError, "spawn EINVAL"):
                    core._verify_codex_cli(wrapper)
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
