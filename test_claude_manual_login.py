"""Windows launch boundaries; no real account, browser or network is used."""
from contextlib import redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from tools import claude_manual_login as manual


class ManualLoginTests(unittest.TestCase):
    def test_manual_launch_rejects_a_live_check_process_without_killing_it(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            profile = manual.profile_path(1, 'edge', root)
            profile.mkdir(parents=True)
            (profile / 'DevToolsActivePort').write_text('9222\n/devtools/browser/fixture-id')
            with patch.object(manual.http.client, 'HTTPConnection') as http, patch.object(manual.subprocess, 'Popen') as launch:
                response = http.return_value.getresponse.return_value
                response.status = 200
                response.read.return_value = b'{"webSocketDebuggerUrl":"ws://127.0.0.1:9222/devtools/browser/fixture-id"}'
                with self.assertRaises(manual.ProfileInCheckMode):
                    manual.launch_browser(Path('msedge.exe'), 1, 'edge', root)
                launch.assert_not_called()
                http.return_value.close.assert_called_once()
                response.read.return_value = b'{"webSocketDebuggerUrl":"ws://127.0.0.1:9222/devtools/browser/another-profile"}'
                self.assertFalse(manual.profile_in_check_mode(profile))

    def test_detects_user_chrome_and_system_edge_without_using_path(self):
        with tempfile.TemporaryDirectory(prefix='manual profile ') as folder:
            root = Path(folder)
            chrome = root / 'local' / manual.BROWSERS['chrome'][2]
            edge = root / 'programs' / manual.BROWSERS['edge'][2]
            for file in (chrome, edge):
                file.parent.mkdir(parents=True)
                file.touch()
            with patch.dict(manual.os.environ, {'LOCALAPPDATA': str(root / 'local'),
                            'ProgramFiles': str(root / 'programs'), 'ProgramFiles(x86)': ''}, clear=True), \
                    patch.object(manual, 'registered_path', return_value=None):
                self.assertEqual(manual.installed_browsers(), {'chrome': chrome, 'edge': edge})

    def test_registry_fallback_supports_custom_install_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            browser = Path(folder) / 'chrome.exe'
            browser.touch()
            with patch.dict(manual.os.environ, {}, clear=True), \
                    patch.object(manual, 'registered_path', side_effect=lambda name: browser if name == 'chrome.exe' else None):
                self.assertEqual(manual.installed_browsers(), {'chrome': browser})

    def test_launch_keeps_account_browser_and_personal_profiles_separate(self):
        with tempfile.TemporaryDirectory(prefix='manual profile ') as folder, \
                patch.object(manual.subprocess, 'Popen') as start:
            root = Path(folder)
            chrome = root / 'Program Files' / 'chrome.exe'
            for account_id, browser in ((1, 'chrome'), (1, 'chrome'), (2, 'chrome'), (1, 'edge')):
                manual.launch_browser(chrome, account_id, browser, root)
            calls = start.call_args_list
            profiles = [next(a for a in c.args[0] if a.startswith('--user-data-dir=')) for c in calls]
            self.assertEqual(profiles[0], profiles[1])
            self.assertEqual(len(set(profiles)), 3)
            for call, profile in zip(calls, profiles):
                self.assertFalse(call.kwargs['shell'])
                self.assertEqual(call.args[0][-1], 'https://claude.ai/')
                self.assertTrue(Path(profile.split('=', 1)[1]).is_dir())
                self.assertFalse(any('remote-debugging' in x or 'disable-' in x or 'automation' in x for x in call.args[0]))
            for invalid in ('../1', 0, -1, True):
                with self.assertRaises(ValueError):
                    manual.launch_browser(chrome, invalid, 'chrome', root)
            self.assertEqual(start.call_count, 4)

    def test_missing_requested_browser_never_launches_another_browser(self):
        with patch.object(manual.sys, 'platform', 'win32'), \
                patch.object(manual, 'installed_browsers', return_value={'edge': Path('msedge.exe')}), \
                patch.object(manual, 'launch_browser') as launch, redirect_stdout(io.StringIO()):
            self.assertEqual(manual.main(['--account-id', '1', '--browser', 'chrome']), 2)
            launch.assert_not_called()

    def test_interactive_manual_launch_needs_no_playwright(self):
        with patch.object(manual.sys, 'platform', 'win32'), \
                patch.object(manual, 'installed_browsers', return_value={'chrome': Path('chrome.exe')}), \
                patch('builtins.input', side_effect=['7', '1']), \
                patch.object(manual, 'launch_browser') as launch, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(manual.main([]), 0)
            launch.assert_called_once_with(Path('chrome.exe'), 7, 'chrome')
            self.assertIn('claude_account.cmd', output.getvalue())

    def test_download_bundle_contains_the_launcher_companion_and_instructions(self):
        from build_plugin import build_manual_login, TOOL_FILES
        root = Path(__file__).resolve().parent
        archive = build_manual_login(root)
        with zipfile.ZipFile(archive) as z:
            self.assertIsNone(z.testzip())
            self.assertEqual(set(z.namelist()), set(TOOL_FILES) | {'tools/claude_browser_ui.py', 'tools/claude_browser_actions.py'})
            for name in z.namelist():
                source = root / (Path(name).name if Path(name).name in ('claude_browser_ui.py','claude_browser_actions.py') else name)
                self.assertEqual(z.read(name), source.read_bytes())


if __name__ == '__main__':
    unittest.main()
