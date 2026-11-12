"""Real Chromium tests against local fixtures based on the user's screenshots."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from claude_browser_ui import (ACCOUNT_URL, USAGE_URL, LABELS, SettingsError, read_usage,
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

    def test_weekly_dropdown_with_svg_title_or_hidden_text(self):
        for decoration in ('<svg width="16" height="16"><title>Expand menu</title><path d="M0 0 L8 8"/></svg>',
                           '<span style="display:none">Choose limit</span>'):
            with self.subTest(decoration=decoration):
                self.load(self.usage('12%', '34%').replace('This week</button>',
                                                         'This week' + decoration + '</button>'))
                details = {}
                self.assertEqual(read_usage(self.page, details), {'five_hour': 12, 'seven_day': 34})
                self.assertEqual(details['weekly']['exact_text_matches'], 0)
                self.assertEqual(details['weekly']['rendered_text_matches'], 1)
                self.assertTrue(details['weekly']['percentage_read'])

    def test_weekly_dropdown_rendered_dom_variants(self):
        clipped = 'position:absolute;width:1px;height:1px;margin:-1px;overflow:hidden;clip:rect(0,0,0,0)'
        variants = (
            'This week<span style="display:none">This week</span>',
            f'This week<span style="{clipped}">Open menu</span>',
            f'This week<span style="{clipped}">This week</span>',
            'This week<svg width="16" height="16"><title>This week</title></svg>',
            '<span style="display:inline-flex;gap:4px"><span>This</span><span>week</span></span>',
            'This week<span aria-hidden="true">⌄</span>',
            'This\u200b week',
            '<span style="display:contents">This week</span>',
        )
        for label in variants:
            with self.subTest(label=label):
                self.load(self.usage('15%', '22%').replace('This week</button>', label + '</button>'))
                details = {}
                self.assertEqual(read_usage(self.page, details), {'five_hour': 15, 'seven_day': 22})
                self.assertEqual(details['weekly']['rendered_text_matches'], 1)

    def test_visible_native_selector_reads_only_selected_option(self):
        label = '<select><option>Current session</option><option selected>This week</option></select>'
        self.load(self.usage('15%', '22%').replace('<button>This week</button>', label))
        self.assertEqual(read_usage(self.page), {'five_hour': 15, 'seven_day': 22})

    def test_screenshot_layout_reads_15_and_22_with_extra_usage_rows(self):
        weekly = '<div><button><span>This week<span style="display:none">This week</span></span><svg width="16" height="16"><title>Expand</title></svg></button></div>'
        html = self.usage('15%', '22%').replace('<button>This week</button>', weekly)
        html += '<h2>Limit resets</h2><p>No resets right now</p><section>Other model <span>55%</span></section><section>Extra usage <span>0%</span></section>'
        self.load(html)
        self.assertEqual(read_usage(self.page), {'five_hour': 15, 'seven_day': 22})

    def test_screen_reader_percentage_is_not_a_second_visible_value(self):
        hidden = '<span style="position:absolute;clip-path:inset(50%);width:1px;height:1px;overflow:hidden">22%</span>'
        self.load(self.usage('15%', '22%').replace('22% used', '22% used' + hidden))
        self.assertEqual(read_usage(self.page), {'five_hour': 15, 'seven_day': 22})

    def test_hidden_only_weekly_label_and_visible_qualifiers_are_rejected(self):
        variants = (
            '<span style="position:absolute;clip:rect(0,0,0,0);width:1px;height:1px">This week</span>',
            '<span aria-hidden="true">This week remaining</span>',
            '<span style="opacity:0">This week</span>',
            '<select aria-hidden="true" style="width:1px;height:1px;clip-path:inset(50%)"><option>This week</option></select>',
        )
        for label in variants:
            with self.subTest(label=label):
                self.load(self.usage('15%', '22%').replace('This week</button>', label + '</button>'))
                with self.assertRaisesRegex(SettingsError, '^weekly_label_missing$'):
                    read_usage(self.page)

    def test_missing_session_percentage_cannot_borrow_weekly_value(self):
        self.load(self.usage('15%', '22%').replace('15% used', 'Loading'))
        with self.assertRaisesRegex(SettingsError, 'ambiguous_row|missing_row_value'):
            read_usage(self.page)

    def test_weekly_label_must_be_visible_exact_and_unique(self):
        for replacement in ('This week remaining', 'This week 100% available',
                            '<svg width="16" height="16"><title>This week</title></svg>'):
            with self.subTest(replacement=replacement):
                self.load(self.usage().replace('This week</button>', replacement + '</button>'))
                with self.assertRaisesRegex(SettingsError, '^weekly_label_missing$'):
                    read_usage(self.page)
        self.load(self.usage() + '<section><button>This week</button><span>0% used</span></section>')
        with self.assertRaisesRegex(SettingsError, '^weekly_label_ambiguous$'):
            read_usage(self.page)

    def test_rendered_label_normalizes_line_breaks_and_nonbreaking_spaces(self):
        for label in ('This&nbsp;week', 'This <span style="display:block">week</span>'):
            self.load(self.usage('5%', '6%').replace('This week</button>', label + '</button>'))
            self.assertEqual(read_usage(self.page), {'five_hour': 5, 'seven_day': 6})

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

    def tab_navigation_fixture(self, labels=('Account', 'Usage')):
        import json
        # A settings app can read the fragment on startup, then use internal
        # state for its tabs. Changing the fragment alone does not render it.
        account = self.account().replace('<section role="dialog">', '<section>')
        usage = self.usage('12%', '34%').replace('role="dialog"', '')
        return '<body><section role="dialog"><nav>' + ''.join(
            f'<button onclick="showTab(\'{section}\')">{label}</button>'
            for section, label in zip(('account', 'usage'), labels)
        ) + '</nav><article id="content"></article></section><script>const views=' + json.dumps(
            {'account': account, 'usage': usage}
        ) + ''';window.showTab = section => {
            history.replaceState(null, '', '#settings/' + section);
            document.querySelector('#content').innerHTML = views[section];
        };showTab(location.hash.endsWith('/usage') ? 'usage' : 'account');</script>'''

    def test_tab_navigation_when_hash_changes_do_not_update_settings(self):
        self.html = self.tab_navigation_fixture()
        # Establish that direct fragment navigation reproduces the failure.
        self.page.goto(ACCOUNT_URL)
        self.page.goto(USAGE_URL)
        self.assertTrue(self.page.get_by_text('Organization ID', exact=True).is_visible())
        self.assertEqual(self.page.get_by_text('Current session', exact=True).count(), 0)
        for labels in (('Account', 'Usage'), ('Аккаунт', 'Использование')):
            with self.subTest(labels=labels):
                self.html = self.tab_navigation_fixture(labels)
                navigation = []
                org, report = inspect_account(self.context, ORG, navigation=navigation)
                self.assertEqual(org, ORG)
                self.assertEqual((report['five_hour'], report['seven_day']), (12, 34))
                self.assertFalse(report['logout_tested'])
                self.assertEqual([r['method'] for r in navigation], ['direct', 'settings_tab', 'settings_tab'])
                self.assertEqual([r['target'] for r in navigation], ['account', 'usage', 'account'])
                self.assertTrue(all(r['ready'] for r in navigation))
                self.assertEqual(navigation[1]['visible_labels']['session'], 1)
                import json
                self.assertNotIn(ORG, json.dumps(navigation))
                self.assertNotIn('https:', json.dumps(navigation))

    def test_entire_check_reads_decorated_weekly_label_and_returns_to_account(self):
        self.html = self.tab_navigation_fixture().replace('This week</button>',
            'This week<svg><title>Toggle menu</title></svg></button>')
        details, navigation = {}, []
        org, result = inspect_account(self.context, ORG, navigation=navigation, usage_diagnostics=details)
        self.assertEqual((org, result['seven_day']), (ORG, 34))
        self.assertEqual(details['weekly']['rendered_text_matches'], 1)
        self.assertEqual(navigation[-1]['target'], 'account')
        self.assertTrue(navigation[-1]['ready'])
        self.assertEqual(self.context.pages, [self.page])

    def test_failed_usage_check_keeps_page_and_reports_missing_metric(self):
        self.html = self.tab_navigation_fixture().replace('This week', 'Unknown heading')
        details = {}
        with self.assertRaisesRegex(SettingsError, '^weekly_label_missing$'):
            inspect_account(self.context, ORG, usage_diagnostics=details)
        self.assertEqual(details['weekly']['rendered_text_matches'], 0)
        self.assertTrue(details['session']['percentage_read'])
        self.assertEqual(len(self.context.pages), 2)
        self.assertEqual(self.context.pages[-1].url, USAGE_URL)

    def test_full_reload_fallback_when_settings_tabs_cannot_be_found(self):
        self.html = self.tab_navigation_fixture(('Unknown account label', 'Unknown usage label'))
        navigation = []
        org, report = inspect_account(self.context, ORG, navigation=navigation)
        self.assertEqual(org, ORG)
        self.assertEqual(report['seven_day'], 34)
        self.assertEqual([r['method'] for r in navigation], ['direct', 'full_reload', 'full_reload'])

    def test_ambiguous_tab_or_additional_dialog_stops_without_clicking(self):
        for extra in ('<button>Usage</button>',
                      '<section role="alertdialog"><button>Log out of all devices</button></section>'):
            self.load(self.tab_navigation_fixture().replace('</nav>', extra + '</nav>'), ACCOUNT_URL)
            with self.assertRaisesRegex(SettingsError, '^ambiguous_settings_navigation$'):
                check.visit_settings(self.page, USAGE_URL, LABELS['session'])
            self.assertEqual(self.page.url, ACCOUNT_URL)
            self.assertIsNone(self.page.locator('body').get_attribute('data-clicked'))

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
                progress = unittest.mock.Mock()
                with self.assertRaisesRegex(CheckError, '^' + code + '$'):
                    inspect_account(self.context, progress=progress)
                progress.assert_called_once_with('reading_account')

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
