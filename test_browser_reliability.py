"""Dynamic browser regressions: moving DOM, login transitions, real settings layouts."""
import os
import unittest
from unittest.mock import patch
from claude_browser_ui import ACCOUNT_URL, USAGE_URL, read_organization, read_usage
from tools import claude_account_check as check

ORG = '00000000-0000-0000-0000-000000000001'


@unittest.skipUnless(os.environ.get('RUN_BROWSER_TESTS'), 'Requires Chromium')
class BrowserReliabilityTests(unittest.TestCase):
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
        self.context = self.browser.new_context()
        self.html = ''
        self.context.route('**/*',lambda route:route.fulfill(status=200,content_type='text/html; charset=utf-8',body=self.html))
        self.page = self.context.new_page()

    def tearDown(self):
        self.context.close()

    def load(self, html, url):
        self.html = html
        self.page.goto(url)
        self.page.reload()

    def account(self, readonly=False):
        value = f'<input type="text" readonly value="{ORG}">' if readonly else f'<code>{ORG}</code>'
        return '<section role="dialog"><h1>Настройки</h1><nav><button>Аккаунт</button><button>Использование</button></nav><article><h2>Профиль</h2><div>Полное имя<input value="Test"></div><h2>Аккаунт</h2><div><span>Выйти на всех устройствах</span><button>Выйти</button></div><div><span id="moving-label">ID организации</span>' + value + '</div></article></section>'

    def usage(self, first=0, second=100):
        return f'<section role="dialog"><div><span id="moving-label">Текущий сеанс</span><b>{first}% использовано</b></div><div><button>На этой неделе<span style="display:none">На этой неделе</span></button><b>{second}% использовано</b></div></section>'

    def move_after_measurement(self):
        self.page.evaluate('''() => {
            const original = Element.prototype.getBoundingClientRect;
            Element.prototype.getBoundingClientRect = function() {
                const rect = original.call(this);
                if (this.id === 'moving-label' && !window.scheduled) {
                    window.scheduled = true;
                    queueMicrotask(() => {
                        const banner = document.createElement('div');
                        banner.innerHTML = '<span>Dynamic banner</span><button>Dismiss</button>';
                        document.body.prepend(banner);
                    });
                }
                return rect;
            };
        }''')

    def test_russian_organization_survives_dom_insertion_between_browser_calls(self):
        self.load(self.account(),ACCOUNT_URL)
        self.move_after_measurement()
        self.assertEqual(read_organization(self.page),ORG)
        self.assertTrue(self.page.evaluate('window.scheduled'))

    def test_both_limits_are_read_atomically_while_elements_move(self):
        for first,second in ((0,0),(0,100),(100,0),(100,100),(70,70.5)):
            with self.subTest(limits=(first,second)):
                self.load(self.usage(first,second),USAGE_URL)
                self.move_after_measurement()
                self.assertEqual(read_usage(self.page),{'five_hour':first,'seven_day':second})

    def test_organization_readonly_field_is_supported(self):
        self.load(self.account(readonly=True),ACCOUNT_URL)
        self.assertEqual(read_organization(self.page),ORG)

    def test_chat_text_outside_settings_is_not_another_limit(self):
        self.load('<aside><p>Current session</p><b>99%</b><p>This week</p></aside>'+self.usage(0,100),USAGE_URL)
        self.assertEqual(read_usage(self.page),{'five_hour':0,'seven_day':100})

    def test_editable_profile_field_is_not_an_organization_id(self):
        self.load(self.account(readonly=True).replace('readonly ',''),ACCOUNT_URL)
        from claude_browser_ui import SettingsError
        with self.assertRaisesRegex(SettingsError,'missing_row_value'):
            read_organization(self.page)

    def test_organization_value_can_arrive_after_label(self):
        self.load(self.account().replace(ORG,'Loading'),ACCOUNT_URL)
        self.page.evaluate('(value) => setTimeout(() => document.querySelector("code").textContent=value, 250)',ORG)
        self.assertEqual(check.wait_for_read(self.page,lambda:read_organization(self.page),timeout_seconds=2),ORG)

    def test_repeated_renders_never_mix_limit_snapshots(self):
        self.load(self.usage(0,100),USAGE_URL)
        # Use separate label nodes, as in the real UI, while repeatedly replacing rows.
        self.page.evaluate("""() => {
            const original=setInterval;
            window.fixtureInterval=original(() => {
                const first=Math.random()<.5?0:100;
                document.querySelector('[role=dialog]').innerHTML =
                    `<div><span>Current session</span><b>${first}% used</b></div><div><span>This week</span><b>${100-first}% used</b></div>`;
            }, 3);
        }""")
        for _ in range(30):
            result=check.wait_for_read(self.page,lambda:read_usage(self.page),timeout_seconds=1)
            self.assertIn((result['five_hour'],result['seven_day']),((0,100),(100,0)))

    def test_recovery_closes_native_check_browser_and_preserves_profile(self):
        import subprocess, sys, tempfile
        from pathlib import Path
        from tools import claude_manual_login as manual
        original = subprocess.Popen
        children = []
        def launch(args, **kwargs):
            child = original(args[:-1]+['--headless=new','--no-sandbox','about:blank'],**kwargs)
            children.append(child)
            return child
        with tempfile.TemporaryDirectory() as folder:
            profile = Path(folder)
            try:
                with patch.object(check.subprocess,'Popen',side_effect=launch):
                    connection = check.connect_profile(self.p,'/usr/bin/chromium',profile)
                context = connection.contexts[0]
                context.route('**/*',lambda r:r.fulfill(status=200,body='<p>Fixture</p>'))
                page = context.new_page();page.goto('https://claude.ai/')
                page.evaluate('localStorage.setItem("fixture","retained")')
                script = 'from tools.claude_manual_login import close_check_profile; import sys; close_check_profile(sys.argv[1])'
                closed = subprocess.run([sys.executable,'-c',script,str(profile)],cwd=Path(__file__).parent,
                                        text=True,capture_output=True,timeout=20)
                self.assertEqual(closed.returncode,0,closed.stderr)
                self.assertFalse(manual.profile_in_check_mode(profile))
                self.assertTrue(self.browser.is_connected())
                with patch.object(check.subprocess,'Popen',side_effect=launch):
                    reopened = check.connect_profile(self.p,'/usr/bin/chromium',profile)
                reopened.contexts[0].route('**/*',lambda r:r.fulfill(status=200,body='<p>Fixture</p>'))
                page2 = reopened.contexts[0].new_page();page2.goto('https://claude.ai/')
                self.assertEqual(page2.evaluate('localStorage.getItem("fixture")'),'retained')
                reopened.close()
            finally:
                for child in children:
                    if child.poll() is None:
                        child.terminate()
                        try:child.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            child.kill();child.wait(timeout=5)

    def test_magic_link_page_is_classified_as_manual_login(self):
        self.load('<h1>Не удалось выполнить вход</h1>', 'https://claude.ai/magic-link')
        self.assertEqual(check.access_state(self.page),'login_link_failed')


if __name__ == '__main__':
    unittest.main()
