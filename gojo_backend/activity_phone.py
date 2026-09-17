"""Activity phone-access profiles for schedule busy behavior.

Busy state (free / soft_busy / hard_busy) stays the activity's objective
interruptibility. These profiles only vary phone-check cadence inside
soft_busy. Character modifiers scale the profile; they never hard-code a
character id into the scheduler.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Optional


@dataclass(frozen=True)
class ActivityPhoneProfile:
    kind: str
    busy_state: str
    interruptibility: str
    phone_access: str
    check_interval_min: int
    check_interval_max: int
    quick_reply_probability: float
    defer_probability: float

    def to_dict(self):
        return asdict(self)


PROFILES = {
    'free': ActivityPhoneProfile(
        kind='free', busy_state='free',
        interruptibility='high', phone_access='high',
        check_interval_min=0, check_interval_max=0,
        quick_reply_probability=1.0, defer_probability=0.0,
    ),
    'meeting': ActivityPhoneProfile(
        kind='meeting', busy_state='soft_busy',
        interruptibility='medium', phone_access='high',
        check_interval_min=5, check_interval_max=18,
        quick_reply_probability=0.42, defer_probability=0.58,
    ),
    'lesson_prep': ActivityPhoneProfile(
        kind='lesson_prep', busy_state='soft_busy',
        interruptibility='medium', phone_access='high',
        check_interval_min=12, check_interval_max=30,
        quick_reply_probability=0.40, defer_probability=0.60,
    ),
    'report_work': ActivityPhoneProfile(
        kind='report_work', busy_state='soft_busy',
        interruptibility='low-medium', phone_access='medium-high',
        check_interval_min=15, check_interval_max=35,
        quick_reply_probability=0.34, defer_probability=0.66,
    ),
    'soft_default': ActivityPhoneProfile(
        kind='soft_default', busy_state='soft_busy',
        interruptibility='medium', phone_access='medium-high',
        check_interval_min=8, check_interval_max=28,
        quick_reply_probability=0.38, defer_probability=0.62,
    ),
    'class': ActivityPhoneProfile(
        kind='class', busy_state='hard_busy',
        interruptibility='low', phone_access='low',
        check_interval_min=0, check_interval_max=0,
        quick_reply_probability=0.0, defer_probability=1.0,
    ),
    'combat': ActivityPhoneProfile(
        kind='combat', busy_state='hard_busy',
        interruptibility='none', phone_access='none',
        check_interval_min=0, check_interval_max=0,
        quick_reply_probability=0.0, defer_probability=1.0,
    ),
    'hygiene': ActivityPhoneProfile(
        kind='hygiene', busy_state='hard_busy',
        interruptibility='none', phone_access='none',
        check_interval_min=0, check_interval_max=0,
        quick_reply_probability=0.0, defer_probability=1.0,
    ),
    'driving': ActivityPhoneProfile(
        kind='driving', busy_state='hard_busy',
        interruptibility='none', phone_access='none',
        check_interval_min=0, check_interval_max=0,
        quick_reply_probability=0.0, defer_probability=1.0,
    ),
    'sleep': ActivityPhoneProfile(
        kind='sleep', busy_state='free',
        interruptibility='high', phone_access='high',
        check_interval_min=0, check_interval_max=0,
        quick_reply_probability=1.0, defer_probability=0.0,
    ),
    'meal': ActivityPhoneProfile(
        kind='meal', busy_state='free',
        interruptibility='high', phone_access='high',
        check_interval_min=0, check_interval_max=0,
        quick_reply_probability=1.0, defer_probability=0.0,
    ),
    'leisure': ActivityPhoneProfile(
        kind='leisure', busy_state='free',
        interruptibility='high', phone_access='high',
        check_interval_min=0, check_interval_max=0,
        quick_reply_probability=1.0, defer_probability=0.0,
    ),
    'commute': ActivityPhoneProfile(
        kind='commute', busy_state='soft_busy',
        interruptibility='medium', phone_access='high',
        check_interval_min=8, check_interval_max=22,
        quick_reply_probability=0.40, defer_probability=0.60,
    ),
    'exercise': ActivityPhoneProfile(
        kind='exercise', busy_state='soft_busy',
        interruptibility='low-medium', phone_access='medium',
        check_interval_min=15, check_interval_max=32,
        quick_reply_probability=0.30, defer_probability=0.70,
    ),
}

# (kind, keywords) — first match wins. Do not put character names here.
_TITLE_RULES = (
    ('sleep', ('睡', '就寝', '寝る', '休息', '入睡')),
    ('combat', ('任务', '讨伐', '战斗', '出勤', '祓除', '交战', '出击')),
    ('class', ('上课', '授课', '教学', '讲课', '上课中', '课堂')),
    ('hygiene', ('洗澡', '泡澡', '沐浴')),
    ('driving', ('开车', '驾驶')),
    ('meeting', ('会议', '开会', '例会', '谈判', '会谈')),
    ('lesson_prep', ('备课', '教案', '改作业', '批改')),
    ('report_work', ('报告', '汇报', '汇报材料', '处理报告', '写报告', '文书')),
    ('commute', ('通勤', '移动', '赶路', '电车', '地铁')),
    ('exercise', ('训练', '锻炼', '跑步', '健身')),
    ('meal', ('吃饭', '午餐', '晚餐', '早餐', 'brunch', '吃饭中')),
    ('leisure', ('逛街', '探店', '发呆', '散步', '排队')),
)


def busy_prompt_hint(activity) -> str:
    """Prompt tone for the current activity. Soft and hard must not share copy."""
    activity = activity or {}
    state = str(activity.get('reply_state') or '').strip()
    if state == 'hard_busy':
        return '\n★ 这段时间你没法看手机，暂时无法回复。'
    if state == 'soft_busy':
        return '\n★ 这段时间你在忙，但偶尔能瞄一眼手机。语气可以简短一些。'
    if state == 'free' or activity.get('can_reply'):
        return ''
    return '\n★ 这段时间你没法看手机，暂时无法回复。'


def classify_activity_kind(title: str) -> str:
    text = title or ''
    for kind, keywords in _TITLE_RULES:
        if any(k in text for k in keywords):
            return kind
    return ''


def profile_for_title(title: str, reply_state: Optional[str] = None) -> ActivityPhoneProfile:
    kind = classify_activity_kind(title)
    if kind and kind in PROFILES:
        return PROFILES[kind]
    state = (reply_state or '').strip()
    if state == 'hard_busy':
        return PROFILES['combat']
    if state == 'soft_busy':
        return PROFILES['soft_default']
    if state == 'free':
        return PROFILES['free']
    return PROFILES['soft_default']


def apply_character_modifier(profile: ActivityPhoneProfile, character_id=None) -> ActivityPhoneProfile:
    """Scale intervals using optional character.phone_behavior. Never branch on name."""
    if profile.busy_state != 'soft_busy':
        return profile
    extra = {}
    try:
        from characters import get_character
        char = get_character(character_id) if character_id else None
        if isinstance(char, dict):
            raw = char.get('phone_behavior') or (char.get('extra') or {}).get('phone_behavior')
            if isinstance(raw, dict):
                extra = raw
    except Exception:
        extra = {}
    try:
        scale = float(extra.get('check_interval_scale') or 1.0)
    except (TypeError, ValueError):
        scale = 1.0
    scale = min(1.8, max(0.6, scale))
    try:
        reply_bonus = float(extra.get('quick_reply_bonus') or 0.0)
    except (TypeError, ValueError):
        reply_bonus = 0.0
    reply = min(0.85, max(0.15, profile.quick_reply_probability + reply_bonus))
    lo = max(3, int(round(profile.check_interval_min * scale)))
    hi = max(lo + 1, int(round(profile.check_interval_max * scale)))
    return ActivityPhoneProfile(
        kind=profile.kind,
        busy_state=profile.busy_state,
        interruptibility=profile.interruptibility,
        phone_access=profile.phone_access,
        check_interval_min=lo,
        check_interval_max=hi,
        quick_reply_probability=reply,
        defer_probability=round(1.0 - reply, 4),
    )


def profile_for_activity(activity, character_id=None) -> ActivityPhoneProfile:
    activity = activity or {}
    base = profile_for_title(
        activity.get('title') or '',
        activity.get('reply_state'),
    )
    return apply_character_modifier(base, character_id or activity.get('character_id'))
