"""Deterministic candidate checks and projection through existing authority.

Passing these bounded checks does not confer cognitive or recall authority.
The caller owns the cursor's transaction and the lifetime of source read locks.
"""
import hashlib
import json
import logging
import re
import unicodedata

from raw_events import get_active_events_by_ids, cognitive_source_matches_scope
from memory_authority import project_canonical_memory
from utils import _check_number_consistency


_PREDICATES = frozenset({
    'reported_preference', 'reported_state', 'reported_identity', 'reported_fact',
})
_LOGGER = logging.getLogger(__name__)
_NUMBER = re.compile(
    r'[-+负]?(?:[0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?'
    r'|[0-9零〇一二三四五六七八九十百千万萬亿億兆两]+(?:[.点][0-9零〇一二三四五六七八九]+)?)'
)
_HELPER_NUMBER = re.compile(r'(?:[0-9]+|[零〇一二三四五六七八九十百千]+)')

# Ordinary words whose characters happen to be numerals. A number token is
# ignored only when the whole token sits inside one of these words, so 三十分
# still counts while 十分 (the adverb) does not.
_NUMBER_WORD_EXEMPTIONS = (
    '八卦',
    '一直', '一边', '一起', '一样', '一点', '一些', '一下', '一切',
    '一定', '一般', '一会', '万一', '十分', '第一', '二手',
)
# 一 immediately before a measure is an indefinite article (一个 / 一瓶),
# not a quantity. Larger numerals such as 一百个 stay quantities.
_INDEFINITE_MEASURES = frozenset('个瓶次张条只台份句天年块杯件本场段种类点')

# Chinese side of utils' translation guards. These are bounded lexical checks,
# not a general entailment or multi-clause polarity classifier.
_DOUBLE_NEGATION = re.compile(r'不是不|并非不|不能不|不得不|没有不')
# X不X / X没X with the same single character is a question (是不是 / 有没有),
# not a denial. Multi-character repeats such as 喜欢不喜欢 are left alone.
_A_NOT_A = re.compile(r'([\u4e00-\u9fff])(?:不|没)\1')
# 淹没/埋没/出没/沉没/吞没 put 没 in the second character. 没收 is confiscation,
# but 没收到/没收下/没收着/没收过 stay denials. 没关系/没错/没事 are
# conversational affirmations; 没事做/没事干/没事儿 stay denials.
_MEI_NON_NEGATION = re.compile(
    r'淹没|埋没|出没|沉没|吞没|没收(?![到下着过])'
    r'|没关系|没错|没事(?![做干儿])')
_GROUNDING_NEGATION = re.compile(
    r'不(?:是|会|能|想|要|行|可以|可能|应该|对|知道|必|用|需要|愿意|喜欢|爱|亏'
    r'|重要|合适|够|清楚|舒服)'
    r'|没'
    r'|别(?:想|说|闹|来|急|忘)|才不|才没|并不|绝不|从不'
    # Rhetorical denials. 哪有钱 denies having money exactly as 没钱 does, so a
    # faithful summary that writes 没 must not read as a polarity flip. Kept to
    # 哪 + a bounded verb list so 哪里 / 哪个 / 哪天 stay interrogatives.
    r'|哪(?:有|能|会)'
    # Japanese denial. Longer forms first. ない also covers 〜たくない.
    # Bare ぬ is not included: it is a classical auxiliary, and it also sits
    # inside everyday words such as いぬ and ぬるい.
    r'|ませんでした|なかった|ません|たくない|ない'
)
# ない-final words that are not denial. Removed before the denial search,
# the same way _MEI_NON_NEGATION removes 没关系 / 没错.
# あんない / しつない are the hiragana spellings of 案内 / 室内. The kanji
# forms contain no ない, but this speaker types those words in kana.
_JA_NON_NEGATION = re.compile(
    r'もったいない|さりげない|情けない|しようがない|しょうがない|しかたない'
    r'|つまらない|くだらない|たまらない|きたない|あんない|しつない|汚い|危ない|少ない'
)
# A bare 没有 / 没 answering the previous turn ("很疼吗" -> "没有哦，就是有点点疼")
# denies the question, not the clause that follows it. It is dropped only when
# nothing else in the text carries a denial, so 没有，不疼 keeps its polarity.
_ANSWER_PARTICLE_NEGATION = re.compile(r'^(?:没有|没)(?=[哦啊呀吧呢，,。！!？?…\s])')
_ANY_NEGATION_CHAR = re.compile(r'[不没别无非甭]')
_TIME_WORDS = {
    '早上': 'morning', '上午': 'morning', '清晨': 'morning', '今早': 'morning',
    '早饭': 'morning', '早餐': 'morning',
    '中午': 'noon', '正午': 'noon', '午饭': 'noon', '午餐': 'noon',
    '下午': 'afternoon', '傍晚': 'evening', '黄昏': 'evening',
    '晚上': 'night', '今晚': 'night', '晚饭': 'night', '晚餐': 'night',
    '深夜': 'late_night', '半夜': 'late_night',
    '昨晚': 'night', '明晚': 'night', '明早': 'morning',
}
_COMPATIBLE_TIMES = {
    'morning': {'morning'}, 'noon': {'noon'}, 'afternoon': {'afternoon'},
    'evening': {'evening', 'night'},
    'night': {'night', 'evening', 'late_night'},
    'late_night': {'late_night', 'night'},
}
_DAY_WORDS = {
    '前天': -2, '昨天': -1, '昨日': -1, '昨晚': -1,
    '今天': 0, '今日': 0, '今早': 0, '今晚': 0,
    '明天': 1, '明日': 1, '明早': 1, '明晚': 1, '后天': 2,
}
_PERSPECTIVE_ASSERTION = re.compile(
    r'(?:她|他|你|您|用户|对方)(?:其实|心里|内心|肯定|一定|在想|不敢说)'
    r'|(?:她|他|你|您|用户|对方)[^，。！？；\n]{0,12}'
    r'(?:在意|喜欢|讨厌|爱|想要|希望|害怕|担心|难过|开心|嫉妒|依赖|安全感)'
)
# Prompt-shaped report frame. Dropped once from the start of the residue,
# after pronouns, the same way a leading answer particle is dropped once.
# A 说 later in the sentence can be what she actually said, so it stays.
_LEADING_REPORT_FRAME = re.compile(r'^(?:说自己|说)')


def _same_number(left, right):
    left, right = left.replace(',', ''), right.replace(',', '')
    if left == right:
        return True
    if any(char in left + right for char in '两万萬亿億'):
        def integer_value(token):
            # Only explicit integers, optionally scaled by one final 万/亿.
            # Compound/shorthand forms stay exact-only (e.g. 一万二, 两百二).
            token = token.replace('两', '二').replace('萬', '万').replace('億', '亿')
            scale = {'万': 10000, '亿': 100000000}.get(token[-1], 1)
            if scale != 1:
                token = token[:-1]
            if not _HELPER_NUMBER.fullmatch(token):
                return None
            if token.isascii():
                coefficient = token.lstrip('0') or '0'
                return coefficient + ('0' * (len(str(scale)) - 1) if coefficient != '0' else '')
            digits = dict(zip('零〇一二三四五六七八九', (0, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9)))
            if all(char in digits for char in token):
                # 连写 can mean an approximate range, e.g. 两三万.
                return str(digits[token] * scale) if len(token) == 1 else None
            total, digit, last_unit = 0, None, 10000
            for index, char in enumerate(token):
                if char in digits:
                    if digit not in (None, 0):
                        return None
                    if char in '零〇' and (index == 0 or index == len(token) - 1
                                           or digit == 0 or last_unit == 10):
                        return None
                    digit = digits[char]
                else:
                    unit = {'十': 10, '百': 100, '千': 1000}[char]
                    if (unit >= last_unit or digit == 0
                            or (digit is None and (char != '十' or index != 0))
                            or (index >= 2 and token[index - 2] in '零〇'
                                and unit * 10 == last_unit)):
                        return None
                    total += (digit or 1) * unit
                    digit, last_unit = None, unit
            if digit and last_unit > 10 and token[-2] not in '零〇':
                return None
            return str((total + (digit or 0)) * scale)

        values = [integer_value(token) for token in (left, right)]
        return None not in values and values[0] == values[1]
    # The existing helper ignores long/unsupported tokens. None is evidence of
    # equality only when both tokens are known to be within its numeric grammar.
    if all(len(token) <= 6 and _HELPER_NUMBER.fullmatch(token)
           for token in (left, right)):
        return _check_number_consistency(left, right) is None
    return False


def _number_tokens(text):
    """Explicit quantities, minus numeral characters that are not quantities."""
    text = unicodedata.normalize('NFKC', text)
    exempt_spans = []
    for word in sorted(_NUMBER_WORD_EXEMPTIONS, key=len, reverse=True):
        start = 0
        while True:
            index = text.find(word, start)
            if index < 0:
                break
            end = index + len(word)
            # 十分钟 / 十分秒 is ten units, not the adverb 十分.
            # 一会议 is 一 + 会议, not the indefinite duration 一会.
            if not (word == '十分' and end < len(text) and text[end] in '钟鐘秒') \
                    and not (word == '一会' and end < len(text) and text[end] == '议'):
                exempt_spans.append((index, end))
            start = end
    tokens = []
    for match in _NUMBER.finditer(text):
        start, end = match.span()
        if any(word_start <= start and end <= word_end
               for word_start, word_end in exempt_spans):
            continue
        token = match.group()
        if token == '一' and end < len(text) and text[end] in _INDEFINITE_MEASURES:
            continue
        tokens.append(token)
    return tokens


def _numbers_match(quote, summary):
    source_numbers = _number_tokens(quote)
    summary_numbers = _number_tokens(summary)
    # Compare every summary value separately so a shared value cannot mask an
    # invented one. Clock values are checked too; translation exemptions do not
    # authorize adding numbers to a memory candidate.
    return all(any(_same_number(number, source) for source in source_numbers)
               for number in summary_numbers)


def _has_grounding_negation(text):
    """Whether G5 and value polarity should treat text as containing a denial.

    A leading 没有 / 没 that answers the previous turn is dropped first when the
    rest of the text carries no denial of its own.
    Questions of the form X不X / X没X are removed first. 没 is a denial except
    the words in _MEI_NON_NEGATION, including 没关系 / 没错 / 没事.
    没事做、没事干 and 没事儿 stay denials. 不 stays on a fixed verb list so
    不过 / 不但 / 不仅 / 不如 / 差不多 are not denials.
    Japanese ない / ません is a denial except the words in _JA_NON_NEGATION.
    """
    answered = _ANSWER_PARTICLE_NEGATION.sub('', text, count=1)
    if answered != text and not _ANY_NEGATION_CHAR.search(answered):
        text = answered
    text = _DOUBLE_NEGATION.sub('', text)
    text = _MEI_NON_NEGATION.sub('', text)
    text = _JA_NON_NEGATION.sub('', text)
    text = _A_NOT_A.sub('', text)
    return _GROUNDING_NEGATION.search(text) is not None


def _time_words(text, vocabulary):
    return {vocabulary[match.group()] for match in re.finditer(
        '|'.join(sorted(vocabulary, key=len, reverse=True)), text)}


def _times_match(quote, summary):
    source_days = _time_words(quote, _DAY_WORDS)
    summary_days = _time_words(summary, _DAY_WORDS)
    if source_days and summary_days and not summary_days.issubset(source_days):
        return False
    periods = []
    for text in (quote, summary):
        for period, replacement in (('上午', '深夜'), ('下午', '正午')):
            text = re.sub(period + r'(?=(?:12|１２|十二)点)', replacement, text)
        periods.append(_time_words(text, _TIME_WORDS))
    source_times, summary_times = periods
    return not source_times or all(
        any(period in _COMPATIBLE_TIMES[source] for source in source_times)
        for period in summary_times)


def validate_candidate(cur, user_id, character_id, candidate):
    """Return (accepted, reason), without writing or calling a model.

    G1 compares the locked live source with the existing cognitive ingress
    snapshot, never with a caller-provided copy. Missing/changed sources fail
    closed. Database errors propagate to the caller.

    G8 alone mutates the input: an unsafe/uncertain perspective is removed and
    (True, 'perspective_dropped') is returned. Earlier rejection leaves the
    candidate untouched. No perspective is persisted by this module.

    interpretation is not an input. G1–G9 compare quote, summary, and the
    literal predicate fields only. A wrong or ungrounded reading stays on the
    candidate and cannot accept or reject it.
    """
    source_id = candidate.get('source_event_id') if isinstance(candidate, dict) else None
    if not isinstance(source_id, str) or not source_id.strip():
        return False, 'source_not_active_or_changed'
    cur.execute('''SELECT payload FROM cognitive_events
        WHERE user_id=%s AND character_id=%s
          AND source_event_type='canonical_user_turn' AND source_event_id=%s
        FOR SHARE''', (user_id, character_id, source_id))
    row = cur.fetchone()
    payload = row[0] if row and isinstance(row[0], dict) else {}
    chat_id = payload.get('source_chat_id') or character_id
    raw = get_active_events_by_ids(
        user_id, chat_id, [source_id], conn=cur.connection, lock=True)
    if len(raw) != 1:
        return False, 'source_not_active_or_changed'
    if raw[0]['role'] != 'user':
        return False, 'source_not_active_or_not_user'
    raw_content = raw[0]['content']
    if (not isinstance(raw_content, str) or raw_content != payload.get('content')
            or not cognitive_source_matches_scope(raw[0], character_id, chat_id)):
        return False, 'source_not_active_or_changed'

    quote = candidate.get('quote')
    if not isinstance(quote, str) or raw_content.find(quote) < 0:
        return False, 'quote_not_verbatim'
    if len(re.sub(r'\s', '', quote)) < 4:
        return False, 'quote_too_short'

    summary = candidate.get('summary')
    if isinstance(summary, str):
        if not _numbers_match(quote, summary):
            return False, 'number_value_mismatch'
        if _has_grounding_negation(quote) != _has_grounding_negation(summary):
            return False, 'negation_polarity_mismatch'
        if not _times_match(quote, summary):
            return False, 'time_word_contradiction'
    predicate = candidate.get('predicate')
    if (not isinstance(predicate, str) or predicate not in _PREDICATES
            or not isinstance(summary, str) or not summary.strip() or len(summary) > 60
            or any(not isinstance(candidate.get(key), str) or not candidate[key].strip()
                   for key in ('object', 'value'))):
        return False, 'invalid_predicate_or_summary'

    # G9 covers summary residue after the time and number checks.
    # interpretation is a subjective reading and is not part of this text.
    # Grounding reads the whole canonical turn, not just the quoted span: one
    # user turn is split into several quotes, so a referent introduced in an
    # earlier sentence of the same turn (那个外卖小哥 ... 一看到他) is already
    # provenanced. raw_content is the verbatim turn verified above; nothing
    # outside it, and no generated text, becomes grounding evidence.
    grounded_text = re.sub(r'她|他|您|五条悟|用户|对方', '', summary)
    grounded_text = _LEADING_REPORT_FRAME.sub('', grounded_text, count=1)
    time_words = sorted(_TIME_WORDS.keys() | _DAY_WORDS.keys(), key=len, reverse=True)
    grounded_text = re.sub('|'.join(time_words), '', grounded_text)
    q = re.sub(r'\s', '', raw_content)
    grounded_text = re.sub(
        r'[-+负]?[0-9０-９零〇一二三四五六七八九十百千万萬亿億兆两,.点]+',
        lambda match: match.group() if match.group() in q else '', grounded_text)
    # CJK ideographs plus hiragana U+3040–U+309F and katakana U+30A0–U+30FF.
    # The long-vowel mark ー is inside the katakana block. A Japanese quote has
    # no ideograph for the old class to match, so coverage was identically zero.
    chars = ''.join(re.findall(
        r'[\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff]', grounded_text))
    covered = [False] * len(chars)
    for i in range(len(chars)):
        for j in range(len(chars), i + 1, -1):
            if chars[i:j] in q:
                covered[i:j] = [True] * (j - i)
                break
    # Empty residue has no G9 check, but still needs G8 perspective handling.
    if chars and sum(covered) / len(chars) < 0.60:
        return False, 'summary_not_grounded_in_quote'

    if 'perspective' in candidate:
        perspective = candidate['perspective']
        if (not isinstance(perspective, str) or not perspective.strip()
                or len(perspective) > 40 or not perspective.strip().startswith('我')
                or perspective.strip().startswith('我们')
                or _PERSPECTIVE_ASSERTION.search(perspective)):
            candidate.pop('perspective')
            return True, 'perspective_dropped'
    return True, None


def apply_candidate(cur, user_id, character_id, candidate, *, occurred_at,
                    event_id, operation_id):
    """Validate and project in the caller's transaction, without adjudicating.

    The caller supplies canonical ingress and adjudication. A physical projection
    alone never grants recall authority: the unchanged reader gate still checks
    the complete adjudication, stable belief and current source. No perspective,
    interpretation, or utterance observation enters the claim, projection, or
    log. The caller's candidate is unchanged.
    """
    checked = dict(candidate) if isinstance(candidate, dict) else candidate
    if isinstance(checked, dict):
        checked.pop('interpretation', None)
        checked.pop('utterance_mode', None)
        checked.pop('context_excerpt', None)
        checked.pop('prior_character_text', None)
    accepted, reason = validate_candidate(cur, user_id, character_id, checked)
    if not accepted:
        return {'status': 'rejected', 'reason': reason}
    perspective_dropped = 'perspective' in candidate
    checked.pop('perspective', None)
    if perspective_dropped:
        _LOGGER.info('perspective_dropped source_event_id=%s', checked['source_event_id'])

    cur.execute('''SELECT adjudication FROM cognitive_events
        WHERE id=%s AND user_id=%s AND character_id=%s
          AND source_event_type='canonical_user_turn' AND source_event_id=%s
        FOR SHARE''', (event_id, user_id, character_id, checked['source_event_id']))
    row = cur.fetchone()
    if row is None:
        return {'status': 'rejected', 'reason': 'source_not_active_or_changed'}
    adjudication = row[0] if isinstance(row[0], dict) else {}
    decision = (adjudication.get('operations', {}).get(operation_id, {})
                if 'operations' in adjudication else adjudication)
    if adjudication and not decision:
        return {'status': 'rejected', 'reason': 'adjudication_operation_missing'}
    scope = {'subject': 'user:' + user_id, 'predicate': checked['predicate'],
             'object': checked['object'], 'time_scope': checked.get('time_scope', 'unspecified'),
             'source': 'canonical_user_turn'}
    question_key = 'report.' + hashlib.sha256(
        json.dumps(scope, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()[:24]
    claim = dict(scope, value=checked['value'], question_key=question_key,
                 text=checked['quote'], display_summary=checked['summary'])
    projection = project_canonical_memory(
        cur, user_id=user_id, character_id=character_id, event_id=event_id,
        source_id=checked['source_event_id'], content=checked['summary'], claim=claim,
        belief_key=decision.get('belief_key'), occurred_at=occurred_at,
        operation_id=operation_id, dependencies=[{'source_id': checked['source_event_id']}])
    return dict(projection, status='validated_projection', claim=claim,
                perspective_dropped=perspective_dropped)
