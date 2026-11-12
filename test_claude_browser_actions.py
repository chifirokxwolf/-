"""Actual Chromium tests on intercepted local HTML; no live Claude access."""
import os
import unittest
from html import escape
from claude_browser_actions import GLOBAL_LABELS, LogoutError, logout_everywhere


@unittest.skipUnless(os.environ.get('RUN_BROWSER_TESTS'), 'Set RUN_BROWSER_TESTS=1 with Playwright and Chromium')
class BrowserLogoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(executable_path=os.environ.get('CHROMIUM_PATH', '/usr/bin/chromium'),
                                                     headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.context = self.browser.new_context()
        self.page = self.context.new_page()

    def tearDown(self):
        self.context.close()

    def html(self, label, confirm='Confirm', *, duplicate=False, direct=False):
        action = "document.body.dataset.clicked='1';"
        action += "document.body.dataset.revoked='1'" if direct else "document.querySelector('dialog').showModal()"
        button = f'<button onclick="{action}">{escape(label)}</button>'
        markup = ('<body data-clicked="0" data-revoked="0"><span data-testid="account">fixture@example.org</span>'
                  '<button onclick="document.body.dataset.local=\'1\'">Log out</button>' + button
                  + (button if duplicate else '') + '<dialog><p>Confirm global logout</p>'
                  '<button onclick="document.body.dataset.revoked=\'1\';this.parentElement.close()">'
                  + escape(confirm) + '</button><button onclick="this.parentElement.close()">Cancel</button></dialog>')
        self.context.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html; charset=utf-8', body=markup))
        self.page.goto('https://claude.ai/settings/account')

    def perform(self, verify=None, identity=None):
        return logout_everywhere(self.page, timeout_ms=300,
            verify_account=identity or (lambda p: p.get_by_test_id('account').inner_text() == 'fixture@example.org'),
            verify_revoked=verify or (lambda ms: self.page.locator('body').get_attribute('data-revoked') == '1'))

    def test_supported_languages_click_global_action_and_scoped_confirmation(self):
        confirmations = {'ru': 'Выйти', 'en': 'Log out', 'uk': 'Вийти', 'de': 'Abmelden',
                         'fr': 'Se déconnecter', 'es': 'Cerrar sesión', 'pt': 'Sair',
                         'zh': '退出', 'ja': 'ログアウト', 'ko': '로그아웃'}
        for locale, labels in GLOBAL_LABELS.items():
            for label in labels:
                with self.subTest(locale=locale, label=label):
                    self.context.unroute('**/*')
                    self.html(label, confirmations[locale])
                    self.assertTrue(self.perform())
                    self.assertIsNone(self.page.locator('body').get_attribute('data-local'))

    def test_only_local_logout_is_never_clicked(self):
        self.html('Log out')
        with self.assertRaisesRegex(LogoutError, 'global logout button'):
            self.perform()
        self.assertEqual(self.page.locator('body').get_attribute('data-clicked'), '0')

    def test_settings_dialog_and_delayed_confirmation_are_separate(self):
        html = '''<body data-revoked="0"><section role="dialog" aria-label="Settings">
            <span data-testid="account">fixture@example.org</span>
            <button onclick="setTimeout(() => document.querySelector('#confirmation').showModal(), 100)">Log out of all devices</button>
            </section><dialog id="confirmation"><h2>Log out of all devices</h2><p>Are you sure?</p>
            <button onclick="document.body.dataset.revoked='1';this.parentElement.close()">Log out of all devices</button>
            <button>Cancel</button></dialog></body>'''
        self.context.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body=html))
        self.page.goto('https://claude.ai/new#settings/account')
        self.assertTrue(self.perform())

    def test_unconfirmed_exit_inside_settings_is_not_clicked_twice(self):
        html = '''<body data-clicks="0" data-revoked="0"><section role="dialog" aria-label="Settings">
            <span data-testid="account">fixture@example.org</span>
            <button onclick="document.body.dataset.clicks=String(Number(document.body.dataset.clicks)+1)">Log out of all devices</button>
            </section></body>'''
        self.context.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body=html))
        self.page.goto('https://claude.ai/new#settings/account')
        with self.assertRaises(LogoutError):
            self.perform()
        self.assertEqual(self.page.locator('body').get_attribute('data-clicks'), '1')

    def test_unknown_language_and_duplicate_actions_fail_closed(self):
        for label, duplicate in [('Unrecognized global exit', False), ('Выйти везде', True)]:
            self.context.unroute('**/*')
            self.html(label, duplicate=duplicate)
            with self.assertRaises(LogoutError):
                self.perform()
            self.assertEqual(self.page.locator('body').get_attribute('data-clicked'), '0')

    def test_confirmation_without_revocation_is_not_success(self):
        self.html('Выйти везде', 'Выйти')
        with self.assertRaisesRegex(LogoutError, 'revocation was not confirmed'):
            self.perform(verify=lambda ms: False)

    def test_unknown_confirmation_is_not_clicked(self):
        self.html('Выйти везде', 'Delete account')
        with self.assertRaisesRegex(LogoutError, 'confirmation was not found'):
            self.perform()
        self.assertEqual(self.page.locator('body').get_attribute('data-revoked'), '0')

    def test_wrong_account_is_rejected_before_click(self):
        self.html('Log out everywhere')
        with self.assertRaisesRegex(LogoutError, 'identity was not confirmed'):
            self.perform(identity=lambda p: False)
        self.assertEqual(self.page.locator('body').get_attribute('data-clicked'), '0')

    def test_direct_logout_without_modal_still_requires_verification(self):
        self.html('Log out everywhere', direct=True)
        self.assertTrue(self.perform())

    def test_russian_global_label_with_separate_logout_button(self):
        html = """<body data-revoked="0"><span data-testid="account">fixture@example.org</span>
            <section role="dialog"><h2>Аккаунт</h2>
            <div style="display:flex;justify-content:space-between"><span>Выйти на всех устройствах</span>
            <button onclick="document.querySelector('#confirm').showModal()">Выйти</button></div>
            <button onclick="document.body.dataset.deleted='1'">Удалить аккаунт</button></section>
            <dialog id="confirm"><button onclick="document.body.dataset.revoked='1';this.parentElement.close()">Выйти</button></dialog>"""
        self.context.route('**/*',lambda r:r.fulfill(status=200,content_type='text/html; charset=utf-8',body=html))
        self.page.goto('https://claude.ai/new#settings/account')
        self.assertTrue(self.perform())
        self.assertIsNone(self.page.locator('body').get_attribute('data-deleted'))

    def test_orphan_global_heading_cannot_select_an_unrelated_local_logout(self):
        html = """<span data-testid="account">fixture@example.org</span><section role="dialog">
            <section><p>Выйти на всех устройствах</p><p>Not currently available</p></section>
            <section><button onclick="document.body.dataset.local='1'">Выйти</button></section></section>"""
        self.context.route('**/*',lambda r:r.fulfill(status=200,content_type='text/html; charset=utf-8',body=html))
        self.page.goto('https://claude.ai/new#settings/account')
        with self.assertRaises(LogoutError):self.perform()
        self.assertIsNone(self.page.locator('body').get_attribute('data-local'))

    def test_confirmation_keeps_original_target_when_duplicate_is_inserted(self):
        self.html('Log out everywhere')
        self.page.evaluate("""() => {
            const button=document.querySelector('dialog button');
            button.id='original-confirm';
            const original=Element.prototype.getBoundingClientRect;
            Element.prototype.getBoundingClientRect=function() {
                const rect=original.call(this);
                if(this.id==='original-confirm' && this.closest('dialog').open && !window.injected) {
                    window.injected=true;
                    queueMicrotask(()=>{
                        const other=document.createElement('button');
                        other.textContent=button.textContent;
                        other.onclick=()=>document.body.dataset.wrong='1';
                        button.before(other);
                    });
                }
                return rect;
            };
        }""")
        self.assertTrue(self.perform())
        self.assertIsNone(self.page.locator('body').get_attribute('data-wrong'))

    def test_direct_navigation_after_global_logout_is_verified(self):
        html = """<span data-testid="account">fixture@example.org</span>
            <button onclick="location.href='/login'">Log out everywhere</button>"""
        def route(r):
            body=html if '/login' not in r.request.url else '<input type="email">'
            r.fulfill(status=200,content_type='text/html',body=body)
        self.context.route('**/*',route)
        self.page.goto('https://claude.ai/new#settings/account')
        self.assertTrue(self.perform(verify=lambda ms:self.page.url=='https://claude.ai/login'))

    def test_wrong_origin_is_rejected(self):
        self.html('Log out everywhere')
        self.page.goto('https://unrelated.example/settings')
        with self.assertRaisesRegex(LogoutError, 'origin'):
            self.perform()


if __name__ == '__main__':
    unittest.main()
