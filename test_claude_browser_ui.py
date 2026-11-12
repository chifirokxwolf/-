"""Real Chromium tests against local fixtures based on the user's screenshots."""
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from claude_browser_ui import (ACCOUNT_URL, USAGE_URL, SettingsError, read_usage,
                               read_organization, global_logout_available)
from tools import claude_account_check as check
from tools.claude_account_check import CheckError, inspect_account

ORG = '00000000-0000-0000-0000-000000000001'


@unittest.skipUnless(os.environ.get('RUN_BROWSER_TESTS'), 'Set RUN_BROWSER_TESTS=1 with Playwright and Chromium')
class BrowserUsageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.p = sync_playwright().start()
        cls.browser = cls.p.chromium.launch(headless=True, executable_path=os.environ.get('CHROMIUM_PATH', '/usr/bin/chromium'))

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.p.stop()

    def setUp(self):
        self.context = self.browser.new_context()
        self.page = self.context.new_page()
        self.html = ''
        self.context.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html; charset=utf-8', body=self.html))

    def tearDown(self):
        self.context.close()

    def usage(self, session='0%', weekly='0%', labels=('Current session', 'This week')):
        return f'''<main><section role="dialog" aria-label="Settings"><h2>Your usage</h2>
            <p>No usage yet. 100% of your weekly limit is available.</p>
            <section><div><p>{labels[0]}</p><small>Starts with your first message</small></div><span>{session} used</span></section>
            <section><div><button>{labels[1]}</button><small>Resets Tuesday 7:00 AM</small></div><span>{weekly} used</span></section>
            <p>Usage credits $0</p></section></main>'''

    def load(self, html, url=USAGE_URL):
        self.html = html
        self.page.goto(url)
        self.page.reload()

    def test_read_used_percentages_without_available_summary(self):
        for session, weekly in [('0%', '0%'), ('70%', '71%'), ('70,5%', '100%')]:
            with self.subTest(session=session, weekly=weekly):
                self.load(self.usage(session, weekly))
                self.assertEqual(read_usage(self.page), {'five_hour': float(session[:-1].replace(',', '.')),
                                                        'seven_day': float(weekly[:-1])})

    def test_russian_labels_and_missing_data_fail_closed(self):
        self.load(self.usage('5%', '6%', labels=('Текущая сессия', 'На этой неделе')))
        self.assertEqual(read_usage(self.page)['seven_day'], 6)
        for html in (self.usage().replace('0% used', 'Loading'), self.usage('101%', '0%'),
                     self.usage('-1%', '0%'), '<h1>Cloudflare</h1><p>100% available</p>',
                     self.usage().replace('0% used', '50% available')):
            self.load(html)
            with self.assertRaises(SettingsError):
                read_usage(self.page)

    def test_duplicate_rows_and_both_zero_values_are_ambiguous(self):
        for html in (self.usage() + '<div>Current session</div><span>1%</span>',
                     '<main><p>Current session</p><p>This week</p><span>0%</span><span>0%</span></main>'):
            self.load(html)
            with self.assertRaises(SettingsError):
                read_usage(self.page)

    def test_wrong_origin_or_section_is_rejected(self):
        for url in ('https://example.org/new#settings/usage', ACCOUNT_URL):
            self.load(self.usage(), url)
            with self.assertRaises(SettingsError):
                read_usage(self.page)

    def account(self):
        return f'''<section role="dialog"><div><span>Organization ID</span><span>{ORG}</span></div>
            <button onclick="document.body.dataset.clicked='1'">Log out of all devices</button></section>'''

    def test_account_identity_and_button_are_read_without_clicking(self):
        self.load(self.account(), ACCOUNT_URL)
        self.assertEqual(read_organization(self.page), ORG)
        self.assertTrue(global_logout_available(self.page))
        self.assertIsNone(self.page.locator('body').get_attribute('data-clicked'))
        self.load(self.account() + '<dialog open><button>Log out of all devices</button></dialog>', ACCOUNT_URL)
        with self.assertRaises(SettingsError):
            global_logout_available(self.page)

    def test_real_check_navigation_keeps_bound_identity_and_never_claims_revocation(self):
        # The real UI is a hash-routed settings modal. Reproduce that behavior,
        # rather than treating fragments as network request paths.
        import json
        self.html = '<body><script>const account=' + json.dumps(self.account()) + ';const usage=' + json.dumps(self.usage('12%', '34%')) + ''';
            function render(){document.body.innerHTML=location.hash.includes('usage')?usage:account;}
            addEventListener('hashchange',render);render();</script>'''
        org, result = inspect_account(self.context, ORG)
        self.assertEqual(org, ORG)
        self.assertEqual((result['five_hour'], result['seven_day']), (12, 34))
        self.assertTrue(result['global_logout_button'])
        self.assertFalse(result['logout_tested'])
        with self.assertRaises(CheckError):
            inspect_account(self.context, '00000000-0000-0000-0000-000000000002')

    def test_login_and_challenge_stop_before_identity_prompt(self):
        import json
        for target, code in [('https://claude.ai/login', 'sign_in_manually_first'),
                             ('https://claude.ai/api/challenge_redirect?to=login', 'site_security_check')]:
            with self.subTest(target=target):
                self.context.unroute('**/*')
                def route_request(route):
                    # Model the SPA's auth navigation without any live request
                    # to the real login/challenge service.
                    route.fulfill(status=200, content_type='text/html', body='<title>Login</title>'
                                  '<input type="email"><script>history.replaceState(null,"",'
                                  + json.dumps(target) + ')</script>')
                self.context.route('**/*', route_request)
                confirmation = unittest.mock.Mock()
                with self.assertRaisesRegex(CheckError, '^' + code + '$'):
                    inspect_account(self.context, confirm_identity=confirmation)
                confirmation.assert_not_called()

    def test_browser_events_continue_while_console_waits_for_identity(self):
        self.load(self.account(), ACCOUNT_URL)
        event = threading.Event()
        def routed_request(route):
            route.fulfill(status=200, body='ok')
            event.set()
        self.context.route('**/event-check', routed_request)
        self.page.evaluate("setTimeout(() => fetch('/event-check'), 50)")
        answer = check.prompt_with_browser_events(self.page, 'fixture', reader=lambda _: 'yes' if event.wait(3) else 'no')
        self.assertEqual(answer, 'yes')

    def test_connects_to_real_native_browser_on_a_random_local_port(self):
        original_popen = subprocess.Popen
        children = []
        def launch(arguments, **kwargs):
            # Linux CI has no Windows desktop. These flags belong only to the
            # local test process; production Windows launch does not use them.
            args = arguments[:-1] + ['--headless=new', '--no-sandbox', 'about:blank']
            process = original_popen(args, **kwargs)
            children.append(process)
            return process
        with tempfile.TemporaryDirectory() as folder:
            try:
                with patch.object(check.subprocess, 'Popen', side_effect=launch):
                    connection = check.connect_profile(self.p, os.environ.get('CHROMIUM_PATH', '/usr/bin/chromium'), Path(folder))
                self.assertEqual(len(connection.contexts), 1)
                from tools.claude_manual_login import profile_in_check_mode
                self.assertTrue(profile_in_check_mode(Path(folder)))
                context = connection.contexts[0]
                context.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body=self.usage('12%', '34%')))
                page = context.new_page()
                page.goto(USAGE_URL)
                self.assertEqual(read_usage(page), {'five_hour': 12, 'seven_day': 34})
                connection.close()
            finally:
                for process in children:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)


if __name__ == '__main__':
    unittest.main()
