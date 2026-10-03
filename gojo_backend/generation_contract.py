"""The non-streaming Generator envelope and its visible-reply boundary.

JSON Schema constrains transport shape. The existing reply and schedule guards
remain responsible for language, truth, and machine-intent validation.
"""

from utils import classify_reply_content, reply_message_rejection_reason


def _object(properties, required=()):
    return {
        'type': 'object',
        'properties': properties,
        'required': list(required),
        'additionalProperties': False,
    }


_STRING = {'type': 'string'}
_PLACE = _object({
    'provider': _STRING,
    'provider_place_id': _STRING,
    'area_description': _STRING,
})

# Keep optional properties below Anthropic's schema compilation limit. Do not
# encode semantic constraints here: they are checked after parsing and again
# at the schedule commit gate.
GENERATION_ENVELOPE_SCHEMA = _object({
    'emotion': _STRING,
    'messages': {
        'type': 'array',
        'minItems': 1,
        'items': _object({'jp': _STRING, 'zh': _STRING}, ('jp', 'zh')),
    },
    'reminder': _object({
        'date': _STRING, 'time': _STRING, 'content': _STRING,
        'notification': _STRING,
    }, ('date', 'time', 'content', 'notification')),
    'cancel_reminder': _object({'keyword': _STRING, 'latest': {'type': 'boolean'}}),
    'pending_transaction': _object({
        'type': {'type': 'string', 'enum': ['in', 'out']},
        'category': _STRING,
        'amount': {'type': 'number'},
        'desc': _STRING,
        'account_hint': _STRING,
        'date': _STRING,
        'time': {'type': ['string', 'null']},
    }, ('type', 'category', 'amount', 'desc', 'account_hint', 'date', 'time')),
    'proactive_promise': _object({
        'trigger_kind': {'type': 'string', 'enum': ['once', 'daily']},
        'trigger_at': _STRING,
        'trigger_time': _STRING,
        'context': _STRING,
    }, ('trigger_kind', 'context')),
    'schedule_action_intent': _object({
        'type': {'type': 'string', 'enum': [
            'complete', 'extend', 'cancel', 'relocate', 'insert']},
        'event_id': {'type': 'integer'},
        'expected_revision': {'type': 'integer'},
        'extend_minutes': {'type': 'integer'},
        'planned_place': _PLACE,
        'event': _object({
            'title': _STRING,
            'duration_minutes': {'type': 'integer'},
            'reply_state': _STRING,
            'category': _STRING,
            'fixedness': _STRING,
        }, ('title', 'duration_minutes')),
    }, ('type',)),
}, ('emotion', 'messages'))


def rejection_reason(parsed, raw='', min_messages=1, acceptance_mode=None):
    """Validate visible content; only server-created degraded text may omit zh."""
    if not isinstance(parsed, dict):
        return ('plaintext_verbal' if classify_reply_content(raw) == 'text'
                else 'malformed_json')
    messages = parsed.get('messages')
    if not isinstance(messages, list) or len(messages) < min_messages:
        return 'missing_messages'
    if acceptance_mode == 'plaintext':
        if (len(messages) == 1 and isinstance(messages[0], dict)
                and classify_reply_content(messages[0].get('jp')) == 'text'
                and messages[0].get('zh') == ''):
            return None
        return 'invalid_message_field'
    for message in messages:
        reason = reply_message_rejection_reason(message)
        if reason:
            return reason
    return None


def degraded_reply(raw):
    """Recover safe display text without translation or machine-only fields."""
    kind = classify_reply_content(raw)
    if kind not in ('nonverbal', 'text'):
        return None
    text = raw.strip()
    return {
        'emotion': '平静',
        'messages': [{'jp': text, 'zh': text if kind == 'nonverbal' else ''}],
        '_acceptance_mode': 'nonverbal' if kind == 'nonverbal' else 'plaintext',
    }
