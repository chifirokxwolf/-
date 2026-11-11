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


def label_pattern(labels):
    return re.compile(r'^\s*(?:' + '|'.join(re.escape(x) for x in labels) + r')\s*$', re.I)


def unique_label(page, labels):
    locator = page.get_by_text(label_pattern(labels))
    visible = [locator.nth(i) for i in range(locator.count()) if locator.nth(i).is_visible()]
    if len(visible) != 1:
        raise SettingsError('missing_or_ambiguous_label')
    return visible[0]


def row_text(label, pattern):
    """Find the smallest visible parent containing exactly one value.

    Multiple values are an ambiguity even if numerically identical. Never
    deduplicate two 0% rows or interpret the page's 100% available summary.
    """
    scope = label
    label_text = label.inner_text(timeout=3000)
    for _ in range(6):
        if scope.count() != 1:
            break
        text = scope.inner_text(timeout=3000)
        # Adjacent inline spans can produce "Organization ID0000..." in
        # innerText even when CSS visually separates the columns.
        values = pattern.findall(text.replace(label_text, '\n', 1))
        if len(values) > 1:
            raise SettingsError('ambiguous_row')
        if len(values) == 1:
            return text, values[0]
        scope = scope.locator('..')
    raise SettingsError('missing_row_value')


def read_usage(page):
    require_page(page, 'usage')
    values = {}
    for kind in ('session', 'weekly'):
        text, value = row_text(unique_label(page, LABELS[kind]), PERCENT)
        if REMAINING.search(text):
            raise SettingsError('remaining_is_not_used')
        number = float(value.replace(',', '.'))
        if not 0 <= number <= 100:
            raise SettingsError('invalid_percentage')
        values[kind] = number
    return {'five_hour': values['session'], 'seven_day': values['weekly']}


def read_organization(page):
    require_page(page, 'account')
    _, value = row_text(unique_label(page, LABELS['organization']), ORG_ID)
    return value.lower()


def global_logout_available(page):
    require_page(page, 'account')
    labels = tuple(label for group in GLOBAL_LABELS.values() for label in group)
    buttons = page.get_by_role('button', name=label_pattern(labels))
    count = sum(buttons.nth(i).is_visible() for i in range(buttons.count()))
    if count > 1:
        raise SettingsError('ambiguous_logout_or_confirmation_open')
    return count == 1
