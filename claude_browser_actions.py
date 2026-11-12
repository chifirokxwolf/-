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


LOCAL_LOGOUT_LABELS = ('Выйти', 'Log out', 'Sign out', 'Вийти', 'Abmelden',
                       'Se déconnecter', 'Cerrar sesión', 'Sair', '退出', '登出', 'ログアウト', '로그아웃')


def global_logout_target(page):
    """Resolve a global button or the local button in its explicit, isolated row.

    Return a pinned DOM element, not a positional locator. A detached element
    fails rather than clicking the replacement at the same index.
    """
    from claude_browser_ui import _RENDERER
    handle = page.evaluate_handle(r'''(labels) => {
        const r = (''' + _RENDERER + r''')();
        const norm = s => r.normalize(s).replace(/(?:…|\.\.\.)$/, '').trim().toLowerCase();
        const global = new Set(labels.global.map(norm)), local = new Set(labels.local.map(norm));
        const dialogs = [...document.querySelectorAll('dialog,[role="dialog"],[role="alertdialog"]')].filter(r.visible);
        if (dialogs.length > 1) return {error:true};
        const root = dialogs[0] || document.body;
        const elements = [...root.querySelectorAll('*')];
        const buttons = elements.filter(el => el.matches('button,[role="button"]') && r.visible(el) && !el.disabled);
        const candidates = new Set(buttons.filter(el => global.has(norm(r.text(el) || el.getAttribute('aria-label') || ''))));
        const matches = elements.filter(el => r.visible(el) && global.has(norm(r.text(el) || el.getAttribute('aria-label') || '')));
        const labelsOnly = matches.filter(el => !el.closest('button,[role="button"]') &&
            !matches.some(other => other !== el && el.contains(other)));
        for (const label of labelsOnly) {
            for (let row = label.parentElement; row && row !== root; row = row.parentElement) {
                const contained = buttons.filter(el => row.contains(el));
                if (!contained.length) continue;
                // A row must contain exactly this label and one logout button.
                // Never pair an orphan heading with a logout elsewhere in Settings.
                if (contained.length === 1 && local.has(norm(r.text(contained[0])))) {
                    const target = contained[0], a = label.getBoundingClientRect(), b = target.getBoundingClientRect();
                    const rest = r.normalize(r.text(row)).replace(r.normalize(r.text(label)), '')
                        .replace(r.normalize(r.text(target)), '').trim();
                    if (!rest && a.top < b.bottom && b.top < a.bottom) candidates.add(target);
                }
                break;
            }
        }
        if (candidates.size > 1) return {error:true};
        return candidates.size ? [...candidates][0] : null;
    }''', {'global': [label for group in GLOBAL_LABELS.values() for label in group],
           'local': list(LOCAL_LOGOUT_LABELS)})
    element = handle.as_element()
    if element is not None:
        return element
    try:
        if handle.json_value() is not None:
            raise LogoutError('A unique global logout button was not found; account remains held')
        return None
    finally:
        handle.dispose()


def confirmation_target(page, previous, labels):
    from claude_browser_ui import _RENDERER
    handle = page.evaluate_handle(r'''(args) => {
        const r = (''' + _RENDERER + r''')();
        const norm = s => r.normalize(s).replace(/(?:…|\.\.\.)$/, '').trim().toLowerCase();
        const dialogs = [...document.querySelectorAll('dialog,[role="dialog"],[role="alertdialog"]')]
            .filter(el => r.visible(el) && !args.previous.includes(el));
        if (!dialogs.length) return null;
        if (dialogs.length > 1) return {error:'multiple_dialogs'};
        const names = new Set(args.labels.map(norm));
        const buttons = [...dialogs[0].querySelectorAll('button,[role="button"]')]
            .filter(el => r.visible(el) && !el.disabled && names.has(norm(r.text(el) || el.getAttribute('aria-label') || '')));
        return buttons.length === 1 ? buttons[0] : {error:'ambiguous_confirmation'};
    }''', {'previous': previous, 'labels': list(labels)})
    element = handle.as_element()
    if element is not None:
        return element
    try:
        value = handle.json_value()
        if value is not None:
            if value.get('error') == 'multiple_dialogs':
                raise LogoutError('Multiple confirmation dialogs; account remains held')
            raise LogoutError('A unique logout confirmation was not found; account remains held')
        return None
    finally:
        handle.dispose()


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
    target = None
    confirm = None
    existing_dialogs = []
    try:
        url = urlsplit(page.url)
        if url.scheme != 'https' or url.hostname != 'claude.ai' or url.port not in (None, 443) or url.username or url.password:
            raise LogoutError('Unexpected settings origin')
        if not callable(verify_account) or not callable(verify_revoked) or verify_account(page) is not True:
            raise LogoutError('Account identity was not confirmed')
        if not 100 <= timeout_ms <= 60000:
            raise LogoutError('Invalid browser timeout')
        existing_dialogs = page.locator('dialog:visible,[role="dialog"]:visible,[role="alertdialog"]:visible').element_handles()
        target = global_logout_target(page)
        if target is None:
            raise LogoutError('A unique global logout button was not found; account remains held')
        before_url = page.url
        target.click(timeout=timeout_ms)
        # A direct logout may have no modal. If a modal appears, scope the confirm
        # search to it so a local logout elsewhere on the page can never be clicked.
        try:
            page.wait_for_function('''previous => [...document.querySelectorAll('dialog,[role="dialog"],[role="alertdialog"]')]
                .some(el => !previous.includes(el) && el.getClientRects().length &&
                    getComputedStyle(el).visibility !== 'hidden')''', arg=existing_dialogs,
                timeout=min(1500, timeout_ms))
        except Exception as exc:
            if type(exc).__name__ != 'TimeoutError' and page.url == before_url:
                raise
        # Resolve and pin the new confirmation in one browser task as well.
        if page.url == before_url:
            confirm = confirmation_target(page, existing_dialogs, labels + CONFIRM_LABELS)
            if confirm is not None:
                confirm.click(timeout=timeout_ms)
        if verify_revoked(timeout_ms) is not True:
            raise LogoutError('Logout was clicked but revocation was not confirmed; account remains held')
        return True
    except LogoutError:
        raise
    except Exception as exc:
        # Only the fixed exception class, never URLs, HTML or session values.
        raise LogoutError('Browser action failed (' + type(exc).__name__ + '); account remains held') from None
    finally:
        for handle in [target, confirm, *existing_dialogs]:
            if handle is not None:
                try:
                    handle.dispose()
                except Exception:
                    pass
