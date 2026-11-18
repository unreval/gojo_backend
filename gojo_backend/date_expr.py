"""Parse one explicit calendar day from a user message.

The caller supplies Singapore-local `now_local`. This module does not read the
clock and does not touch the database.
"""
import re
from datetime import date, datetime, timedelta


_DIGITS = {
    '一': 1, '二': 2, '三': 3, '四': 4, '五': 5,
    '六': 6, '七': 7, '八': 8, '九': 9,
}


def _number_tokens():
    tokens = ['三十一', '三十']
    for digit in '九八七六五四三二一':
        tokens.append('二十' + digit)
    tokens.append('二十')
    for digit in '九八七六五四三二一':
        tokens.append('十' + digit)
    tokens.append('十')
    tokens.extend('九八七六五四三二一')
    return tokens


_NUMBER = '(?:' + '|'.join(_number_tokens()) + r'|\d{1,2})'
_RELATIVE = re.compile(r'大前天|前天|昨天|今天')
_MONTH_DAY = re.compile(rf'(?<!\d)({_NUMBER})月({_NUMBER})[号日](?!线)')
_DAY = re.compile(rf'(?<!\d)({_NUMBER})[号日](?!线)')
_WINDOW_DAYS = 90


def _today(now_local):
    if isinstance(now_local, datetime):
        return now_local.date()
    if isinstance(now_local, date):
        return now_local
    return None


def _number_value(token):
    if token.isdigit():
        value = int(token)
    elif token in _DIGITS:
        value = _DIGITS[token]
    elif token == '十':
        value = 10
    elif token == '二十':
        value = 20
    elif token == '三十':
        value = 30
    elif token.startswith('十') and len(token) == 2 and token[1] in _DIGITS:
        value = 10 + _DIGITS[token[1]]
    elif token.startswith('二十') and len(token) == 3 and token[2] in _DIGITS:
        value = 20 + _DIGITS[token[2]]
    elif token.startswith('三十') and len(token) == 3 and token[2] in _DIGITS:
        value = 30 + _DIGITS[token[2]]
    else:
        return None
    if 1 <= value <= 31:
        return value
    return None


def _calendar_date(year, month, day):
    if not 1 <= month <= 12:
        return None
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _accept(found, today):
    if found is None or found > today:
        return None
    if (today - found).days > _WINDOW_DAYS:
        return None
    return found


def _recent_day(today, day_num):
    year, month = today.year, today.month
    for _ in range(14):
        found = _calendar_date(year, month, day_num)
        if found is not None and found <= today:
            return _accept(found, today)
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    return None


def _recent_month_day(today, month, day):
    found = _calendar_date(today.year, month, day)
    if found is None or found > today:
        found = _calendar_date(today.year - 1, month, day)
    return _accept(found, today)


def _resolve(kind, match, today):
    if kind == 'relative':
        offset = {'今天': 0, '昨天': 1, '前天': 2, '大前天': 3}[match.group(0)]
        return _accept(today - timedelta(days=offset), today)
    if kind == 'month_day':
        month = _number_value(match.group(1))
        day = _number_value(match.group(2))
        if month is None or day is None or month > 12:
            return None
        return _recent_month_day(today, month, day)
    day = _number_value(match.group(1))
    if day is None:
        return None
    return _recent_day(today, day)


def parse_date_expression(text, now_local):
    """Return the first explicit day in `text`, or None.

    Day-only and month-day expressions use the latest such day that is not
    after `now_local`. A result more than 90 days earlier, or any future day,
    is None.
    """
    today = _today(now_local)
    if today is None or not isinstance(text, str) or not text:
        return None
    found = []
    for kind, pattern in (
            ('relative', _RELATIVE),
            ('month_day', _MONTH_DAY),
            ('day', _DAY)):
        for match in pattern.finditer(text):
            found.append((match.start(), -(match.end() - match.start()), kind, match))
    if not found:
        return None
    found.sort()
    _, _, kind, match = found[0]
    return _resolve(kind, match, today)
