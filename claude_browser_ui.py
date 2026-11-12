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
READER_VERSION = '0.4.1'
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
        if (el.tagName === 'SELECT') {
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


def rendered_text(locator):
    return locator.evaluate('(el) => { const r = (' + _RENDERER + ')(); return r.normalize(r.text(el)); }')


def visible_labels(page, labels, diagnostics=None):
    """Find exact visible labels, deduplicating wrappers around the same label.

    Scan the DOM independently of get_by_text so hidden copies cannot suppress
    their visible parent. Match the complete cleaned label, never a substring.
    Unknown labels and multiple actual rows still fail closed.
    """
    elements = page.locator('body *')
    indices = elements.evaluate_all('''(elements, labels) => {
        const r = (''' + _RENDERER + ''')();
        const expected = new Set(labels.map(label => r.normalize(label).toLowerCase()));
        const matches = elements.map((el, index) => ({el, index}))
            .filter(({el}) => r.visible(el) && expected.has(r.normalize(r.text(el)).toLowerCase()));
        return matches.filter(({el}) => !matches.some(other => other.el !== el && el.contains(other.el)))
            .map(({index}) => index);
    }''', list(labels))
    matched = [elements.nth(i) for i in indices]
    if diagnostics is not None:
        raw = page.get_by_text(label_pattern(labels))
        partial = page.get_by_text(label_pattern(labels, exact=False))
        diagnostics.update(partial_text_matches=sum(partial.nth(i).is_visible() for i in range(partial.count())),
                           rendered_text_matches=len(matched), reader_version=READER_VERSION,
                           exact_text_matches=sum(raw.nth(i).is_visible() for i in range(raw.count())))
    return matched


def settings_tab(page, section):
    """Find one exact settings navigation label; never guess among dialogs."""
    current = urlsplit(page.url).fragment.removeprefix('settings/')
    if current not in NAV_LABELS or section not in NAV_LABELS:
        raise SettingsError('wrong_settings_page')
    require_page(page, current)
    dialogs = page.locator('dialog, [role="dialog"], [role="alertdialog"]')
    visible = [dialogs.nth(i) for i in range(dialogs.count()) if dialogs.nth(i).is_visible()]
    if len(visible) > 1:
        raise SettingsError('ambiguous_settings_navigation')
    scope = visible[0] if visible else page.locator('body')
    candidates = scope.get_by_text(label_pattern(NAV_LABELS[section]))
    tabs = [candidates.nth(i) for i in range(candidates.count()) if candidates.nth(i).is_visible()]
    if len(tabs) > 1:
        raise SettingsError('ambiguous_settings_navigation')
    return tabs[0] if tabs else None


def unique_label(page, labels):
    visible = visible_labels(page, labels)
    if len(visible) != 1:
        raise SettingsError('missing_or_ambiguous_label')
    return visible[0]


def row_text(label, pattern, other_labels=(), expected_labels=()):
    """Find the smallest visible parent containing exactly one value.

    Multiple values are an ambiguity even if numerically identical. Never
    deduplicate two 0% rows or interpret the page's 100% available summary.
    """
    scope = label
    label_text = rendered_text(label)
    if expected_labels and not label_pattern(expected_labels).fullmatch(label_text):
        raise SettingsError('label_changed_during_read')
    for _ in range(6):
        if scope.count() != 1:
            break
        text = rendered_text(scope)
        if other_labels and label_pattern(other_labels, exact=False).search(text):
            raise SettingsError('ambiguous_row')
        # Adjacent inline spans can produce "Organization ID0000..." in
        # innerText even when CSS visually separates the columns.
        values = pattern.findall(text.replace(label_text, '\n', 1))
        if len(values) > 1:
            raise SettingsError('ambiguous_row')
        if len(values) == 1:
            return text, values[0]
        scope = scope.locator('..')
    raise SettingsError('missing_row_value')


def read_usage(page, diagnostics=None):
    require_page(page, 'usage')
    values = {}
    for kind in ('session', 'weekly'):
        details = {}
        if diagnostics is not None:
            diagnostics[kind] = details
        labels = visible_labels(page, LABELS[kind], details)
        if len(labels) != 1:
            raise SettingsError(kind + ('_label_missing' if not labels else '_label_ambiguous'))
        other = LABELS['weekly' if kind == 'session' else 'session']
        text, value = row_text(labels[0], PERCENT, other_labels=other, expected_labels=LABELS[kind])
        if REMAINING.search(text):
            raise SettingsError('remaining_is_not_used')
        number = float(value.replace(',', '.'))
        if not 0 <= number <= 100:
            raise SettingsError('invalid_percentage')
        values[kind] = number
        details['percentage_read'] = True
    return {'five_hour': values['session'], 'seven_day': values['weekly']}


def read_organization(page):
    require_page(page, 'account')
    _, value = row_text(unique_label(page, LABELS['organization']), ORG_ID, expected_labels=LABELS['organization'])
    return value.lower()


def global_logout_available(page):
    require_page(page, 'account')
    labels = tuple(label for group in GLOBAL_LABELS.values() for label in group)
    buttons = page.get_by_role('button', name=label_pattern(labels))
    count = sum(buttons.nth(i).is_visible() for i in range(buttons.count()))
    if count > 1:
        raise SettingsError('ambiguous_logout_or_confirmation_open')
    return count == 1
