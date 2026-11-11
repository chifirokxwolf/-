"""Validate local profile persistence with real Chromium and intercepted pages."""
import os
import tempfile
import unittest

from tools.claude_session import open_context, profile_path


@unittest.skipUnless(os.environ.get('RUN_BROWSER_TESTS'), 'Set RUN_BROWSER_TESTS=1 with Playwright and Chromium')
class OwnerProfileTests(unittest.TestCase):
    def test_login_state_persists_only_in_its_account_profile(self):
        from playwright.sync_api import sync_playwright
        executable = os.environ.get('CHROMIUM_PATH', '/usr/bin/chromium')
        with tempfile.TemporaryDirectory() as root, sync_playwright() as p:
            for account_id, expected_cookie in [(1, False), (1, True), (2, False)]:
                with self.subTest(account=account_id, returning=expected_cookie):
                    context = open_context(p, profile_path(account_id, root), headless=True,
                                           executable_path=executable)
                    try:
                        context.route('**/*', lambda route: route.fulfill(status=200, body='<h1>Local fixture</h1>'))
                        page = context.pages[0]
                        page.goto('https://claude.ai/')
                        self.assertEqual(page.evaluate('localStorage.getItem("fixture-login")'),
                                         'signed-in' if expected_cookie else None)
                        self.assertEqual(any(c['name'] == 'fixture-session' for c in context.cookies()), expected_cookie)
                        context.add_cookies([{'name': 'fixture-session', 'value': 'local-only',
                                              'domain': 'claude.ai', 'path': '/', 'secure': True,
                                              'httpOnly': True, 'expires': 2147483647}])
                        page.evaluate('localStorage.setItem("fixture-login", "signed-in")')
                    finally:
                        context.close()


if __name__ == '__main__':
    unittest.main()
