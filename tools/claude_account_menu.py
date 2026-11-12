"""Single entry point for manual sign-in and the read-only account check."""
if __package__:
    from . import claude_manual_login as manual
    from . import claude_account_check as check
else:
    import claude_manual_login as manual
    import claude_account_check as check


def main():
    while True:
        print('\nClaude Account 0.4.2')
        print('1. Войти в аккаунт вручную / восстановить ручной режим')
        print('2. Проверить сохранённый вход и лимиты')
        print('0. Выход')
        try:
            choice = input('Выберите действие: ').strip()
        except (EOFError, KeyboardInterrupt):
            return 0
        if choice == '0':
            return 0
        if choice == '1':
            manual.main([])
        elif choice == '2':
            print('Сначала завершите ручной вход и закройте окна этого тестового профиля.')
            check.main()
        else:
            print('Введите 1, 2 или 0.')


if __name__ == '__main__':
    raise SystemExit(main())
