"""Open an isolated persistent browser for the owner's first manual Claude login.

This is setup tooling, not a Claude provider. It neither issues buyer access nor
reads usage or revokes sessions. Browser data stays under Cardinal's storage/.
"""
import argparse
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def profile_path(account_id, root=ROOT):
    if type(account_id) is not int or account_id < 1:
        raise ValueError('Account ID must be positive')
    return Path(root) / 'storage' / 'claude_rental' / 'browser_profiles' / str(account_id) / 'owner'


def open_context(playwright, profile, *, headless=False, executable_path=None):
    profile = Path(profile)
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    options = {'headless': headless, 'viewport': {'width': 1280, 'height': 900}}
    if executable_path:
        options['executable_path'] = executable_path
    return playwright.chromium.launch_persistent_context(str(profile), **options)


def main():
    parser = argparse.ArgumentParser(description='Первый вход владельца в отдельном браузере Claude.')
    parser.add_argument('--account-id', type=int, help='ID аккаунта из меню «Аккаунты» Cardinal')
    args = parser.parse_args()
    try:
        account_id = args.account_id
        if account_id is None:
            account_id = int(input('ID тестового аккаунта из меню Cardinal: ').strip())
        profile = profile_path(account_id)
    except (ValueError, EOFError):
        print('Введите положительный числовой ID аккаунта, например 1.')
        return 2

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print('Сначала запустите tools\\setup_claude_browser.cmd.')
        return 2

    print(f'Открывается отдельный профиль аккаунта #{account_id}.')
    print('Войдите в нужный тестовый аккаунт Claude вручную по его почте и коду.')
    print('Этот инструмент пока не подключает автоматическую выдачу, лимиты и выход всех сессий.')
    print('Cookies останутся на этом компьютере в storage/claude_rental/browser_profiles/.')
    try:
        with sync_playwright() as p:
            context = open_context(p, profile)
            try:
                page = context.pages[0] if context.pages else context.new_page()
                try:
                    page.goto('https://claude.ai/', wait_until='domcontentloaded', timeout=45000)
                except Exception:
                    print('Страница не загрузилась вовремя. Проверьте доступ к Claude в открытом браузере.')
                input('После входа вернитесь сюда и нажмите Enter: браузер закроется, профиль сохранится. ')
            finally:
                context.close()
        print('Профиль сохранён. Откройте его повторно с тем же ID и проверьте, остался ли вход.')
        return 0
    except KeyboardInterrupt:
        print('\nЗапуск прерван. При следующем запуске проверьте состояние входа.')
        return 1
    except Exception:
        # Browser exceptions can contain authentication URLs. Never print them.
        print('Не удалось завершить работу браузера. Закройте другое окно этого профиля и повторите запуск.')
        print('Если браузер не установлен, выполните tools\\setup_claude_browser.cmd.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
