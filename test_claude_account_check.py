import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from tools import claude_account_check as check


class AccountCheckTests(unittest.TestCase):
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
