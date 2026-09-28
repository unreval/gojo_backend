"""Deterministic novelty checks for generated schedule content.

Only flavor and discretionary leisure are strict.  Work, obligations, and
routine care may recur; making a teacher or family head unable to do normal
work would be a different kind of repetition bug.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import re
from typing import Any, Dict, Iterable, Optional


STRICT_CATEGORIES = {'flavor', 'leisure'}
POI_COOLDOWN_DAYS = 14
FOOD_COOLDOWN_DAYS = 10
MOTIF_COOLDOWN_DAYS = 7
NOTE_COOLDOWN_DAYS = 14

_SPACE = re.compile(r'\s+')
_PUNCT = re.compile(r'[，。！？、,.!?:：;；“”"\'\-—()（）\[\]{}]')
_FOOD_ITEMS = (
    '冰淇淋', '可颂', '蛋糕', '甜点', '甜品', '布丁', '华夫饼',
    '可丽饼', '咖啡', '拉面', '寿司', '下午茶', 'brunch',
)
_FOOD_FLAVORS = (
    '草莓', '抹茶', '巧克力', '栗子', '柚子', '桃子', '芒果', '香草',
    '芝麻', '焦糖', '柠檬', '红豆', '咖啡',
)
_MOTIF_TERMS = (
    '赏樱', '红叶', '花火', '祭', '限定', '甜品', '探店', '看展',
    '散步', '泡汤', '购物', '夜游',
)


@dataclass(frozen=True)
class NoveltyDecision:
    rejected: bool
    reason: str = ''


def normalized_text(value: Any) -> str:
    text = str(value or '').strip().lower()
    text = _PUNCT.sub('', text)
    return _SPACE.sub('', text)


def _place_id(item: Dict[str, Any]) -> str:
    place = item.get('planned_place') or item.get('place') or {}
    if not isinstance(place, dict):
        return ''
    provider = normalized_text(place.get('provider'))
    provider_id = normalized_text(place.get('provider_place_id') or place.get('place_id'))
    return f'{provider}:{provider_id}' if provider and provider_id else ''


def _food_key(item: Dict[str, Any]) -> str:
    text = normalized_text(f"{item.get('title', '')} {item.get('note', '')}")
    food = next((term for term in _FOOD_ITEMS if normalized_text(term) in text), '')
    if food:
        flavor = next((term for term in _FOOD_FLAVORS
                       if normalized_text(term) in text), '')
        return normalized_text(f'{flavor}{food}')
    return ''


def _motif_key(item: Dict[str, Any]) -> str:
    text = normalized_text(f"{item.get('title', '')} {item.get('note', '')}")
    for term in _MOTIF_TERMS:
        if normalized_text(term) in text:
            return normalized_text(term)
    return ''


def _as_date(value: Any):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _within_cooldown(candidate: Dict[str, Any], prior: Dict[str, Any],
                     days: int, candidate_date=None) -> bool:
    candidate_day = _as_date(candidate_date or candidate.get('sched_date'))
    prior_day = _as_date(prior.get('sched_date'))
    if candidate_day is None or prior_day is None:
        # Callers provide a bounded recent-history window. Without dates, be
        # conservative and treat that supplied window as the cooldown.
        return True
    distance = (candidate_day - prior_day).days
    return 0 <= distance < days


def validate_novelty(candidate: Dict[str, Any], history: Iterable[Dict[str, Any]],
                     *, candidate_date=None) -> NoveltyDecision:
    """Return a deterministic decision without silently rewriting content."""
    category = str(candidate.get('category') or '').strip().lower()
    if category not in STRICT_CATEGORIES:
        return NoveltyDecision(False)
    candidate_poi = _place_id(candidate)
    candidate_food = _food_key(candidate)
    candidate_motif = _motif_key(candidate)
    candidate_note = normalized_text(candidate.get('note'))
    for prior in history or []:
        if not isinstance(prior, dict):
            continue
        prior_category = str(prior.get('category') or '').strip().lower()
        if prior_category not in STRICT_CATEGORIES:
            continue
        if (candidate_poi and candidate_poi == _place_id(prior)
                and _within_cooldown(candidate, prior, POI_COOLDOWN_DAYS,
                                     candidate_date)):
            return NoveltyDecision(True, 'same_poi_cooldown')
        if (candidate_food and candidate_food == _food_key(prior)
                and _within_cooldown(candidate, prior, FOOD_COOLDOWN_DAYS,
                                     candidate_date)):
            return NoveltyDecision(True, 'same_specific_food_cooldown')
        if (candidate_motif and candidate_motif == _motif_key(prior)
                and _within_cooldown(candidate, prior, MOTIF_COOLDOWN_DAYS,
                                     candidate_date)):
            return NoveltyDecision(True, 'same_flavor_motif_cooldown')
        if (candidate_note and len(candidate_note) >= 6
                and candidate_note == normalized_text(prior.get('note'))
                and _within_cooldown(candidate, prior, NOTE_COOLDOWN_DAYS,
                                     candidate_date)):
            return NoveltyDecision(True, 'same_note_wording_cooldown')
    return NoveltyDecision(False)


def role_bucket_for_item(item: Dict[str, Any]) -> str:
    text = normalized_text(f"{item.get('title', '')} {item.get('note', '')}")
    if any(term in text for term in ('家族', '家主', '本家', '家系', '资源安排', '人员安排', '对外交涉')):
        return 'clan_head'
    if any(term in text for term in ('授课', '上课', '教学', '备课', '教案', '学生', '批改')):
        return 'teacher'
    if any(term in text for term in ('任务', '祓除', '巡逻', '咒术', '战斗', '出勤')):
        return 'sorcerer'
    return 'personal'


def responsibility_weights(history: Iterable[Dict[str, Any]]) -> Dict[str, float]:
    """Favor a role that has been underrepresented in the recent window."""
    buckets = {'teacher': 0, 'sorcerer': 0, 'clan_head': 0}
    for item in history or []:
        if not isinstance(item, dict):
            continue
        bucket = item.get('role_bucket') or role_bucket_for_item(item)
        if bucket in buckets:
            buckets[bucket] += 1
    total = sum(buckets.values())
    if not total:
        return {'teacher': 1.0, 'sorcerer': 1.0, 'clan_head': 1.0}
    return {
        key: round(max(0.35, (total - count + 1) / float(total + 1)), 3)
        for key, count in buckets.items()
    }
