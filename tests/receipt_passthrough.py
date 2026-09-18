"""Always-generate stub so existing route tests do not hit Postgres."""
import types
from unittest.mock import Mock


class _PassthroughHeartbeat:
    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        return self

    def stop(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def passthrough_generation_receipt():
    module = types.ModuleType('db_generation_receipt')
    module.ENDPOINT_CHAT_TEXT = 'chat_text'
    module.ENDPOINT_CHAT_IMAGE = 'chat_image'
    module.CRASH_BEFORE_COMPLETE = False
    module.CRASH_BEFORE_EFFECT = None
    module.CRASH_AFTER_EFFECT = None
    module.DEFAULT_LEASE_SECONDS = 240
    module.DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 45
    module.GenerationHeartbeat = _PassthroughHeartbeat

    def assistant_turn_id_for(endpoint, source_event_id):
        source = str(source_event_id or '').strip()
        if endpoint == 'chat_image':
            return f'image_reply:{source}'
        return f'chat_reply:{source}'

    def stamp_assistant_messages(msgs, turn_id):
        stamped = []
        for index, raw in enumerate(msgs or []):
            item = dict(raw or {})
            item['event_id'] = f'{turn_id}:{index}'
            item['segment_index'] = index
            stamped.append(item)
        return stamped

    def assign_source_event_id(raw):
        source = str(raw or '').strip()
        if source:
            return source, False
        assigned = 'legacy:passthrough'
        print(f'[chat:idempotency] legacy request without source_event_id; assigned {assigned}')
        return assigned, True

    def needed_effects(endpoint, payload, ctx=None):
        ctx = ctx or {}
        payload = payload or {}
        effects = ['assistant_short_memory', 'record_turn']
        user_text = str(
            ctx.get('user_text')
            or payload.get('_user_text')
            or payload.get('user_text')
            or ''
        ).strip()
        if user_text:
            effects.append('private_extraction')
        # Image passthrough tests do not stub relationship_engine.
        if endpoint == 'chat_text':
            effects.append('behavior_evidence')
            effects.append('relationship_update')
            msgs = payload.get('messages') or []
            if any(str((m or {}).get('zh') or '').strip() for m in msgs):
                effects.append('promise_detector')
        if payload.get('reminder'):
            effects.append('reminder')
        if (payload.get('cancelled_tasks')
                or payload.get('cancel_reminder')
                or payload.get('_cancel_reminder')
                or payload.get('_cancel_targets')
                or ctx.get('cancel_reminder')):
            effects.append('cancel_reminder')
        if (payload.get('saved_promise')
                or payload.get('proactive_promise')
                or payload.get('_proactive_promise')
                or ctx.get('proactive_promise')):
            effects.append('proactive_promise')
        return effects

    def resolve_generation(user_id, character_id, source_event_id, endpoint, **_kwargs):
        source, _legacy = assign_source_event_id(source_event_id)
        return {
            'action': 'generate',
            'claim_token': 'passthrough',
            'source_event_id': source,
        }

    def complete_generation(user_id, character_id, source_event_id, endpoint,
                            claim_token, response_json, effects=None,
                            effect_payloads=None):
        return True

    def ensure_completed_generation_effects(user_id, character_id, source_event_id,
                                            endpoint, ctx=None, apply_fn=None):
        ctx = dict(ctx or {})
        ctx.setdefault('user_id', user_id)
        ctx.setdefault('character_id', character_id)
        ctx.setdefault('source_event_id', source_event_id)
        ctx.setdefault('endpoint', endpoint)
        payload = ctx.get('payload') or {}
        ran = []

        def _apply(effect, one_ctx):
            if apply_fn is not None:
                return apply_fn(effect, one_ctx)
            if effect == 'relationship_update':
                fn = (one_ctx or {}).get('relationship_fn')
                if fn is not None:
                    fn()
                return {}
            if effect == 'promise_detector':
                detect = (one_ctx or {}).get('detect_and_save')
                if detect is not None:
                    payload_one = (one_ctx or {}).get('payload') or {}
                    msgs = (one_ctx or {}).get('msgs') or payload_one.get('messages') or []
                    reply_zh = ' '.join(
                        str((m or {}).get('zh') or '') for m in msgs if (m or {}).get('zh'))
                    if reply_zh:
                        detect(
                            one_ctx.get('character_id'), one_ctx.get('user_id'),
                            one_ctx.get('user_text') or '', reply_zh)
                return {}
            from generation_effects import apply_effect
            return apply_effect(effect, one_ctx)

        for effect in needed_effects(endpoint, payload, ctx):
            _apply(effect, ctx)
            ran.append(effect)
        return ran

    def after_generation_commit(user_id, character_id, source_event_id, endpoint,
                                ctx=None, skip=None):
        return ensure_completed_generation_effects(
            user_id, character_id, source_event_id, endpoint, ctx=ctx)

    def hydrate_replay(body, character_id=None, **_kwargs):
        if not isinstance(body, dict):
            return body
        payload = {
            k: v for k, v in dict(body or {}).items() if not str(k).startswith('_')
        }
        try:
            from tts import tts_to_b64
            emotion = payload.get('emotion') or '平静'
            for item in payload.get('messages') or []:
                jp = item.get('jp') or ''
                if jp and not item.get('audio_b64'):
                    item['audio_b64'] = tts_to_b64(jp, emotion, None)
        except Exception:
            pass
        return payload

    def hydrate_completed_generation_response(
            user_id, character_id, source_event_id, endpoint, payload=None):
        return hydrate_replay(payload or {}, character_id=character_id)

    module.assistant_turn_id_for = assistant_turn_id_for
    module.stamp_assistant_messages = stamp_assistant_messages
    module.assign_source_event_id = assign_source_event_id
    module.needed_effects = needed_effects
    module.resolve_generation = resolve_generation
    module.complete_generation = complete_generation
    module.ensure_completed_generation_effects = ensure_completed_generation_effects
    module.after_generation_commit = after_generation_commit
    module.fail_generation = Mock(return_value=True)
    module.release_generation = Mock(return_value=True)
    module.renew_generation_lease = Mock(return_value=True)
    module.canonical_payload = lambda body: body
    module.hydrate_replay = hydrate_replay
    module.hydrate_completed_generation_response = hydrate_completed_generation_response
    module.in_progress_body = lambda sid: {
        'generation_in_progress': True,
        'source_event_id': sid,
        'retryable': True,
    }
    module.init_generation_receipt_table = Mock()
    module.claim_generation = Mock()
    module.get_generation = Mock()
    module.wait_for_completed = Mock()
    module.list_side_effects = Mock(return_value=[])
    module.occurrence_key_for = lambda endpoint, source_event_id, effect: (
        f'{endpoint}:{source_event_id}:{effect}')
    return module
