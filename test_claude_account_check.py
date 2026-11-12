import json
from contextlib import redirect_stdout
import io
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from tools import claude_account_check as check


class AccountCheckTests(unittest.TestCase):
    def test_new_attempt_replaces_stale_error_before_waiting_for_input(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'report.json'
            check.save_json(path, {'result': 'error', 'code': 'old_failure'})
            report = check.CheckReport(path)
            first = json.loads(path.read_text())
            self.assertEqual((first['result'], first['stage']), ('running', 'starting'))
            self.assertNotIn('code', first)
            report.write('running', 'awaiting_identity_confirmation', account_id=1)
            waiting = json.loads(path.read_text())
            self.assertEqual(waiting['run_id'], first['run_id'])
            self.assertEqual(waiting['stage'], 'awaiting_identity_confirmation')
            self.assertIn('checked_at', waiting)
            report.write('cancelled', 'awaiting_identity_confirmation', account_id=1)
            self.assertEqual(json.loads(path.read_text())['result'], 'cancelled')

    def test_error_details_do_not_include_raw_messages_urls_or_email(self):
        try:
            raise RuntimeError('Connection closed https://claude.ai/login?token=PRIVATE test@example.org')
        except RuntimeError as exc:
            data = check.safe_error_details(exc)
        self.assertEqual(data['error_category'], 'connection_closed')
        serialized = json.dumps(data)
        for secret in ('PRIVATE', 'example.org', 'https:', 'token='):
            self.assertNotIn(secret, serialized)

    def test_confirmation_is_on_main_thread_after_browser_session_finishes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            reads = []
            with patch.object(check.sys, 'platform', 'win32'), patch.object(check, 'ROOT', root), \
                    patch('playwright.sync_api.sync_playwright') as manager, \
                    patch.object(check, 'account_record', return_value={'id': 1, 'email': 'fixture@example.org'}), \
                    patch.object(check, 'installed_browsers', return_value={'edge': Path('msedge.exe')}), \
                    patch.object(check, 'connect_profile') as connect, \
                    patch.object(check, 'inspect_account', return_value=('fixture-org', {'five_hour': 12, 'seven_day': 34,
                                 'global_logout_button': True, 'logout_tested': False, 'claude_code_revocation_tested': False})), \
                    redirect_stdout(io.StringIO()):
                connect.return_value.contexts = [Mock()]
                def answer(prompt):
                    import threading
                    self.assertIs(threading.current_thread(), threading.main_thread())
                    reads.append(prompt)
                    state = json.loads((root / 'storage/claude_rental/browser_check.json').read_text())
                    self.assertEqual(state['result'], 'running')
                    if len(reads) < 3:
                        manager.return_value.__enter__.assert_not_called()
                        return '1'
                    manager.return_value.__exit__.assert_called_once()
                    self.assertEqual(state['stage'], 'awaiting_identity_confirmation')
                    return 'ДА'
                with patch('builtins.input', side_effect=answer):
                    self.assertEqual(check.main(), 0)
                result = json.loads((root / 'storage/claude_rental/browser_check.json').read_text())
                self.assertEqual((result['result'], result['stage']), ('ok', 'completed'))
                self.assertEqual(result['five_hour'], 12)
                self.assertNotIn('fixture-org', json.dumps(result))

    def test_endpoint_only_accepts_local_browser_file_format(self):
        with tempfile.TemporaryDirectory() as folder:
            profile = Path(folder)
            port = profile / 'DevToolsActivePort'
            port.write_text('9222\n/devtools/browser/fixture-id')
            self.assertEqual(check.local_endpoint(profile), 'ws://127.0.0.1:9222/devtools/browser/fixture-id')
            for content in ('65536\n/devtools/browser/id', '0\n/devtools/browser/id',
                            '9222\nhttps://other.example/token', '9222\n/devtools/browser/../../id'):
                port.write_text(content)
                with self.assertRaises(check.CheckError):
                    check.local_endpoint(profile)

    def test_native_connection_is_local_and_does_not_copy_cookies(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(check.subprocess, 'Popen') as process:
            profile = Path(folder)
            (profile / 'DevToolsActivePort').write_text('9222\n/devtools/browser/fixture-id')
            p = Mock()
            connection = check.connect_profile(p, Path('msedge.exe'), profile)
            self.assertIs(connection, p.chromium.connect_over_cdp.return_value)
            args = process.call_args.args[0]
            self.assertIn('--remote-debugging-address=127.0.0.1', args)
            self.assertIn('--remote-debugging-port=0', args)
            self.assertIn('--user-data-dir=' + str(profile), args)
            self.assertNotIn('--new-window', args)
            self.assertEqual(args[-1], 'about:blank')
            self.assertFalse(process.call_args.kwargs['shell'])
            p.chromium.connect_over_cdp.assert_called_once_with('ws://127.0.0.1:9222/devtools/browser/fixture-id', timeout=1200)

    def test_account_lookup_is_read_only_and_does_not_return_pickup_token(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(check.os.environ, {}, clear=True):
            root = Path(folder)
            config = root / 'storage/claude_rental/config.json'
            with self.assertRaises(check.CheckError):
                check.account_record(root, 1)
            check.save_json(config, {'database': 'accounts.sqlite3'})
            with sqlite3.connect(root / 'accounts.sqlite3') as c:
                c.execute('CREATE TABLE accounts(id INTEGER PRIMARY KEY, email TEXT, pickup TEXT)')
                c.execute('INSERT INTO accounts VALUES (1,?,?)', ('test@example.org', 'LOCAL-SECRET'))
            self.assertEqual(check.account_record(root, 1), {'id': 1, 'email': 'test@example.org'})
            with self.assertRaises(check.CheckError):
                check.account_record(root, 2)

    def test_snapshot_saved_atomically(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'nested/report.json'
            check.save_json(path, {'result': 'error'})
            check.save_json(path, {'result': 'ok', 'five_hour': 0.0})
            self.assertEqual(json.loads(path.read_text()), {'result': 'ok', 'five_hour': 0.0})
            self.assertEqual(list(path.parent.iterdir()), [path])


if __name__ == '__main__':
    unittest.main()
