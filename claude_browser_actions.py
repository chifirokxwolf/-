# noplug
"""Locale-aware browser actions for a future validated Claude adapter.

This module neither logs in nor claims that UI clicks revoke Claude Code tokens.
The caller supplies identity and remote revocation checks on its own browser worker.
"""
import re
from urllib.parse import urlsplit


class LogoutError(Exception):
    pass


GLOBAL_LABELS = {
    'ru': ('Выйти везде', 'Выйти на всех устройствах', 'Выйти из всех сеансов', 'Завершить все сеансы'),
    'en': ('Log out everywhere', 'Log out of all devices', 'Log out all devices',
           'Log out of all sessions', 'Sign out everywhere', 'Sign out of all devices'),
    'uk': ('Вийти скрізь', 'Вийти на всіх пристроях', 'Завершити всі сеанси'),
    'de': ('Überall abmelden', 'Von allen Geräten abmelden', 'Auf allen Geräten abmelden'),
    'fr': ('Se déconnecter de tous les appareils', 'Déconnexion de tous les appareils'),
    'es': ('Cerrar sesión en todos los dispositivos', 'Cerrar todas las sesiones'),
    'pt': ('Sair de todos os dispositivos', 'Encerrar todas as sessões'),
    'zh': ('退出所有设备', '登出所有裝置', '登出所有设备'),
    'ja': ('すべてのデバイスからログアウト', 'すべてのデバイスでログアウト'),
    'ko': ('모든 기기에서 로그아웃', '모든 장치에서 로그아웃'),
}
CONFIRM_LABELS = ('Выйти', 'Подтвердить', 'Завершить сеансы', 'Log out', 'Sign out', 'Confirm',
                  'Вийти', 'Підтвердити', 'Abmelden', 'Bestätigen', 'Se déconnecter', 'Confirmer',
                  'Cerrar sesión', 'Confirmar', 'Sair', '退出', '登出', '确认', '確認',
                  'ログアウト', '로그아웃', '확인')


def _pattern(labels):
    return re.compile(r'^\s*(?:' + '|'.join(re.escape(x) for x in labels) + r')\s*(?:…|\.\.\.)?\s*$', re.I)


def _visible(locator):
    return [locator.nth(i) for i in range(locator.count()) if locator.nth(i).is_visible()]


def logout_everywhere(page, *, verify_account, verify_revoked, timeout_ms=10000):
    """Click the global logout action and confirm it in a visible modal, if any.

    page: authenticated Playwright Page already displaying the real account settings.
    verify_account(page) must confirm the expected account, without guessing.
    verify_revoked(timeout_ms) must test old independent sessions/tokens, including
    Code if sold, and the previous login link. Return True only after these checks.
    Unknown labels, ambiguous buttons and failed checks raise LogoutError. The rental
    must remain in cleanup; the caller must not catch this and return True.
    Playwright is optional: this module is imported without launching a browser.
    """
    labels = tuple(x for values in GLOBAL_LABELS.values() for x in values)
    try:
        url = urlsplit(page.url)
        if url.scheme != 'https' or url.hostname != 'claude.ai' or url.port not in (None, 443) or url.username:
            raise LogoutError('Unexpected settings origin')
        if not callable(verify_account) or not callable(verify_revoked) or verify_account(page) is not True:
            raise LogoutError('Account identity was not confirmed')
        if not 100 <= timeout_ms <= 60000:
            raise LogoutError('Invalid browser timeout')
        dialogs = page.get_by_role('dialog').or_(page.get_by_role('alertdialog'))
        existing_dialogs = [d.element_handle() for d in _visible(dialogs)]
        buttons = _visible(page.get_by_role('button', name=_pattern(labels)))
        if len(buttons) != 1:
            raise LogoutError('A unique global logout button was not found; account remains held')
        buttons[0].click(timeout=timeout_ms)
        # A direct logout may have no modal. If a modal appears, scope the confirm
        # search to it so a local logout elsewhere on the page can never be clicked.
        try:
            page.wait_for_function('''previous => [...document.querySelectorAll('dialog,[role="dialog"],[role="alertdialog"]')]
                .some(el => !previous.includes(el) && el.getClientRects().length &&
                    getComputedStyle(el).visibility !== 'hidden')''', arg=existing_dialogs,
                timeout=min(1500, timeout_ms))
        except Exception as exc:
            if type(exc).__name__ != 'TimeoutError':
                raise
        # The screenshot's Settings window is itself a dialog. Only a newly
        # opened confirmation may receive the second click.
        visible = [d for d in _visible(dialogs)
                   if d.evaluate('(el, previous) => !previous.includes(el)', existing_dialogs)]
        if len(visible) > 1:
            raise LogoutError('Multiple confirmation dialogs; account remains held')
        if visible:
            confirm = _visible(visible[0].get_by_role('button', name=_pattern(labels + CONFIRM_LABELS)))
            if len(confirm) != 1:
                raise LogoutError('A unique logout confirmation was not found; account remains held')
            confirm[0].click(timeout=timeout_ms)
        if verify_revoked(timeout_ms) is not True:
            raise LogoutError('Logout was clicked but revocation was not confirmed; account remains held')
        return True
    except LogoutError:
        raise
    except Exception:
        # Browser errors can contain URLs, HTML, account data and session tokens.
        raise LogoutError('Browser action failed; account remains held') from None
