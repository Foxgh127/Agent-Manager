"""Deterministic Windows shutdown budgets; taskkill and process scans are mocked."""

import subprocess
import unittest
from unittest.mock import patch

import agent_manager.core as core


class VirtualClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += max(0, seconds)


class CodexCloseDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.clock = VirtualClock()
        for target, name, value in (
            (core.os, "name", "nt"),
            (core.time, "monotonic", self.clock.monotonic),
            (core.time, "sleep", self.clock.sleep),
        ):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def graph(children=63):
        return [
            {"name": "ChatGPT.exe", "pid": "100", "parentPid": "9"},
            *[{"name": "codex.exe", "pid": str(101 + i), "parentPid": "100"} for i in range(children)],
        ]

    def test_many_hung_taskkill_calls_share_total_timeout(self):
        graph = self.graph(120)
        commands = []

        def hung(command, **kwargs):
            commands.append((command, kwargs["timeout"]))
            self.clock.sleep(kwargs["timeout"])
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])

        with patch.object(core, "running_codex_processes", return_value=graph), patch.object(core.subprocess, "run", side_effect=hung):
            with self.assertRaisesRegex(core.ManagerError, "无法自动关闭 Codex"):
                core.close_codex_processes(timeout_seconds=10)
        self.assertAlmostEqual(self.clock.now, 10)
        self.assertEqual(len(commands), 2)
        self.assertAlmostEqual(commands[0][1], 3)
        self.assertAlmostEqual(commands[1][1], 7)
        self.assertNotIn("/F", commands[0][0])
        self.assertIn("/F", commands[1][0])
        for command, _timeout in commands:
            self.assertNotIn("/T", command)
            self.assertNotIn("/IM", command)

    def test_same_depth_batch_avoids_per_renderer_command_latency(self):
        graph = self.graph(63)
        commands = []

        def close(command, **_kwargs):
            commands.append(command)
            self.clock.sleep(0.2)

        def scan():
            return graph if not commands else []

        with patch.object(core, "running_codex_processes", side_effect=scan), patch.object(core.subprocess, "run", side_effect=close):
            result = core.close_codex_processes()
        self.assertEqual(len(commands), 2)
        self.assertEqual(commands[0], ["taskkill.exe", "/PID", "100"])
        self.assertEqual(commands[1].count("/PID"), 63)
        self.assertAlmostEqual(self.clock.now, 0.4)
        self.assertEqual(len(result["closed"]), 64)
        self.assertEqual(result["forced"], [])

    def test_short_total_budget_reserves_force_pass_and_verified_failure(self):
        def hung(command, **kwargs):
            self.clock.sleep(kwargs["timeout"])
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])

        with patch.object(core, "running_codex_processes", return_value=self.graph()), patch.object(core.subprocess, "run", side_effect=hung):
            with self.assertRaises(core.ManagerError):
                core.close_codex_processes(timeout_seconds=1)
        self.assertAlmostEqual(self.clock.now, 1)

    def test_force_pass_revalidates_and_runs_children_before_roots(self):
        active = self.graph(2)
        commands = []
        scans = []

        def scan():
            scans.append(len(commands))
            return list(active)

        def close(command, **_kwargs):
            commands.append(command)
            if "/F" in command:
                pids = {command[i + 1] for i, arg in enumerate(command) if arg == "/PID"}
                active[:] = [item for item in active if item["pid"] not in pids]

        with patch.object(core, "running_codex_processes", side_effect=scan), patch.object(core.subprocess, "run", side_effect=close):
            result = core.close_codex_processes(4)
        self.assertEqual(commands[-2:], [
            ["taskkill.exe", "/F", "/PID", "102", "/PID", "101"],
            ["taskkill.exe", "/F", "/PID", "100"],
        ])
        self.assertEqual(result["forced"], ["102", "101", "100"])
        self.assertIn(len(commands) - 1, scans)

    def test_pid_no_longer_path_verified_is_not_forced(self):
        graph = self.graph(1)
        commands = []
        at_force = 0

        def scan():
            nonlocal at_force
            if self.clock.now >= 2:
                at_force += 1
                if at_force > 1:
                    return [] if any("/F" in command for command in commands) else graph[:1]
            return graph

        with patch.object(core, "running_codex_processes", side_effect=scan), patch.object(core.subprocess, "run", side_effect=lambda command, **_kw: commands.append(command)):
            result = core.close_codex_processes(4)
        forced = [command for command in commands if "/F" in command]
        self.assertEqual(forced, [["taskkill.exe", "/F", "/PID", "100"]])
        self.assertEqual(result["forced"], ["100"])

    def test_unknown_scan_after_budget_is_never_reported_as_stopped(self):
        graph = self.graph()

        def hung(command, **kwargs):
            self.clock.sleep(kwargs["timeout"])
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])

        def scan():
            if self.clock.now >= 1:
                return core.CodexProcessScan([], known=False, error="unknown")
            return graph

        with patch.object(core, "running_codex_processes", side_effect=scan), patch.object(core.subprocess, "run", side_effect=hung):
            with self.assertRaises(core.CodexProcessScanError):
                core.close_codex_processes(1)
        self.assertAlmostEqual(self.clock.now, 1)


if __name__ == "__main__":
    unittest.main()
