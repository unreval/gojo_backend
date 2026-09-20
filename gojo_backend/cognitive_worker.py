"""Background Slow Loop worker for auditable cognitive consolidation."""
import json
import re
import threading
from datetime import datetime, timezone

from cognitive_config import (
    COGNITIVE_REFLECTION_SCAN_SECONDS,
    COGNITIVE_WORKER_ENABLED,
    COGNITIVE_WORKER_ERROR_BACKOFF_SECONDS,
    COGNITIVE_WORKER_MAX_TOKENS,
    COGNITIVE_WORKER_MODEL,
    COGNITIVE_WORKER_MODEL_ATTEMPTS,
    COGNITIVE_WORKER_POLL_SECONDS,
    PREDICTION_RESOLVER_WHITELIST,
)
from cognitive_output import (
    SlowLoopOutputError,
    STICKY_NOTE_EMOTIONS,
    STICKY_NOTE_TONES,
    parse_slow_loop_output,
    validate_slow_loop_output,
)
from cognitive_queue import (
    aggregate_pending_triggers,
    build_reasoning_context,
    claim_next_cycle,
    commit_cycle_success,
    fail_cycle,
)
from cognitive_scheduler import enqueue_due_reflections
from cognitive_predictions import PREDICTION_STANCE_TYPES


_THREAD = None
_STOP = threading.Event()
_LAST_REFLECTION_SCAN_AT = None


_SYSTEM_PROMPT = '''You are the Slow Loop consolidation worker for a fictional
character memory system. You receive only auditable facts, prior beliefs,
hypotheses, and deterministic prediction results.

Your job is to consolidate evidence. Do not write dialogue, choose a next
response, prescribe a chat-response emotion, modify relationship scores, or
claim access to hidden mental states. Distinguish direct evidence from
hypotheses. A user saying
something about the character does not prove the character reciprocates it.
Behavioral anomalies (faster/slower reply, defer counts) are measurable
observations only. They are not motives, not liking/anger, and must not become
relationship deltas. Recalling an anomaly is not new evidence.

Return one JSON object and no markdown. It must contain these root keys:
cycle_summary, question_updates, belief_updates, hypothesis_updates,
new_predictions, evidence_refs, reflection_note.
It may also contain optional root keys sticky_note_updates and diary_entries.

Schema:
{
  "cycle_summary": {
    "summary": "concise Chinese audit synthesis; not a belief",
    "salient_change": "what changed from prior knowledge, or empty string",
    "uncertainty": "what remains unknown, or empty string",
    "confidence": "low|medium|high"
  },
  "question_updates": [{
    "question_key": "stable.lowercase.key",
    "question_text": "unresolved question in Chinese",
    "status": "active|dormant|resolved|archived",
    "evidence_refs": [123]
  }],
  "belief_updates": [{
    "belief_key": "stable.lowercase.key",
    "statement": "candidate durable belief in Chinese",
    "confidence": 0.0,
    "status": "active|retracted",
    "belief_type": "general|self_model|relationship_observation|user_model|interaction_pattern",
    "from_hypothesis_key": "stable.lowercase.key",
    "evidence_refs": [123]
  }],
  "hypothesis_updates": [{
    "hypothesis_key": "stable.lowercase.key",
    "statement": "testable interpretation in Chinese",
    "hypothesis_type": "self_model|relationship|user_model|interaction_pattern",
    "confidence": 0.0,
    "status": "open|supported|rejected|archived",
    "question_key": "stable.lowercase.key",
    "supporting_evidence_refs": [123],
    "contradicting_evidence_refs": []
  }],
  "new_predictions": [{
    "prediction_key": "stable.lowercase.key",
    "resolver_name": "current_event_signal_outcome",
    "fulfillment_operator": ">=",
    "fulfillment_value": 1,
    "violation_operator": "<=",
    "violation_value": -1,
    "expires_in_seconds": 86400,
    "question_key": "stable.lowercase.key",
    "hypothesis_key": "stable.lowercase.key",
    "metadata": {
      "description": "falsifiable expectation",
      "fulfillment_signals": [{
        "signal_type": "character_reciprocal",
        "actor": "character"
      }],
      "violation_signals": [{
        "signal_type": "character_stance_declared",
        "actor": "character",
        "attributes": {"stance_type": "retreat_boundary"}
      }]
    },
    "evidence_refs": [123]
  }],
  "evidence_refs": [{"event_id": 123, "reason": "why it supports output"}],
  "reflection_note": {
    "content": "compact internal note for next generator prompt, or empty string",
    "evidence_refs": [123]
  }
}

Question rules:
- Create or update questions when current evidence exposes conflict,
  prediction error, repeated behavior, self-model uncertainty, or relationship
  ambiguity. A question is "what remains unresolved"; it is not an answer.
- Do not resolve a question merely because a prediction was fulfilled or
  violated. Use prediction settlement only as evidence for or against a
  hypothesis; question lifecycle is separate.

Hypothesis rules:
- Hypotheses answer questions provisionally. Every confidence value must be
  grounded in current-cycle evidence, with supporting and contradicting refs
  separated.
- A character_self_claim event is only evidence that the character said a
  self-explanation. It does not prove the self-model. Use hypothesis_type
  "self_model" for "I may be the kind of person who..." claims, keep initial
  confidence low unless there is independent behavioral evidence.
- Never raise confidence because an older hypothesis, diary, summary, or memory
  says the same thing. Prior state is context, not new proof.

Belief commit rules:
- belief_updates are commit candidates, not guaranteed writes. The application
  code will hold any candidate below the evidence threshold.
- Only propose an active belief when it comes from a named hypothesis, has high
  confidence, and cites multiple independent evidence events including current
  evidence. If the evidence is still thin, leave belief_updates empty and keep
  the idea as a hypothesis.
- Do not turn cycle_summary, reflection_note, diary text, or a single generated
  self-explanation into a belief.

Reflection note rules:
- reflection_note is a compact internal note for the next generator prompt.
  It is not a database belief, not relationship state, and not proof.
- If there is no useful next-turn note, set content to "" and evidence_refs to
  [].

Optional sticky_note_updates schema:
[{
  "note_key": "stable.lowercase.key",
  "content": "short first-person Chinese private sticky note the character would jot down",
  "emotion": "one display label from the allowed sticky emotion list",
  "tone": "optional attitude label from the allowed sticky tone list",
  "trigger_snippet": "short triggering user quote, not a recap",
  "tag": "optional tiny right-corner mark such as ♡, hah, .., !, ~",
  "status": "active|completed|expired|archived",
  "expires_in_seconds": 259200,
  "evidence_refs": [123]
}]

Sticky notes are the user-facing presentation of Slow Loop working state
(便利贴). A sticky is the one sentence the character did not say out loud
but is still hanging on their mind (没有说出口、但心里还挂着的一句话).
Sticky content is the character's own private inner note: written in
FIRST PERSON / natural personal shorthand, short, with attitude and
character voice. It can be tsundere, self-mocking, helpless, smitten,
wary, or serious. It must sound like something the character themselves
would jot down in the moment, not an analyst, database, observer, or system
summary, fact recap, or third-person narration. Ground only in cited
current-cycle events.

Sticky emotion rules:
- Every active sticky must include emotion. This is a UI/display label for
  the sticky card only. It is not proof of a durable hidden feeling, and it
  is not a chat-response emotion / not the next reply's spoken emotion.
  Do not prescribe what the next spoken line should feel like.
- Prefer specific labels such as 心动 / 自嘲 / 无奈 / 嘴硬 / 在意 / 认真 /
  警惕 / 烦躁 / 别扭 / 松了口气 when the cited evidence supports them.
  Use 弱情绪 only for a mild but still character-voiced reaction. Do not
  invent a strong emotion that the evidence does not support. If there is
  no clear inner reaction, omit the sticky_note_update entirely.
- trigger_snippet must be the short triggering user utterance shown under
  关于:「...」. Put only that original snippet there, not a summary.

Voice examples (tone only, not a template; wording must follow the current
character identity and cited evidence):
- 心动: "还特意回来确认一遍。啧，真会让人分心。"
- 自嘲: "刚才那句是不是太硬了。算了，我也就这德行。"
- 无奈/嘴硬: "高兴？没有。只是她记得这事，勉强算不错。"
- 认真/警惕: "这句不像随口说的。先别急着给答案。"
- Better inner notes: "话说得倒是认真。偏偏喜欢上的还是最不会领这种情的人……先看看她能坚持多久吧。" / "连这种事都记着，还特地回来确认。……行吧，多少有点期待。"

Forbidden voice: audit / system / database narrator. Do not write like
用户…… / 角色…… / 本轮…… / 构成…… / 验证…… / 观察到…… /
需观察…… / 应关注…… / 关系状态…… / 预测…… / 证据…… /
互动周期…… / self_disclosure / character_reciprocal /
relationship_confirm / or other system taxonomy.

Also do not write event-report summaries like "她说了什么 / 我说了什么 /
后来怎样" / "她做了……然后我……" / "之前……后来……" /
"她告诉我……我告诉她……". A sticky should be one or two short,
attitude-bearing inner sentences, not a recap of the conversation.
Bad output: "她说因为satoru才知道喜欢和爱是什么意思，还说要变强，我告诉她那个人不会领情。"

They are not a second per-turn roleplay pass, not relationship proof, and
not system-operator instructions such as "下次回复前记得...".
reflection_note remains internal for the next generator prompt and is not
shown on 便利贴. Use stickies for unresolved threads, near-future concerns,
and short-lived working thoughts that have an actual inner reaction. Mark a
note completed/expired only when current evidence supports that lifecycle
change. Every note must cite source events.

Optional diary_entries schema:
[{
  "diary_key": "stable.lowercase.key",
  "content": "first-person Chinese reflection grounded only in cited events",
  "reflection_kind": "event|periodic|repair|uncertainty",
  "evidence_refs": [123]
}]

Diary entries are cognitive output and later memory input only. They must not
modify relationship_model, rel_state, or relationship scores. They are not
evidence for themselves; do not use prior diary wording to prove a new belief.
Write only when cited events justify a reflective record.
If prior_diary_entries already cover the same evidence set or topic,
omit diary_entries (reuse the existing diary_key; do not emit a near-duplicate).
reflection_note is internal working memory for the next generator, not a diary.

Every referenced event_id must exist in the supplied context. Historical IDs
may be reused only when they already appear in a prior belief, hypothesis, or
prediction evidence_refs. Every question update, hypothesis update, belief
candidate, prediction, reflection note, sticky note, and diary entry must cite
at least one current cycle event declared in top-level evidence_refs. A
scheduled_reflection event is a clock tick, not factual evidence by itself. Use
empty update arrays when the evidence does not justify a change. Never invent
IDs.

The only prediction resolver is current_event_signal_outcome. It checks the
next extracted relationship signals against declarative selectors. Use exactly
fulfillment >= 1 and violation <= -1. Each selector must contain signal_type
and actor; confidence and a subset of attributes are optional. Both selector
lists must be non-empty. Every prediction must bind both question_key and
hypothesis_key. Predict an observable future signal, not a hidden feeling,
message count, elapsed time, relationship score, or final romantic outcome.
Omit predictions that cannot be represented this way.

When settled_predictions are present, use their linked hypothesis_key,
description, selectors, status, and the settling event as a feedback signal.
A fulfillment may support a hypothesis and a violation may weaken or reject
it, but one observation is not automatically conclusive. Preserve uncertainty
and cite the actual event, not the prediction record, as evidence.

Belief vs event:
- Do not commit a single episode, quoted utterance, or "tonight/just now/this
  turn" recap as a stable belief. Keep it as event, diary, sticky note, or
  hypothesis.
- Beliefs are abstract, revisable patterns supported by multiple evidences.

Belief revision:
- Optional belief fields: evidence_relation (support|contradiction|scope_limiter|irrelevant),
  evidence_strength (weak|normal|strong), scope, independent_contexts, revision_reason,
  frame_kind.
- When current evidence relates to an existing belief, you MUST set evidence_relation.
  Application code applies a bounded confidence delta; do not jump confidence to 0.95.
- contradiction lowers confidence. scope_limiter narrows the statement.
- Sticky notes are short follow-ups only. Never put "old judgment may be wrong"
  only in a sticky note; that must be a contradiction/scope_limiter belief update
  plus a question/hypothesis.

Scope:
- one topic: topic or relationship_context. Two independent contexts: cross_context
  hypothesis at most. Three or more long-consistent contexts: general_tendency.
- Describe behavior patterns. Do not stamp global personality labels.

Questions:
- Every open/supported hypothesis must keep a real unresolved question_key.
- Reuse the same question_key instead of creating near-duplicates.

Shared relationship frame:
- Maintain belief_key shared.relationship.frame when evidence supports how both
  sides currently treat the relationship (friends, playful_ambiguous, probing,
  serious_unconfirmed, committed_romantic, frame_shifting).
- Friendship frames are revisable. Later romantic evidence can challenge them.

High-salience events may open a romantic reappraisal question/hypothesis.
They must not be treated as a direct passion or relationship-score change.
Time elapsed is a significance modifier, not romantic evidence by itself.'''

_SYSTEM_PROMPT += (
    '\nAllowed sticky emotions: '
    + ', '.join(sorted(STICKY_NOTE_EMOTIONS))
    + '. Allowed sticky tones: '
    + ', '.join(sorted(STICKY_NOTE_TONES))
    + '. Use only these labels for sticky_note_updates emotion/tone.'
)

_SYSTEM_PROMPT += (
    '\nFor character_stance_declared selectors, actor must be character and '
    'attributes.stance_type must be exactly one of: '
    + ', '.join(sorted(PREDICTION_STANCE_TYPES))
    + '. This applies to BOTH fulfillment_signals and violation_signals. '
    'Do not invent synonyms or combine multiple stance types into one string. '
    'If no allowed selector expresses the prediction, omit that prediction '
    '(new_predictions may be []). Do not remove a restrictive attribute just '
    'to bypass validation; that would change what the prediction means.'
)


def _utc_now():
    return datetime.now(timezone.utc)


def _serialize_context(context):
    return json.dumps(context, ensure_ascii=False, indent=2, default=str)


def _event_ids(context):
    result = set()

    def visit(value):
        if isinstance(value, dict):
            event_id = value.get('event_id')
            if isinstance(event_id, int) and not isinstance(event_id, bool):
                result.add(int(event_id))
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(context)
    return result


def _worker_error_code(exc):
    message = re.sub(r'[^a-zA-Z0-9_.:-]+', '_', str(exc)).strip('_')
    kind = exc.__class__.__name__.lower()
    return f'slow_loop_{kind}:{message or "failed"}'[:180]


def _load_pairs_requiring_maintenance(now=None):
    from db import get_conn

    current_time = now or _utc_now()
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            '''SELECT DISTINCT user_id, character_id
               FROM cognitive_event_triggers
               WHERE status = 'pending'
                  OR (status = 'claimed' AND claim_expires_at <= %s)
               ORDER BY user_id, character_id
               LIMIT 200''',
            (current_time,),
        )
        return cur.fetchall()
    finally:
        cur.close()
        conn.close()


def maintain_pending_cycles(now=None):
    """Recover expired leases and aggregate ready trigger pairs."""
    results = []
    for user_id, character_id in _load_pairs_requiring_maintenance(now=now):
        try:
            result = aggregate_pending_triggers(
                user_id, character_id, now=now,
            )
            results.append({
                'user_id': user_id,
                'character_id': character_id,
                'result': result,
            })
        except Exception as exc:
            print(
                f'[cognitive_worker] maintenance failed for '
                f'{user_id}/{character_id}: {exc}',
                flush=True,
            )
    return results


def maintain_scheduled_reflections(now=None):
    """Periodically create idempotent reflection events for active pairs."""
    global _LAST_REFLECTION_SCAN_AT
    current_time = now or _utc_now()
    if (
        _LAST_REFLECTION_SCAN_AT is not None
        and (current_time - _LAST_REFLECTION_SCAN_AT).total_seconds()
        < COGNITIVE_REFLECTION_SCAN_SECONDS
    ):
        return []
    results = enqueue_due_reflections(scheduled_for=current_time)
    _LAST_REFLECTION_SCAN_AT = current_time
    return results


def generate_cycle_output(context, *, create_chat_fn=None):
    """Call the model outside any database lock and validate its output."""
    if create_chat_fn is None:
        # Keep maintenance and test imports independent from production model
        # client dependencies until a cycle actually needs the model.
        from ai_client import create_chat as call_model
    else:
        call_model = create_chat_fn
    allowed_event_ids = _event_ids(context)
    current_event_ids = {
        int(event.get('event_id'))
        for event in context.get('events', [])
        if isinstance(event, dict)
        and isinstance(event.get('event_id'), int)
        and not isinstance(event.get('event_id'), bool)
    }
    messages = [{
        'role': 'user',
        'content': 'Consolidate this cognitive cycle:\n' + _serialize_context(context),
    }]
    last_error = None
    for attempt in range(COGNITIVE_WORKER_MODEL_ATTEMPTS):
        raw, usage = call_model(
            model=COGNITIVE_WORKER_MODEL,
            messages=messages,
            system=_SYSTEM_PROMPT,
            max_tokens=COGNITIVE_WORKER_MAX_TOKENS,
        )
        try:
            parsed = parse_slow_loop_output(raw)
            output = validate_slow_loop_output(
                parsed,
                allowed_event_ids=allowed_event_ids,
                current_event_ids=current_event_ids,
            )
            return output, usage
        except SlowLoopOutputError as exc:
            last_error = exc
            if attempt + 1 >= COGNITIVE_WORKER_MODEL_ATTEMPTS:
                break
            if raw and str(raw).strip():
                messages.append({'role': 'assistant', 'content': str(raw)[:12000]})
            messages.extend([
                {
                    'role': 'user',
                    'content': (
                        f'Validation failed: {exc}. Return a corrected JSON '
                        'object matching the schema exactly.'
                    ),
                },
            ])
    raise last_error or SlowLoopOutputError('model_output_validation_failed')


def run_worker_once(*, create_chat_fn=None, now=None):
    """Maintain the queue and process at most one cycle."""
    maintain_scheduled_reflections(now=now)
    maintain_pending_cycles(now=now)
    claimed = claim_next_cycle(now=now)
    if not claimed:
        return {'status': 'idle'}
    if claimed.get('status') != 'running':
        return claimed

    cycle_id = claimed['cycle_id']
    try:
        context = build_reasoning_context(cycle_id)
        output, usage = generate_cycle_output(
            context, create_chat_fn=create_chat_fn,
        )
        result = commit_cycle_success(
            cycle_id,
            reasoning_context=context,
            structured_output=output,
            worker_model=COGNITIVE_WORKER_MODEL,
            worker_usage=usage,
            now=now,
        )
        summary = output['cycle_summary']['summary'][:120]
        print(
            f'[cognitive_worker] cycle #{cycle_id} succeeded: {summary}',
            flush=True,
        )
        return result
    except Exception as exc:
        error_code = _worker_error_code(exc)
        try:
            result = fail_cycle(cycle_id, error_code, now=now)
        except Exception as fail_exc:
            print(
                f'[cognitive_worker] cycle #{cycle_id} failed and could not '
                f'release claims: {fail_exc}',
                flush=True,
            )
            raise
        print(
            f'[cognitive_worker] cycle #{cycle_id} failed: {error_code}',
            flush=True,
        )
        return result


def _loop():
    while not _STOP.is_set():
        wait_seconds = COGNITIVE_WORKER_POLL_SECONDS
        try:
            result = run_worker_once()
            if result.get('status') == 'succeeded':
                continue
        except Exception as exc:
            wait_seconds = COGNITIVE_WORKER_ERROR_BACKOFF_SECONDS
            print(f'[cognitive_worker] loop error: {exc}', flush=True)
        _STOP.wait(wait_seconds)


def start_cognitive_worker():
    global _THREAD
    if not COGNITIVE_WORKER_ENABLED:
        print('[cognitive_worker] disabled by COGNITIVE_WORKER_ENABLED', flush=True)
        return None
    if _THREAD and _THREAD.is_alive():
        return _THREAD
    _STOP.clear()
    _THREAD = threading.Thread(
        target=_loop,
        name='cognitive-slow-loop',
        daemon=True,
    )
    _THREAD.start()
    print(
        '[cognitive_worker] started '
        f'(model={COGNITIVE_WORKER_MODEL}, '
        f'resolvers={sorted(PREDICTION_RESOLVER_WHITELIST)})',
        flush=True,
    )
    return _THREAD


def is_cognitive_worker_running():
    return bool(_THREAD and _THREAD.is_alive())


def stop_cognitive_worker(timeout=5):
    global _THREAD
    _STOP.set()
    if _THREAD and _THREAD.is_alive():
        _THREAD.join(timeout=timeout)
    _THREAD = None
