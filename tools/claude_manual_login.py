"""Manual Claude login in an installed Windows browser with a separate profile.

No browser automation, remote debugging, cookie export or CAPTCHA solving.
This prepares a local profile; it does not connect the rental provider.
"""
import argparse
import http.client
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parent.parent
BROWSERS = {
    'chrome': ('Google Chrome', 'chrome.exe', Path('Google/Chrome/Application/chrome.exe')),
    'edge': ('Microsoft Edge', 'msedge.exe', Path('Microsoft/Edge/Application/msedge.exe')),
}


class ProfileInCheckMode(Exception):
    pass


def profile_in_check_mode(profile):
    """Identify a still-running debug instance of this isolated profile only.

    Closing Edge windows can leave its process alive. A second launch would
    reuse that process and ignore new command-line arguments. Never describe
    that reused instance as a fresh manual browser.
    """
    connection = None
    try:
        lines = (Path(profile) / 'DevToolsActivePort').read_text(encoding='utf-8').splitlines()
        if (len(lines) != 2 or not lines[0].isdigit() or not 1 <= int(lines[0]) <= 65535
                or not re.fullmatch(r'/devtools/browser/[A-Za-z0-9-]+', lines[1])):
            return False
        connection = http.client.HTTPConnection('127.0.0.1', int(lines[0]), timeout=0.8)
        connection.request('GET', '/json/version')
        response = connection.getresponse()
        if response.status != 200:
            return False
        metadata = json.loads(response.read(8193))
        endpoint = urlsplit(metadata.get('webSocketDebuggerUrl', ''))
        return (endpoint.scheme == 'ws' and endpoint.hostname in ('127.0.0.1', 'localhost')
                and endpoint.port == int(lines[0]) and endpoint.path == lines[1])
    except (OSError, ValueError, TypeError, AttributeError, http.client.HTTPException):
        return False
    finally:
        if connection:
            connection.close()


def registered_path(filename):
    if sys.platform != 'win32':
        return None
    import winreg
    key = 'Software\\Microsoft\\Windows\\CurrentVersion\\App Paths\\' + filename
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive, key, 0, winreg.KEY_READ | view) as handle:
                    raw, _ = winreg.QueryValueEx(handle, '')
                    path = Path(os.path.expandvars(raw.strip().strip('"')))
                    if path.is_file() and path.name.lower() == filename:
                        return path
            except OSError:
                continue
    return None


def installed_browsers():
    found = {}
    for name, (_, filename, relative) in BROWSERS.items():
        for variable in ('LOCALAPPDATA', 'ProgramFiles', 'ProgramFiles(x86)'):
            base = os.environ.get(variable)
            if base and (candidate := Path(base) / relative).is_file():
                found[name] = candidate.resolve()
                break
        if name not in found:
            path = registered_path(filename)
            if path:
                found[name] = path.resolve()
    return found


def profile_path(account_id, browser, root=ROOT):
    if type(account_id) is not int or account_id < 1 or browser not in BROWSERS:
        raise ValueError('Invalid account or browser')
    # Do not reuse personal browser data or the old Playwright Chromium profile.
    return (Path(root) / 'storage' / 'claude_rental' / 'browser_profiles' /
            str(account_id) / (browser + '-manual')).resolve()


def launch_browser(executable, account_id, browser, root=ROOT):
    profile = profile_path(account_id, browser, root)
    if profile_in_check_mode(profile):
        raise ProfileInCheckMode()
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    return subprocess.Popen([str(executable), '--user-data-dir=' + str(profile),
                             '--no-first-run', '--new-window', 'https://claude.ai/'],
                            shell=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Ручной вход в Claude через обычный Chrome или Edge.')
    parser.add_argument('--account-id', type=int)
    parser.add_argument('--browser', choices=BROWSERS)
    args = parser.parse_args(argv)
    if sys.platform != 'win32':
        print('Этот инструмент предназначен для Windows.')
        return 2
    browsers = installed_browsers()
    if not browsers:
        print('Установленный Chrome или Edge не найден. Установите или обновите браузер с официального сайта.')
        return 2
    try:
        account_id = args.account_id
        if account_id is None:
            account_id = int(input('ID тестового аккаунта из меню Cardinal, например 1: ').strip())
        browser = args.browser
        if browser is None:
            choices = list(browsers)
            for i, name in enumerate(choices, 1):
                print(f'{i}. {BROWSERS[name][0]}')
            selection = input('Выберите браузер, в котором Claude у вас открывается [1]: ').strip() or '1'
            if not selection.isdigit() or not 1 <= int(selection) <= len(choices):
                raise ValueError('Invalid browser choice')
            browser = choices[int(selection) - 1]
        profile_path(account_id, browser)  # Validate before launching anything.
        if browser not in browsers:
            print('Выбранный браузер не установлен.')
            return 2
        launch_browser(browsers[browser], account_id, browser)
    except ProfileInCheckMode:
        print('Этот профиль ещё открыт в режиме check, возможно в фоне.')
        print('Полностью завершите именно тестовый браузер и повторите запуск manual.')
        print('База и сохранённый профиль не удаляются. Код проверки: profile_still_in_check_mode.')
        return 2
    except (ValueError, EOFError):
        print('Неверный ввод: нужен положительный ID аккаунта и номер браузера из списка.')
        return 2
    except KeyboardInterrupt:
        print('\nЗапуск отменён.')
        return 1
    except OSError:
        print('Не удалось открыть браузер. Проверьте доступ к папке Cardinal и установку браузера.')
        return 1
    print(f'Запущен {BROWSERS[browser][0]} для аккаунта #{account_id}.')
    print('Войдите в Claude вручную. Это отдельный профиль: личные вкладки и cookies не используются.')
    print('После входа закройте окно браузера крестиком, затем повторите запуск с тем же ID и браузером.')
    print('Профиль сохраняется на этом компьютере. Автовыдача, лимиты и авто-выход пока не подключены.')
    print('При повторной проверке Cloudflare используйте ссылку устранения неполадок на самой странице.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
