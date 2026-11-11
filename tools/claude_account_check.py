"""Read-only local check of the owner's already prepared Windows Claude profile.

Connect over loopback CDP to the isolated profile, verify its local binding,
read visible usage, and check the global logout button without pressing it.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import time

if __package__:
    from .claude_manual_login import ROOT, BROWSERS, installed_browsers, profile_path
else:
    from claude_manual_login import ROOT, BROWSERS, installed_browsers, profile_path
from claude_browser_ui import (ACCOUNT_URL, USAGE_URL, LABELS, SettingsError, label_pattern,
                               read_organization, read_usage, global_logout_available)


class CheckError(Exception):
    pass


def account_record(root, account_id):
    config_path = Path(os.environ.get('CLAUDE_RENTAL_CONFIG', str(root / 'storage/claude_rental/config.json')))
    if not config_path.is_absolute():
        config_path = root / config_path
    if not config_path.is_file():
        raise CheckError('cardinal_config_missing')
    config = json.loads(config_path.read_text(encoding='utf-8'))
    database = Path(config['database'])
    if not database.is_absolute():
        database = root / database
    if not database.is_file():
        raise CheckError('cardinal_database_missing')
    with sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True) as c:
        row = c.execute('SELECT id,email FROM accounts WHERE id=?', (account_id,)).fetchone()
    if row is None:
        raise CheckError('account_not_found')
    return {'id': row[0], 'email': row[1]}


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix='.check-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write('\n')
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def local_endpoint(profile):
    lines = (profile / 'DevToolsActivePort').read_text(encoding='utf-8').splitlines()
    if (len(lines) != 2 or not lines[0].isdigit() or not 1 <= int(lines[0]) <= 65535
            or not re.fullmatch(r'/devtools/browser/[A-Za-z0-9-]+', lines[1])):
        raise CheckError('invalid_local_endpoint')
    return 'ws://127.0.0.1:' + lines[0] + lines[1]


def connect_profile(playwright, executable, profile):
    if not profile.is_dir():
        raise CheckError('manual_profile_missing')
    subprocess.Popen([str(executable), '--user-data-dir=' + str(profile), '--no-first-run',
                      '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=0',
                      '--new-window', 'https://claude.ai/'],
                     shell=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            return playwright.chromium.connect_over_cdp(local_endpoint(profile), timeout=1200)
        except Exception:
            time.sleep(0.4)
    raise CheckError('close_profile_windows_then_retry')


def inspect_account(context, expected_organization=None):
    page = context.new_page()
    try:
        page.goto(ACCOUNT_URL, wait_until='domcontentloaded', timeout=30000)
        page.get_by_text(label_pattern(LABELS['organization'])).first.wait_for(state='visible', timeout=10000)
        organization = read_organization(page)
        if expected_organization and organization != expected_organization:
            raise CheckError('account_identity_mismatch')
        logout = global_logout_available(page)
        page.goto(USAGE_URL, wait_until='domcontentloaded', timeout=30000)
        page.get_by_text(label_pattern(LABELS['session'])).first.wait_for(state='visible', timeout=10000)
        # A loading row is not a zero. Wait briefly for a complete, unambiguous view.
        deadline = time.monotonic() + 8
        while True:
            try:
                usage = read_usage(page)
                break
            except SettingsError:
                if time.monotonic() >= deadline:
                    raise
                page.wait_for_timeout(300)
        page.goto(ACCOUNT_URL, wait_until='domcontentloaded', timeout=30000)
        page.get_by_text(label_pattern(LABELS['organization'])).first.wait_for(state='visible', timeout=10000)
        if read_organization(page) != organization:
            raise CheckError('account_changed_during_check')
        return organization, {**usage, 'global_logout_button': logout,
                              'logout_tested': False, 'claude_code_revocation_tested': False}
    finally:
        try:
            page.close()
        except Exception:
            pass


ERRORS = {
    'cardinal_config_missing': 'Не найдены настройки Cardinal. Скопируйте tools рядом с main.py и откройте /claude_rental.',
    'cardinal_database_missing': 'Не найдена база аккаунтов Cardinal. Проверьте настройку database.',
    'account_not_found': 'Такого ID нет в базе Cardinal. Посмотрите ID в меню «Аккаунты».',
    'manual_profile_missing': 'Нет сохранённого профиля для этого ID и браузера. Сначала выполните ручной вход.',
    'close_profile_windows_then_retry': 'Подключение не открылось. Закройте все окна тестового профиля и повторите запуск. Политика браузера также может запрещать подключение.',
    'account_identity_mismatch': 'Профиль принадлежит другой организации Claude. Проверка остановлена.',
    'account_changed_during_check': 'Аккаунт изменился во время проверки. Результат отклонён.',
}


def main():
    if sys.platform != 'win32':
        print('Этот инструмент предназначен для Windows.')
        return 2
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print('Сначала запустите tools\\setup_claude_check.cmd.')
        return 2
    root = ROOT
    account_id = None
    try:
        account_id = int(input('ID аккаунта из меню Cardinal: ').strip())
        if account_id < 1:
            raise ValueError()
        account = account_record(root, account_id)
        browsers = installed_browsers()
        choices = list(browsers)
        if not choices:
            raise CheckError('installed_browser_missing')
        for i, name in enumerate(choices, 1):
            print(f'{i}. {BROWSERS[name][0]}')
        number = int(input('Номер браузера, в котором сохранён вход: ').strip())
        if not 1 <= number <= len(choices):
            raise ValueError()
        browser = choices[number - 1]
        profile = profile_path(account_id, browser, root)
        binding_path = root / 'storage/claude_rental/browser_bindings.json'
        bindings = json.loads(binding_path.read_text(encoding='utf-8')) if binding_path.exists() else {}
        binding = bindings.get(str(account_id), {})
        print(f'Проверяется аккаунт #{account_id}: {account["email"]}')
        print('Окна этого тестового профиля нужно закрыть перед запуском проверки.')
        print('Подключение браузера будет доступно только на этом компьютере. После проверки закройте его окна.')
        with sync_playwright() as p:
            connection = connect_profile(p, browsers[browser], profile)
            if len(connection.contexts) != 1:
                raise CheckError('ambiguous_browser_context')
            if not binding:
                print('В открытом окне проверьте почту через меню аккаунта Claude.')
                answer = input('Почта совпадает с указанной выше? Введите ДА для привязки: ').strip().casefold()
                if answer not in ('да', 'yes'):
                    raise CheckError('identity_not_confirmed')
            organization, report = inspect_account(connection.contexts[0], binding.get('organization_id'))
        bindings[str(account_id)] = {'browser': browser, 'organization_id': organization}
        save_json(binding_path, bindings)
        report.update(result='ok', account_id=account_id, checked_at=datetime.now(timezone.utc).isoformat())
        save_json(root / 'storage/claude_rental/browser_check.json', report)
        print(f'Current session / за 5 часов: {report["five_hour"]:g}% использовано.')
        print(f'This week / за неделю: {report["seven_day"]:g}% использовано.')
        print('Кнопка глобального выхода: ' + ('найдена.' if report['global_logout_button'] else 'не найдена.'))
        print('Кнопка НЕ нажималась. Автовыдача не включалась. База аренд не изменялась.')
        print('Можно прислать файл storage/claude_rental/browser_check.json: почты, cookies и кодов в нём нет.')
        print('Закройте окна тестового браузера, чтобы завершить локальное подключение.')
        return 0
    except (KeyboardInterrupt, EOFError):
        print('\nПроверка отменена.')
        return 1
    except Exception as exc:
        code = str(exc) if isinstance(exc, (SettingsError, CheckError)) else 'browser_check_failed'
        print(ERRORS.get(code, 'Не удалось прочитать настройки. Проверьте вход, закройте диалог выхода и при необходимости завершите проверку сайта вручную.'))
        print('Код проверки: ' + code)
        # Never print Playwright exceptions, HTML, URLs or mailbox credentials.
        report = {'result': 'error', 'code': code, 'account_id': account_id}
        try:
            save_json(root / 'storage/claude_rental/browser_check.json', report)
        except OSError:
            pass
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
