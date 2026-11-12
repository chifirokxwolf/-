"""SQLite integration and Chromium fixtures. Never connects to a live Claude account."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from rental import Store, Unavailable
from claude_auto_kick import AutoKick, BrowserKicker, KickError, exclusive_worker, revoke_in_browser

ORG = '00000000-0000-0000-0000-000000000001'


class AutoKickTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / 'rentals.sqlite3')
        self.store.add('fixture@example.org', 'https://remail.best/pickup?email=fixture%40example.org&token=fixture')
        self.store.initialized(1)
        self.store.usage(1, 0, 0)
        self.store.reserve('ORDER', 'buyer', 'chat', 60)
        self.store.active('ORDER')
        self.kicker = Mock()
        self.kicker.revoke.return_value = True
        self.worker = AutoKick(self.store, self.kicker)

    def tearDown(self):
        self.tmp.cleanup()

    def expire(self, state='active'):
        with self.store.db() as c:
            c.execute('UPDATE rentals SET expires=?,state=?', (time.time() - 1, state))

    def row(self):
        return self.store.recent_rentals()[0]

    def test_active_rental_is_never_kicked(self):
        self.worker.tick()
        self.kicker.revoke.assert_not_called()
        self.assertEqual(self.row()['state'], 'active')

    def test_boundary_expiration_claims_cleanup(self):
        end = self.row()['expires']
        self.assertEqual(self.store.pending_cleanup(now=end - 0.001), [])
        self.assertEqual(len(self.store.pending_cleanup(now=end)), 1)
        self.assertTrue(self.store.claim_kick('ORDER', now=end))

    def test_expired_kick_survives_restart_without_repeating(self):
        self.expire()
        self.worker.tick()
        self.worker.tick()
        AutoKick(Store(self.store.path), self.kicker).tick()
        self.kicker.revoke.assert_called_once()
        self.assertEqual(self.row()['kick_state'], 'browser_revoked')
        self.assertEqual(self.row()['state'], 'cleanup')
        self.assertEqual(self.store.accounts()[0]['state'], 'hold')
        self.store.usage(1, 0, 0)
        with self.assertRaises(Unavailable):
            self.store.reserve('OTHER', 'another', 'chat2', 60)

    def test_issuing_expiry_is_also_cleaned_up(self):
        self.expire('issuing')
        self.worker.tick()
        self.assertEqual(self.row()['kick_state'], 'browser_revoked')

    def test_renewal_extends_before_cleanup_and_duplicate_does_not_extend_twice(self):
        before = self.row()['expires']
        self.store.renew('EXTEND', 'buyer', 'chat', 3600)
        self.store.renew('EXTEND', 'buyer', 'chat', 3600)
        self.assertEqual(self.row()['expires'], before + 3600)
        self.assertEqual(self.store.pending_cleanup(now=before + 1), [])
        self.worker.tick()
        self.kicker.revoke.assert_not_called()

    def test_cleanup_cannot_be_renewed_or_bonus_awarded(self):
        self.expire()
        self.worker.tick()
        self.assertIsNone(self.store.renew('LATE', 'buyer', 'chat', 60))
        self.assertEqual(self.store.award_review_bonus('ORDER', 'buyer')['state'], 'too_late')

    def test_failure_backoff_restart_retry_and_secret_redaction(self):
        self.expire()
        self.kicker.revoke.side_effect = RuntimeError('SECRET https://claude.ai/login?token=private')
        self.worker.tick()
        self.assertEqual(self.row()['kick_state'], 'retry')
        self.assertEqual(self.row()['kick_error'], 'browser_logout_unconfirmed')
        AutoKick(Store(self.store.path), self.kicker).tick()
        self.assertEqual(self.kicker.revoke.call_count, 1)
        with self.store.db() as c:
            c.execute('UPDATE auto_kicks SET next_attempt=0')
        self.kicker.revoke.side_effect = None
        self.worker.tick()
        self.assertEqual(self.row()['kick_state'], 'browser_revoked')
        self.assertEqual(self.row()['kick_attempts'], 2)

    def test_false_and_non_boolean_success_are_failures(self):
        self.expire()
        for value in (False, None, 1, 'ok'):
            self.kicker.revoke.return_value = value
            with self.store.db() as c:
                c.execute('UPDATE auto_kicks SET next_attempt=0')
            self.worker.tick()
            self.assertEqual(self.row()['kick_state'], 'retry')

    def test_crashed_claim_retries_after_backoff(self):
        self.expire()
        self.store.pending_cleanup()
        self.assertTrue(self.store.claim_kick('ORDER', now=time.time()))
        self.worker.tick()
        self.kicker.revoke.assert_not_called()
        with self.store.db() as c:
            c.execute('UPDATE auto_kicks SET next_attempt=0')
        AutoKick(Store(self.store.path), self.kicker).tick()
        self.assertEqual(self.row()['kick_state'], 'browser_revoked')

    def test_concurrent_worker_cannot_act_during_live_logout(self):
        self.expire()
        with exclusive_worker(self.store.path + '.autokick.lock') as acquired:
            self.assertTrue(acquired)
            self.worker.tick()
            self.kicker.revoke.assert_not_called()
        self.worker.tick()
        self.kicker.revoke.assert_called_once()

    def test_stop_prevents_browser_action(self):
        self.expire()
        stop = threading.Event()
        stop.set()
        self.worker.tick(stop)
        self.kicker.revoke.assert_not_called()

    def test_binding_requires_matching_email_and_does_not_launch_browser(self):
        root = Path(self.tmp.name)
        kicker = BrowserKicker(root)
        record = {'account_id': 1, 'email': 'fixture@example.org'}
        with self.assertRaisesRegex(KickError, 'browser_binding_missing'):
            kicker.revoke(record)
        path = root / 'storage/claude_rental/browser_bindings.json'
        path.parent.mkdir(parents=True)
        for binding in ({'browser': 'edge', 'organization_id': ORG},
                        {'browser': 'edge', 'organization_id': ORG, 'email': 'other@example.org'}):
            path.write_text(json.dumps({'1': binding}))
            with self.assertRaisesRegex(KickError, 'rerun_account_check'):
                kicker.revoke(record)

    def test_browser_adapter_uses_bound_profile_and_real_verifier(self):
        root = Path(self.tmp.name)
        path = root / 'storage/claude_rental/browser_bindings.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'1': {'browser': 'edge', 'organization_id': ORG, 'email': 'fixture@example.org'}}))
        context = object()
        browser = SimpleNamespace(contexts=[context])
        with patch('claude_auto_kick.installed_browsers', return_value={'edge': Path('msedge.exe')}), \
                patch('claude_auto_kick.connect_profile', return_value=browser) as connect, \
                patch('claude_auto_kick.revoke_in_browser', return_value=True) as revoke, \
                patch('playwright.sync_api.sync_playwright') as manager:
            self.assertTrue(BrowserKicker(root).revoke({'account_id': 1, 'email': 'fixture@example.org'}))
            self.assertEqual(connect.call_args.args[2], (root / 'storage/claude_rental/browser_profiles/1/edge-manual').resolve())
            revoke.assert_called_once_with(browser, context, ORG)
            manager.return_value.__exit__.assert_called_once()

    def test_failure_on_one_account_does_not_skip_another(self):
        self.store.add('second@example.org', 'https://remail.best/pickup?email=second%40example.org&token=fixture')
        self.store.initialized(2)
        self.store.usage(2, 0, 0)
        self.store.reserve('SECOND', 'buyer2', 'chat2', 60)
        self.store.active('SECOND')
        self.expire()
        self.kicker.revoke.side_effect = [RuntimeError('fixture network error'), True]
        self.worker.tick()
        states = {r['order_id']: r['kick_state'] for r in self.store.recent_rentals()}
        self.assertEqual(states, {'ORDER': 'retry', 'SECOND': 'browser_revoked'})

    def test_existing_database_migrates_without_losing_rental(self):
        with self.store.db() as c:
            c.execute('DROP TABLE auto_kicks')
        migrated = Store(self.store.path)
        self.assertEqual(migrated.recent_rentals()[0]['order_id'], 'ORDER')
        self.assertEqual(migrated.accounts()[0]['email'], 'fixture@example.org')


class BundledPluginTests(unittest.TestCase):
    def test_only_one_file_in_clean_directory_worker_runs_without_sales_adapter(self):
        from build_plugin import build
        bundle = build()
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'plugin.py'
            target.write_bytes(bundle.read_bytes())
            script = '''
import importlib.util, time, threading
from pathlib import Path
from types import SimpleNamespace
spec=importlib.util.spec_from_file_location('fixture_plugin', 'plugin.py')
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
c=SimpleNamespace(plugins={m.UUID:SimpleNamespace(enabled=True)})
r=m.Runtime(c, Path('storage/claude_rental/config.json'))
assert r.engine is None and r.config['enabled'] is False
r.store.add('a@example.org','https://remail.best/pickup?email=a%40example.org&token=fixture')
r.store.initialized(1);r.store.usage(1,0,0);r.store.reserve('ORDER','buyer','chat',1);r.store.active('ORDER',now=time.time()-10)
calls=[]
r.auto_kick.kicker=SimpleNamespace(revoke=lambda row: calls.append(row['order_id']) or True)
# Occupied delivery lock must not block the independent cleanup worker.
r.provider_lock.acquire()
r.start()
try:
    deadline=time.monotonic()+3
    while time.monotonic()<deadline and r.store.recent_rentals()[0]['kick_state']!='browser_revoked':
        time.sleep(.02)
    assert r.store.recent_rentals()[0]['kick_state']=='browser_revoked'
    assert calls==['ORDER']
finally:
    r.shutdown();r.provider_lock.release()
    for t in r.threads:t.join(2)
r2=m.Runtime(c,Path('storage/claude_rental/config.json'))
r2.auto_kick.kicker=SimpleNamespace(revoke=lambda row: (_ for _ in ()).throw(AssertionError('Repeated kick')))
r2.kick_once()
assert r2.store.recent_rentals()[0]['kick_state']=='browser_revoked'
print('Standalone bundle, paused sales, independent worker, restart: OK')
'''
            result = subprocess.run([sys.executable, '-I', '-c', script], cwd=folder,
                                    text=True, capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('restart: OK', result.stdout)


@unittest.skipUnless(os.environ.get('RUN_BROWSER_TESTS'), 'Requires Playwright/Chromium')
class BrowserAutoKickTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.p = sync_playwright().start()
        cls.browser = cls.p.chromium.launch(headless=True, executable_path=os.environ.get('CHROMIUM_PATH','/usr/bin/chromium'))

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.p.stop()

    def setUp(self):
        self.revoked = False
        self.clicks = 0
        self.mode = 'global'
        self.contexts = []
        self.facade = SimpleNamespace(new_context=self.new_context)
        self.context = self.new_context()
        self.context.add_cookies([{'name':'sessionKey','value':'fixture','domain':'claude.ai','path':'/','secure':True}])

    def new_context(self, **kwargs):
        c = self.browser.new_context(**kwargs)
        c.route('**/*', self.respond)
        self.contexts.append(c)
        return c

    def tearDown(self):
        for c in self.contexts:
            c.close()

    def respond(self, route):
        url = route.request.url
        if '/fixture/logout' in url:
            self.clicks += 1
            if self.mode != 'local_only':
                self.revoked = True
            return route.fulfill(status=200, body='ok')
        if self.revoked and self.mode == 'network_failure':
            return route.abort()
        if self.revoked and self.mode == 'http_error':
            return route.fulfill(status=403, body='<input type="email">')
        if self.revoked:
            if '/login' not in url:
                return route.fulfill(status=200, content_type='text/html', body="<script>location.replace('/login')</script>")
            return route.fulfill(status=200, content_type='text/html', body='<h1>Log in</h1><input type="email">')
        html = f'''<section role="dialog"><div><span>Organization ID</span><span>{ORG}</span></div>
        <button onclick="setTimeout(()=>document.querySelector('#confirm').showModal(),100)">Log out of all devices</button>
        </section><dialog id="confirm"><button onclick="fetch('/fixture/logout',{{method:'POST'}}).then(()=>{{document.cookie='sessionKey=; max-age=0; path=/';location.href='/login'}})">Log out of all devices</button></dialog>'''
        route.fulfill(status=200,content_type='text/html',body=html)

    def perform(self, org=ORG):
        return revoke_in_browser(self.facade,self.context,org,timeout_ms=1500)

    def test_browser_and_database_end_to_end_repeat_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            store=Store(Path(folder)/'rentals.sqlite3')
            store.add('a@example.org','https://remail.best/pickup?email=a%40example.org&token=fixture')
            store.initialized(1);store.usage(1,0,0);store.reserve('ORDER','buyer','chat',1)
            store.active('ORDER',now=time.time()-2)
            kicker=SimpleNamespace(revoke=lambda r:self.perform())
            AutoKick(store,kicker).tick()
            self.assertEqual(store.recent_rentals()[0]['kick_state'],'browser_revoked')
            AutoKick(Store(store.path),kicker).tick()
            self.assertEqual(self.clicks,1)
            self.assertEqual(store.accounts()[0]['state'],'hold')

    def test_local_cookie_deletion_is_not_remote_revocation(self):
        self.mode='local_only'
        with self.assertRaises(Exception):
            self.perform()
        self.assertEqual(self.clicks,1)
        self.assertFalse(self.revoked)

    def test_wrong_account_prevents_click(self):
        with self.assertRaisesRegex(KickError,'identity'):
            self.perform('00000000-0000-0000-0000-000000000002')
        self.assertEqual(self.clicks,0)

    def test_network_failure_after_click_is_not_success(self):
        self.mode='network_failure'
        with self.assertRaises(Exception):
            self.perform()
        self.assertEqual(self.clicks,1)

    def test_http_error_after_click_is_not_success(self):
        self.mode='http_error'
        with self.assertRaises(Exception):
            self.perform()
        self.assertEqual(self.clicks,1)


if __name__ == '__main__':
    unittest.main()
