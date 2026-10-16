"""opening_hours.py — OSM opening_hours 常见子集解析器

只支持常见语法子集:
  24/7
  Mo-Fr 09:00-18:00
  Sa,Su 10:00-22:00
  Tu off
  Mo-Su 11:00-23:00
  多条规则用 ; 分隔

解析不了的一律标 unknown——不过滤,但也不当作"确定营业"。
零外部依赖,零模型调用。
"""

_DAY_MAP = {
    'mo': 0, 'tu': 1, 'we': 2, 'th': 3,
    'fr': 4, 'sa': 5, 'su': 6,
}


def parse_opening_hours(raw):
    """Parse OSM opening_hours string into structured spec.

    Returns dict with:
      status: 'always_open' | 'parsed' | 'unknown'
      raw: original string or None
      rules: list of {days: [int], ranges: [(int,int)] | None, off: bool}

    Day indices: 0=Monday .. 6=Sunday (matches Python weekday()).
    Time ranges: (start_minutes, end_minutes) from midnight.
    """
    if not raw or not isinstance(raw, str):
        return {'status': 'unknown', 'raw': None, 'rules': []}

    raw = raw.strip()
    if not raw:
        return {'status': 'unknown', 'raw': None, 'rules': []}

    if raw == '24/7':
        return {'status': 'always_open', 'raw': raw, 'rules': []}

    rules = []
    for part in raw.split(';'):
        part = part.strip()
        if not part:
            continue
        rule = _parse_rule(part)
        if rule is None:
            # Any unparseable segment → entire spec is unknown
            return {'status': 'unknown', 'raw': raw, 'rules': []}
        rules.append(rule)

    if not rules:
        return {'status': 'unknown', 'raw': raw, 'rules': []}

    return {'status': 'parsed', 'raw': raw, 'rules': rules}


def _parse_days(token):
    """Parse day spec: 'Mo-Fr', 'Sa,Su', 'Tu', 'Mo-Su', 'Mo,We,Fr'."""
    days = set()
    for part in token.split(','):
        part = part.strip()
        if not part:
            return None
        if '-' in part:
            pieces = part.split('-', 1)
            start_idx = _DAY_MAP.get(pieces[0].strip().lower())
            end_idx = _DAY_MAP.get(pieces[1].strip().lower())
            if start_idx is None or end_idx is None:
                return None
            if start_idx <= end_idx:
                days.update(range(start_idx, end_idx + 1))
            else:
                # Wrap around: Fr-Mo = Fr,Sa,Su,Mo
                days.update(range(start_idx, 7))
                days.update(range(0, end_idx + 1))
        else:
            idx = _DAY_MAP.get(part.strip().lower())
            if idx is None:
                return None
            days.add(idx)
    return sorted(days) if days else None


def _parse_time(token):
    """Parse 'HH:MM' → minutes since midnight. Returns None on failure."""
    parts = token.strip().split(':')
    if len(parts) != 2 or any(
            len(part) != 2 or not part.isascii() or not part.isdecimal()
            for part in parts):
        return None
    try:
        h, m = int(parts[0]), int(parts[1])
    except (TypeError, ValueError):
        return None
    if not (0 <= h <= 24 and 0 <= m <= 59) or (h == 24 and m != 0):
        return None
    return h * 60 + m


def _parse_time_range(token):
    """Parse 'HH:MM-HH:MM' → (start_minutes, end_minutes)."""
    pieces = token.strip().split('-')
    if len(pieces) != 2:
        return None
    start = _parse_time(pieces[0])
    end = _parse_time(pieces[1])
    if start is None or end is None or start == 1440:
        return None
    return (start, end)


def _parse_rule(text):
    """Parse one rule like 'Mo-Fr 09:00-18:00' or 'Tu off'.

    Returns {days, ranges, off} or None on failure.
    """
    tokens = text.strip().split(None, 1)
    if len(tokens) < 2:
        return None

    days = _parse_days(tokens[0])
    if days is None:
        return None

    rest = tokens[1].strip()
    if rest.lower() == 'off':
        return {'days': days, 'ranges': None, 'off': True}

    # Time ranges, possibly comma-separated: "09:00-12:00,13:00-18:00"
    ranges = []
    for range_str in rest.split(','):
        r = _parse_time_range(range_str.strip())
        if r is None:
            return None
        ranges.append(r)

    if not ranges:
        return None

    return {'days': days, 'ranges': ranges, 'off': False}


def is_definitely_closed(opening_spec, weekday, time_minutes):
    """Check if a POI is DEFINITELY closed at the given time.

    Args:
        opening_spec: result of parse_opening_hours()
        weekday: 0=Monday .. 6=Sunday (Python weekday convention)
        time_minutes: minutes since midnight (e.g. 540 = 09:00)

    Returns True ONLY if we are certain the place is closed.
    Returns False if open, unknown, or any doubt.

    Conservative: false negatives are fine (miss a closed place),
    false positives are not (wrongly filter an open place).
    """
    if not opening_spec or not isinstance(opening_spec, dict):
        return False

    status = opening_spec.get('status')
    if status in ('unknown', 'always_open'):
        return False
    if status != 'parsed':
        return False

    rules = opening_spec.get('rules')
    if not rules:
        return False

    # Find the LAST rule that covers this weekday (OSM: later rules override)
    matching_rule = None
    for index, rule in enumerate(rules):
        if weekday in rule.get('days', []):
            matching_rule = rule
            matching_index = index

    if matching_rule is None:
        # No rule covers this day. Could mean closed, could mean not specified.
        # Conservative: don't filter.
        return False

    # A later rule may carry yesterday's opening hours past midnight.
    # Keep a potentially open POI; an earlier spillover is overridden by
    # today's last rule, just like an ordinary same-day opening rule.
    previous_day = (weekday - 1) % 7
    for rule in rules[matching_index + 1:]:
        if previous_day in rule.get('days', []) and not rule.get('off'):
            for start, end in rule.get('ranges') or []:
                if end <= start and time_minutes < end:
                    return False

    if matching_rule.get('off'):
        return True

    ranges = matching_rule.get('ranges')
    if not ranges:
        # Has a rule for this day but no time ranges → ambiguous
        return False

    # Check if time falls in any open range
    for start, end in ranges:
        if end > start:
            # Normal range: 09:00-18:00
            if start <= time_minutes < end:
                return False  # It's open
        else:
            # Overnight range: 22:00-02:00
            if time_minutes >= start or time_minutes < end:
                return False  # It's open

    # Time doesn't fall in any open range on this day → definitely closed
    return True
