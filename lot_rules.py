# noplug
"""Explicit paid durations; review bonuses are not part of the purchase title."""
from decimal import Decimal, InvalidOperation
import re

LEGACY_RULES = [{'pattern': '(?i)Claude PRO.*НА 5 ЧАСОВ.*АРЕНДА', 'hours': 5,
                 'quantity_is_duration': False}]
PAID_TERM = re.compile(r'\b(?:на|аренда)\s*(\d+)\s*(минут(?:а|ы)?|мин\.?|час(?:а|ов)?|ч\.?)\b', re.I)


def literal_pattern(title):
    # Cardinal appends the bought quantity to OrderShortcut.description.
    return r'(?i)^' + re.escape(title) + r'(?:,\s+\d[\d ]*\s+(?:шт|pcs)\.)?$'


def default_rules():
    rules = []
    for minutes, phrase, label in [(30, r'30\s*мин(?:ут(?:а|ы)?)?', 'Claude — 30 минут'),
                                   (60, r'1\s*час(?:а|ов)?', 'Claude — 1 час'),
                                   (120, r'2\s*час(?:а|ов)?', 'Claude — 2 часа'),
                                   (180, r'3\s*час(?:а|ов)?', 'Claude — 3 часа'),
                                   (300, r'5\s*час(?:а|ов)?', 'Claude — 5 часов')]:
        rules.append({'label': label, 'pattern': r'(?i)(?=.*\bClaude\b)(?=.*\bаренда\b).*\b(?:на|аренда)\s*' + phrase + r'\b',
                      'minutes': minutes, 'quantity_is_duration': True, 'check_paid_term': True})
    return rules


def rule_seconds(rule):
    keys = [k for k in ('seconds', 'minutes', 'hours') if k in rule]
    if len(keys) != 1 or isinstance(rule[keys[0]], bool):
        raise ValueError('Specify exactly one duration unit')
    try:
        value = Decimal(str(rule[keys[0]])) * {'seconds': 1, 'minutes': 60, 'hours': 3600}[keys[0]]
        if not value.is_finite() or value != value.to_integral_value() or not 0 < value <= 31 * 86400:
            raise ValueError('Invalid duration')
        return int(value)
    except InvalidOperation:
        raise ValueError('Invalid duration') from None


def duration(description, amount, rules):
    suffix = re.search(r',\s+(\d[\d ]*)\s+(?:шт|pcs)\.\s*$', description, re.I)
    if suffix:
        if int(suffix[1].replace(' ', '')) != amount:
            raise ValueError('Order quantity and description disagree')
        description = description[:suffix.start()]
    matches = [r for r in rules if re.search(r['pattern'], description)]
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError('Ambiguous rental rule')
    rule = matches[0]
    seconds = rule_seconds(rule)
    if rule.get('check_paid_term'):
        terms = PAID_TERM.findall(description)
        if len(terms) != 1:
            raise ValueError('Ambiguous paid duration')
        value, unit = terms[0]
        if int(value) * (60 if unit.casefold().startswith('мин') else 3600) != seconds:
            raise ValueError('Title and configured duration disagree')
    units = amount if rule.get('quantity_is_duration', False) else 1
    if type(units) is not int or units < 1 or seconds * units > 31 * 86400:
        raise ValueError('Invalid order quantity')
    return seconds * units


def parse_minutes(text):
    match = re.fullmatch(r'\s*([1-9]\d*)\s*(мин(?:ут(?:а|ы)?)?\.?|ч(?:ас(?:а|ов)?)?\.?)?\s*', text, re.I)
    if not match:
        raise ValueError('Enter 30 мин or 1 час')
    minutes = int(match[1]) * (60 if (match[2] or '').casefold().startswith('ч') else 1)
    if not 1 <= minutes <= 31 * 1440:
        raise ValueError('Invalid duration')
    return minutes
