import ctypes
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import agent_manager.platform.dlls as isolation


class FrozenDllIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='agent-manager-dll-fixture-')
        self.root = Path(self.temp.name).resolve()
        self.assertEqual(self.root.parent, Path(tempfile.gettempdir()).resolve())
        self.assertTrue(self.root.name.startswith('agent-manager-dll-fixture-'))
        self.addCleanup(self.temp.cleanup)
        for obj, field, value in [(isolation, '_INSTALLED', False),
                                   (isolation, '_DLL_DIRECTORY_HANDLES', []),
                                   (isolation, '_ORIGINAL_POPEN_INIT', None),
                                   (subprocess.Popen, '__init__', subprocess.Popen.__init__)]:
            item = patch.object(obj, field, value)
            item.start()
            self.addCleanup(item.stop)

    def test_path_filter_preserves_siblings_system_paths_and_private_restart_state(self):
        original = {'Path': r'C:\Windows;C:\Temp\_MEI123;"C:\Temp\_MEI123\webview\native";C:\Temp\_MEI1234;D:\Tools',
                    'PYINSTALLER_RESET_ENVIRONMENT': '1', '_PYI_APPLICATION_HOME_DIR': 'kept', 'OTHER': 'untouched'}
        result = isolation.sanitize_environment(original, r'C:\Temp\_MEI123')
        self.assertEqual(result['Path'], r'C:\Windows;C:\Temp\_MEI1234;D:\Tools')
        self.assertIn(r'_MEI123\webview', original['Path'])
        self.assertEqual(result['PYINSTALLER_RESET_ENVIRONMENT'], '1')
        self.assertEqual(result['_PYI_APPLICATION_HOME_DIR'], 'kept')
        self.assertEqual(result['OTHER'], 'untouched')

    def test_native_folders_are_architecture_specific(self):
        for arch in ('x64', 'x86', 'arm64'):
            (self.root / 'webview' / 'lib' / 'runtimes' / f'win-{arch}' / 'native').mkdir(parents=True)
        paths = isolation.dll_directories(self.root, machine='AMD64', pointer_bits=64)
        self.assertTrue(any('win-x64' in str(path) for path in paths))
        self.assertFalse(any('win-x86' in str(path) or 'win-arm64' in str(path) for path in paths))

    def test_unfrozen_execution_has_no_global_side_effect(self):
        with patch.object(sys, 'frozen', False, create=True):
            self.assertFalse(isolation.install())
        self.assertEqual(isolation._DLL_DIRECTORY_HANDLES, [])

    def test_runtime_diagnostic_reads_buffer_not_required_capacity(self):
        kernel = Mock()
        def getter(length, buffer):
            if length == 0:
                return 1  # Empty directory still needs a NUL character.
            buffer.value = ''
            return 0
        kernel.GetDllDirectoryW.side_effect = getter
        with patch.object(isolation, '_INSTALLED', True), patch.object(sys, 'platform', 'win32'), patch.object(ctypes, 'WinDLL', return_value=kernel):
            self.assertTrue(isolation.runtime_status()['inheritedDllDirectoryEmpty'])

    def test_install_is_once_and_handles_remain_alive(self):
        kernel = Mock()
        kernel.SetDefaultDllDirectories.return_value = 1
        kernel.SetDllDirectoryW.return_value = 1
        handle = Mock()
        calls = []
        def fake_init(instance, *args, **kwargs):
            calls.append((args, kwargs))
        with patch.object(sys, 'platform', 'win32'), patch.object(sys, 'frozen', True, create=True), \
             patch.object(sys, '_MEIPASS', str(self.root), create=True), \
             patch.object(ctypes, 'WinDLL', return_value=kernel, create=True), \
             patch.object(os, 'add_dll_directory', return_value=handle, create=True), \
             patch.object(subprocess.Popen, '__init__', fake_init):
            self.assertTrue(isolation.install())
            self.assertTrue(isolation.install())
            kernel.SetDllDirectoryW.assert_called_once_with(None)
            kernel.SetDefaultDllDirectories.assert_called_once_with(0x1000)
            handle.close.assert_not_called()
            env = {'PATH': str(self.root) + ';C:\\Windows'}
            subprocess.Popen.__init__(object(), ['external.exe'], env=env)
            self.assertEqual(calls[-1][1]['env']['PATH'], 'C:\\Windows')
            self.assertEqual(env['PATH'], str(self.root) + ';C:\\Windows')
            self.assertEqual(kernel.SetDllDirectoryW.call_count, 1)
            self.assertEqual(len(isolation._DLL_DIRECTORY_HANDLES), 1)

    def test_failed_policy_install_closes_cookies_and_fails_explicitly(self):
        kernel = Mock()
        kernel.SetDefaultDllDirectories.return_value = 0
        handle = Mock()
        with patch.object(sys, 'platform', 'win32'), patch.object(sys, 'frozen', True, create=True), \
             patch.object(sys, '_MEIPASS', str(self.root), create=True), \
             patch.object(ctypes, 'WinDLL', return_value=kernel, create=True), \
             patch.object(os, 'add_dll_directory', return_value=handle, create=True), \
             patch.object(ctypes, 'WinError', return_value=OSError('policy failed'), create=True), \
             patch.object(ctypes, 'get_last_error', return_value=5, create=True):
            with self.assertRaises(OSError):
                isolation.install()
        handle.close.assert_called_once()
        self.assertFalse(isolation._INSTALLED)



if __name__ == '__main__':
    unittest.main()
