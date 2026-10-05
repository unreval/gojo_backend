"""Render evidence-backed projections for the character who is reading them.

This module only chooses words.  Event ownership, audience and authority are
decided by the existing evidence and memory readers before it is called.
"""
import json
import re


PROJECTION_VERSION = 'role_view_v2'


def _character_name(character_id):
    if not character_id or character_id == 'shared':
        return '角色'
    try:
        from characters_data._loader import load_core
        core = load_core(character_id)
        if core and core.get('name'):
            return core['name']
    except (ImportError, KeyError):
        pass
    return character_id


def _person(ref, observer_id, source_character_id=None):
    ref = str(ref or '')
    if ref == 'user' or ref.startswith('user:'):
        return '她'
    if ref in ('character', 'assistant', 'self'):
        ref = 'character:' + str(source_character_id or observer_id)
    if ref.startswith('character:'):
        character_id = ref.partition(':')[2]
        return '我' if character_id == observer_id else _character_name(character_id)
    return ref


def role_label(subject_ref, *, observer_id, source_character_id=None):
    """Label an already-identified speaker without touching their raw message."""
    return _person(subject_ref, observer_id, source_character_id)


def _object(value, observer_id, source_character_id):
    if str(value or '').startswith('character:'):
        return _person(value, observer_id, source_character_id)
    return str(value or '')


def _sentence(value):
    value = str(value or '').strip().rstrip('。.!！')
    return value + '。' if value else ''


def render_canonical_semantic(semantic, *, observer_id):
    """Render the existing deterministic decision; never infer a new fact."""
    if not isinstance(semantic, dict):
        return ''
    source = semantic.get('source_scope') or semantic.get('observer_ref')
    who = _person(semantic.get('subject_ref'), observer_id, source)
    object_text = _object(semantic.get('object'), observer_id, source)
    value = semantic.get('value')
    predicate = semantic.get('predicate')
    time_scope = str(semantic.get('time_scope') or '')
    prefix = (f'在{time_scope}，' if time_scope and time_scope not in ('unspecified', 'this_exchange')
              and not time_scope.startswith('deadline:') else '')
    if predicate == 'reported_preference':
        body = f'{who}{value}{object_text}'
    elif predicate in ('reported_identity', 'reported_state', 'reported_fact'):
        body = f'{who}的{object_text}是「{value}」'
    elif predicate == 'explicit_refusal':
        body = f'{who}{value}「{object_text}」'
    elif predicate == 'explicit_boundary':
        body = f'{who}明确说过：{value}「{object_text}」'
    elif predicate == 'explicit_promise':
        deadline = semantic.get('deadline') or time_scope.removeprefix('deadline:')
        if value == '已兑现':
            body = f'{who}说自己已兑现截至{deadline}的承诺「{object_text}」'
        else:
            body = f'{who}承诺在{deadline}前完成「{object_text}」'
    elif predicate in ('explicit_answer', 'pending_answer'):
        caller = _person(semantic.get('caller_ref'), observer_id, source)
        scope = f'{caller}称呼{who}为「{object_text}」'
        if predicate == 'pending_answer':
            body = f'{who}说会回答是否接受{scope}的问题，答案仍待回答'
        else:
            answer = '接受' if value == 'yes' else '不接受'
            body = f'{who}明确答复：{answer}{scope}（仅记录该答复，不推断感情或其他许可）'
    elif predicate == 'quoted_utterance':
        quote = json.dumps(str(semantic.get('evidence_text') or ''), ensure_ascii=False)
        body = f'{who}实际说过：{quote}（仅为话语记录，不证明话中内容或关系）'
    else:
        return ''
    return _sentence(prefix + body)


_LITERAL_PREFIX = re.compile(r'^(?:用户|她)明确自述：\s*')
_LITERAL_SUFFIX = re.compile(r'（仅限这次自述，不推断隐含心理）[。.!！]?$')


def _render_legacy(text, observer_id, source_character_id):
    text = str(text or '')
    if _LITERAL_PREFIX.match(text):
        body = _LITERAL_SUFFIX.sub('', _LITERAL_PREFIX.sub('', text)).strip()
        if body.startswith('我的'):
            body = '她的' + body[2:]
        elif body.startswith('我'):
            body = '她' + body[1:]
        elif not body.startswith('她'):
            body = '她明确说过：' + body
        return _sentence(body)
    # Older prose has no typed subject.  Adapt only narrator words outside
    # quoted source text; the canonical Raw Event remains untouched.
    name = _person('character:' + str(source_character_id or observer_id), observer_id)
    segments = re.split(r'(「[^」]*」|“[^”]*”|"[^"]*")', text)
    for index in range(0, len(segments), 2):
        segment = segments[index]
        segment = segment.replace('用户表示', '她说').replace('用户告诉', '她告诉')
        segment = segment.replace('用户', '她').replace('角色', name)
        if source_character_id and source_character_id != observer_id:
            segment = re.sub(r'(^|[。；;，,\n])(\s*)我(?!们)',
                             lambda match: match[1] + match[2] + name, segment)
        segments[index] = segment
    return ''.join(segments)


def render_role_view(text, *, observer_id, source_character_id=None, semantic=None):
    """Read adapter for role_view_v2 and historical human-readable records."""
    if semantic is not None:
        return render_canonical_semantic(semantic, observer_id=observer_id)
    return _render_legacy(text, observer_id, source_character_id)
