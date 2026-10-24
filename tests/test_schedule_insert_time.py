"""Today's explicit insert clock is a structured contract, never inferred prose."""
import copy
import os
import sys

import pytest


sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'gojo_backend'))

from schedule_contract import normalize_action_intent


def _intent(**event_fields):
    return {'type': 'insert', 'event': {
        'title': '陪她学日语', 'duration_minutes': 60, 'reply_state': 'free',
        **event_fields,
    }}


@pytest.mark.parametrize('clock, expected', [
    ('21:00', '21:00'), ('9:00', '09:00'), ('00:00', '00:00'),
    ('23:59', '23:59'), ('9:30', '09:30'),
])
def test_insert_keeps_valid_start_time(clock, expected):
    intent = _intent(start_time=clock)
    original = copy.deepcopy(intent)
    normalized = normalize_action_intent(intent)
    assert normalized['event']['start_time'] == expected
    assert intent == original
    assert normalize_action_intent(normalized) == normalized


@pytest.mark.parametrize('clock', [
    '25:00', '晚上九点', '21:00:00', 2100, None, True, {}, [], '',
    '24:00', '21:60', '-1:00', '9:0', ' 21:00', '21:00\n',
])
def test_invalid_insert_start_time_rejects_entire_intent(clock):
    assert normalize_action_intent(_intent(start_time=clock)) is None


@pytest.mark.parametrize('field', ['sched_date', 'date', 'start_at'])
@pytest.mark.parametrize('location', ['intent', 'event'])
def test_insert_rejects_unsupported_date_fields(field, location):
    intent = _intent(start_time='21:00')
    target = intent if location == 'intent' else intent['event']
    target[field] = '2026-10-25T21:00:00+08:00'
    assert normalize_action_intent(intent) is None


def test_insert_without_start_time_preserves_complete_legacy_shape():
    intent = {'type': 'insert_event', 'reason': 'conflict',
              'planned_place': {'area_description': '休息室'},
              'next_event': {'title': '  陪她学日语  ', 'duration_minutes': '60',
                             'reply_state': 'free', 'category': 'social',
                             'fixedness': 'flexible'}}
    assert normalize_action_intent(intent) == {
        'type': 'insert', 'reason': 'conflict',
        'planned_place': {'area_description': '休息室'},
        'event': {'title': '陪她学日语', 'duration_minutes': 60,
                  'reply_state': 'free', 'category': 'social',
                  'fixedness': 'flexible',
                  'planned_place': {'area_description': '休息室'}},
    }


def test_free_text_cannot_become_an_insert():
    assert normalize_action_intent('今天九点陪她学日语') is None
