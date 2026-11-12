# noplug
"""Read visible Claude settings without private API endpoints or inferred usage.

English labels/routes are from the user's screenshots. Other labels are
explicit candidates: unknown, missing or ambiguous layouts fail closed.
"""
import re
from urllib.parse import urlsplit

from claude_browser_actions import GLOBAL_LABELS


ACCOUNT_URL = 'https://claude.ai/new#settings/account'
USAGE_URL = 'https://claude.ai/new#settings/usage'
NAV_LABELS = {
    'account': ('Account', 'Аккаунт', 'Учётная запись', 'Учетная запись', 'Обліковий запис',
                'Konto', 'Compte', 'Cuenta', 'Conta', '账户', '帳戶', 'アカウント', '계정'),
    'usage': ('Usage', 'Использование', 'Использование ресурсов', 'Використання',
              'Nutzung', 'Utilisation', 'Uso', '使用情况', '使用量', '用量', '사용량'),
}
LABELS = {
    'session': ('Current session', 'Текущий сеанс', 'Текущая сессия', 'Поточний сеанс',
                'Aktuelle Sitzung', 'Session actuelle', 'Sesión actual', 'Sessão atual',
                '当前会话', '目前工作階段', '現在のセッション', '현재 세션'),
    'weekly': ('This week', 'Weekly limits', 'На этой неделе', 'Эта неделя', 'Недельные лимиты',
               'Цього тижня', 'Diese Woche', 'Cette semaine', 'Esta semana',
               '本周', '本週', '今週', '이번 주'),
    'organization': ('Organization ID', 'ID организации', 'Идентификатор организации',
                     'Ідентифікатор організації', 'Organisations-ID', "ID de l’organisation",
                     "ID de l'organisation", 'ID de organización', 'ID da organização',
                     '组织 ID', '組織 ID', '組織ID', '조직 ID'),
}
PERCENT = re.compile(r'(?<![\w.])(-?\d+(?:[.,]\d+)?)\s*[%％]')
ORG_ID = re.compile(r'\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b', re.I)
REMAINING = re.compile(r'available|remaining|left|доступн|остал|осталось|verfügbar|restant|disponib|剩余|剩餘|残り|남은', re.I)


class SettingsError(Exception):
    """Fixed error codes only; no account data, HTML or authentication URLs."""


def require_page(page, section):
    u = urlsplit(page.url)
    if (u.scheme != 'https' or u.hostname != 'claude.ai' or u.port not in (None, 443)
            or u.username or u.password or u.fragment != 'settings/' + section):
        raise SettingsError('wrong_settings_page')


def label_pattern(labels, exact=True):
    alternatives = r'(?:' + '|'.join(r'\s+'.join(re.escape(part) for part in label.split())
                                     for label in labels) + r')'
    return re.compile(r'^\s*' + alternatives + r'\s*$' if exact else alternatives, re.I)


# Use rendered DOM text rather than Playwright's text selector: its deepest-match
# rule can discard a visible button when a hidden child repeats the same label.
# Clipped accessibility text and SVG titles are not screen-visible labels/values.
READER_VERSION = '0.4.2'
_RENDERER = r'''() => {
    const styles = new WeakMap(), visibility = new WeakMap(), texts = new WeakMap();
    const style = el => {
        if (!styles.has(el)) styles.set(el, getComputedStyle(el));
        return styles.get(el);
    };
    const normalize = text => text.replace(/[\u200b-\u200d\u2060\ufeff\u00ad]/g, '')
        .replace(/\s+/g, ' ').trim();
    function visible(el) {
        if (!el || el.nodeType !== 1) return false;
        if (visibility.has(el)) return visibility.get(el);
        const s = style(el), rect = el.getBoundingClientRect();
        const clip = s.clip.match(/-?\d+(?:\.\d+)?/g);
        const clipped = s.clip !== 'auto' && clip && clip.length === 4 &&
            (+clip[2] <= +clip[0] || +clip[1] <= +clip[3]);
        const clippedPath = /^inset\(\s*50%(?:\s+50%){0,3}\s*\)$/.test(s.clipPath);
        const tinyClipped = rect.width <= 1 && rect.height <= 1 &&
            (s.overflow === 'hidden' || s.clipPath !== 'none');
        const own = !['SCRIPT', 'STYLE', 'NOSCRIPT', 'SVG', 'TITLE', 'OPTION'].includes(el.tagName.toUpperCase()) &&
            s.display !== 'none' && s.visibility !== 'hidden' && s.visibility !== 'collapse' &&
            Number(s.opacity) !== 0 && !clipped && !clippedPath && !tinyClipped &&
            (el.getClientRects().length > 0 || s.display === 'contents');
        const result = own && (!el.parentElement || visible(el.parentElement));
        visibility.set(el, result);
        return result;
    }
    function text(el) {
        if (texts.has(el)) return texts.get(el);
        if (!visible(el)) return '';
        const s = style(el);
        let value;
        if (el.tagName === 'INPUT' && (el.readOnly || el.disabled) && ['text', ''].includes(el.type)) {
            value = el.value;
        } else if (el.tagName === 'SELECT') {
            value = el.selectedOptions.length === 1 ? el.selectedOptions[0].textContent : '';
        } else if (el.getAttribute('aria-hidden') === 'true' &&
                   /^[\s⌄⌃▾▴▼▲∨∧›‹»«]+$/.test(el.textContent)) {
            value = ''; // Decorative arrow, not arbitrary aria-hidden words.
        } else {
            const parts = [...el.childNodes].map(node => {
                if (node.nodeType === 3) return node.textContent;
                if (node.nodeType !== 1) return '';
                if (node.tagName === 'BR') return ' ';
                const value = text(node), display = style(node).display;
                return ['block', 'flex', 'grid', 'list-item'].includes(display) ? ' ' + value + ' ' : value;
            });
            const separated = ['flex', 'inline-flex', 'grid', 'inline-grid'].includes(s.display) && parseFloat(s.columnGap) > 0;
            value = parts.join(separated ? ' ' : '');
        }
        texts.set(el, value);
        return value;
    }
    return {text, visible, normalize};
}'''


# All matching and value extraction runs in ONE browser task. Returning an index
# and resolving nth(index) in a later task is unsafe on a reactive page.
_SNAPSHOT = r'''(options) => {
    const r = (''' + _RENDERER + r''')();
    const url = new URL(location.href);
    if (url.origin !== 'https://claude.ai' || url.username || url.password ||
        (options.section && url.hash !== '#settings/' + options.section))
        return {error:'wrong_settings_page', details:{}, counts:{}};
    const dialogs = [...document.querySelectorAll('dialog,[role="dialog"],[role="alertdialog"]')].filter(r.visible);
    if (dialogs.length > 1) return {error:'ambiguous_settings_dialog', details:{}, counts:{}};
    const root = dialogs[0] || document.body;
    if (!root) return {error:'settings_not_ready', details:{}, counts:{}};
    const elements = [root, ...root.querySelectorAll('*')];
    const found = {}, details = {}, counts = {};
    for (const [kind, labels] of Object.entries(options.labels)) {
        const expected = new Set(labels.map(s => r.normalize(s).toLowerCase()));
        const matches = elements.filter(el => r.visible(el) && expected.has(r.normalize(r.text(el)).toLowerCase()));
        found[kind] = matches.filter(el => !matches.some(other => other !== el && el.contains(other)));
        counts[kind] = found[kind].length;
        // Counts only: do not export DOM, account IDs, email or login links in diagnostics.
        details[kind] = {
            reader_version: options.version, rendered_text_matches: counts[kind],
            exact_text_matches: elements.filter(el => r.visible(el) && expected.has(r.normalize(el.textContent).toLowerCase())).length,
            partial_text_matches: elements.filter(el => r.visible(el) && [...expected].some(s => r.normalize(el.textContent).toLowerCase().includes(s))).length
        };
    }
    const result = {values:{}, counts, details, error:null};
    for (const kind of options.read) {
        if (counts[kind] !== 1) {
            result.error = kind + (counts[kind] ? '_label_ambiguous' : '_label_missing');
            return result;
        }
        const label = found[kind][0], labelText = r.normalize(r.text(label));
        let value = null, row = label;
        for (let depth = 0; row && root.contains(row) && depth < 16; depth++, row = row.parentElement) {
            const text = r.normalize(r.text(row));
            const otherKind = kind === 'session' ? 'weekly' : kind === 'weekly' ? 'session' : null;
            if (otherKind && found[otherKind].some(el => row.contains(el))) {
                result.error = 'ambiguous_row'; return result;
            }
            const pattern = kind === 'organization' ? /\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b/gi : /(?<![\w.])(-?\d+(?:[.,]\d+)?)\s*[%％]/g;
            const values = [...text.replace(labelText, '\n').matchAll(pattern)];
            if (values.length > 1) { result.error = 'ambiguous_row'; return result; }
            if (values.length === 1) {
                if (kind === 'organization') value = values[0][0].toLowerCase();
                else {
                    if (/available|remaining|left|доступн|остал|осталось|verfügbar|restant|disponib|剩余|剩餘|残り|남은/i.test(text)) {
                        result.error = 'remaining_is_not_used'; return result;
                    }
                    value = Number(values[0][1].replace(',', '.'));
                    if (!Number.isFinite(value) || value < 0 || value > 100) {
                        result.error = 'invalid_percentage'; return result;
                    }
                    details[kind].percentage_read = true;
                }
                break;
            }
        }
        if (value === null) { result.error = 'missing_row_value'; return result; }
        result.values[kind] = value;
    }
    return result;
}'''


def settings_snapshot(page, *, section=None, read=(), diagnostics=None):
    result = page.evaluate(_SNAPSHOT, {'section': section, 'read': list(read),
                                     'labels': LABELS, 'version': READER_VERSION})
    if diagnostics is not None:
        diagnostics.clear()
        diagnostics.update({kind: result['details'][kind] for kind in read if kind in result['details']})
    if result.get('error'):
        raise SettingsError(result['error'])
    return result


def label_counts(page):
    return settings_snapshot(page)['counts']


def settings_tab(page, section):
    """Return a semantic locator, never a positional locator across browser calls."""
    current = urlsplit(page.url).fragment.removeprefix('settings/')
    if current not in NAV_LABELS or section not in NAV_LABELS:
        raise SettingsError('wrong_settings_page')
    require_page(page, current)
    dialogs = page.locator('dialog:visible, [role="dialog"]:visible, [role="alertdialog"]:visible')
    if dialogs.count() > 1:
        raise SettingsError('ambiguous_settings_navigation')
    scope = dialogs if dialogs.count() else page.locator('body')
    name = label_pattern(NAV_LABELS[section])
    tabs = scope.get_by_role('button', name=name).or_(scope.get_by_role('tab', name=name)).or_(scope.get_by_role('link', name=name))
    tabs = tabs.and_(page.locator(':visible'))
    if tabs.count() > 1:
        raise SettingsError('ambiguous_settings_navigation')
    return tabs if tabs.count() else None


def read_usage(page, diagnostics=None):
    values = settings_snapshot(page, section='usage', read=('session', 'weekly'), diagnostics=diagnostics)['values']
    return {'five_hour': values['session'], 'seven_day': values['weekly']}


def read_organization(page):
    return settings_snapshot(page, section='account', read=('organization',))['values']['organization']


def global_logout_available(page):
    require_page(page, 'account')
    # Imported lazily: browser actions use the same renderer without import cycles.
    from claude_browser_actions import global_logout_target, LogoutError
    try:
        target = global_logout_target(page)
    except LogoutError:
        raise SettingsError('ambiguous_logout_or_confirmation_open') from None
    if target is None:
        return False
    target.dispose()
    return True
