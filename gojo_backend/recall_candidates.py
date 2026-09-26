"""Recall candidate adapter + provenance collapse (Recall/Search v2 Phase 2).

Does not change two_level_recall ranking weights. Adapts existing rows into a
unified candidate, attaches provenance when it already exists, and collapses
duplicate *conclusions* that share sources. Never invents source_event_ids.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

_FRAG_SPLIT = re.compile(r'[，。,.\s;；、！!？?：:\n]+')
_CJK_CHUNK = re.compile(r'[\u4e00-\u9fff]{2,}')

TYPE_ROLE = {
    'episode_index': 'factual',
    'episodic': 'factual',
    'fact': 'factual',
    'habit': 'factual',
    'lifecycle': 'factual',
    'rolling_summary': 'factual',
    'pinned': 'factual',
    'bond': 'relational',
    'told': 'relational',
    'diary': 'subjective',
    'cognitive_question': 'unresolved',
    'cognitive_hypothesis': 'unresolved',
    'cognitive_sticky': 'unresolved',
    'cognitive_prediction_error': 'unresolved',
}

FACTUAL_RANK = {
    'fact': 0,
    'episode_index': 1,
    'episodic': 1,
    'habit': 2,
    'lifecycle': 3,
    'bond': 4,
    'told': 5,
    'pinned': 6,
    'rolling_summary': 7,
}


def _fragments(text: str):
    parts = [p for p in _FRAG_SPLIT.split(text or '') if len(p) >= 2]
    return parts or _CJK_CHUNK.findall(text or '')


def text_overlap(a: str, b: str) -> float:
    fa, fb = set(_fragments(a)), set(_fragments(b))
    if not fa or not fb:
        return 0.0
    return len(fa & fb) / float(max(1, min(len(fa), len(fb))))


def parse_source_ids(value) -> Tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (list, tuple, set)):
        raw = value
    else:
        text = str(value).strip()
        if not text:
            return ()
        try:
            import json
            parsed = json.loads(text)
            raw = parsed if isinstance(parsed, list) else [text]
        except Exception:
            raw = [text]
    out = []
    for item in raw:
        if isinstance(item, dict):
            eid = str(item.get('event_id') or item.get('source_id') or '').strip()
        else:
            eid = str(item).strip()
        if eid:
            out.append(eid)
    seen = []
    for eid in out:
        if eid not in seen:
            seen.append(eid)
    return tuple(seen)


@dataclass
class RecallCandidate:
    candidate_id: str
    candidate_type: str
    text: str
    source_event_ids: Tuple[str, ...] = ()
    source_object_id: str = ''
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    relevance_score: float = 0.0
    priority: int = 50
    confidence: float = 0.5
    subjective: bool = False
    lifecycle_state: str = 'active'
    provenance_quality: str = 'legacy_missing'
    recent_overlap_ratio: float = 0.0
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        self.source_event_ids = parse_source_ids(self.source_event_ids)
        if self.source_event_ids:
            if self.provenance_quality == 'legacy_missing':
                self.provenance_quality = 'linked'
        else:
            self.provenance_quality = 'legacy_missing'
        role = TYPE_ROLE.get(self.candidate_type, 'factual')
        self.metadata.setdefault('semantic_role', role)
        if self.candidate_type in ('diary',) or self.metadata.get('semantic_role') == 'subjective':
            self.subjective = True

    @property
    def semantic_role(self) -> str:
        return self.metadata.get('semantic_role') or TYPE_ROLE.get(self.candidate_type, 'factual')


def annotate_recent_overlap(item: dict, recent_ids) -> dict:
    """Mutate item with source_event_ids / recent_overlap_ratio. Do not invent ids."""
    recent = {str(x).strip() for x in (recent_ids or []) if str(x).strip()}
    ids = parse_source_ids(item.get('source_event_ids'))
    if not ids:
        ids = parse_source_ids(item.get('source_event_refs'))
    item['source_event_ids'] = ids
    if ids:
        item['provenance_quality'] = item.get('provenance_quality') or 'linked'
    else:
        item['provenance_quality'] = 'legacy_missing'
    if not ids or not recent:
        item['recent_overlap_ratio'] = 0.0
        return item
    overlap = set(ids) & recent
    item['recent_overlap_ratio'] = len(overlap) / float(len(ids))
    return item


def is_fully_covered_by_recent(item: dict, recent_ids) -> bool:
    annotate_recent_overlap(item, recent_ids)
    ids = set(item.get('source_event_ids') or ())
    recent = {str(x).strip() for x in (recent_ids or []) if str(x).strip()}
    return bool(ids) and ids <= recent


def drop_recent_covered(items: Sequence[dict], recent_ids) -> List[dict]:
    kept = []
    for item in items or []:
        row = dict(item)
        if is_fully_covered_by_recent(row, recent_ids):
            continue
        kept.append(row)
    return kept


def load_memory_source_map(cur, memory_type: str, memory_ids: Sequence) -> Dict[int, Tuple[str, ...]]:
    ids = []
    for mid in memory_ids or []:
        try:
            ids.append(int(mid))
        except (TypeError, ValueError):
            continue
    if not ids or cur is None:
        return {}
    placeholders = ','.join(['%s'] * len(ids))
    cur.execute(
        f'''SELECT memory_id, source_event_id
            FROM memory_source_events
            WHERE memory_type=%s AND memory_id IN ({placeholders})''',
        (memory_type, *ids),
    )
    buckets: Dict[int, List[str]] = {}
    for memory_id, event_id in cur.fetchall():
        eid = str(event_id or '').strip()
        if not eid:
            continue
        buckets.setdefault(int(memory_id), []).append(eid)
    return {key: tuple(dict.fromkeys(val)) for key, val in buckets.items()}


def attach_row_provenance(row: dict, source_map: Dict[int, Tuple[str, ...]]) -> dict:
    extra = []
    mid = row.get('id')
    try:
        extra.extend(source_map.get(int(mid), ()) if mid is not None else ())
    except (TypeError, ValueError):
        pass
    refs = parse_source_ids(row.get('source_event_ids')) + parse_source_ids(row.get('source_event_refs'))
    ids = tuple(dict.fromkeys(list(extra) + list(refs)))
    row['source_event_ids'] = ids
    row['provenance_quality'] = 'linked' if ids else 'legacy_missing'
    return row


def _candidate_from_row(kind, row, extra=None) -> Optional[RecallCandidate]:
    if not isinstance(row, dict):
        return None
    text = (row.get('content') or row.get('text') or '').strip()
    if not text:
        return None
    ids = parse_source_ids(row.get('source_event_ids')) or parse_source_ids(row.get('source_event_refs'))
    oid = str(row.get('id') or row.get('diary_key') or row.get('note_key') or '')
    meta = dict(row)
    if extra:
        meta.update(extra)
    return RecallCandidate(
        candidate_id=f'{kind}:{oid or abs(hash(text)) % 10**8}',
        candidate_type=kind,
        text=text,
        source_event_ids=ids,
        source_object_id=oid,
        created_at=row.get('timestamp') or row.get('created_at'),
        updated_at=row.get('updated_at') or row.get('timestamp'),
        relevance_score=float(row.get('score') or 0),
        priority=int(extra.get('priority') if extra and extra.get('priority') is not None else 50),
        subjective=kind == 'diary',
        provenance_quality=row.get('provenance_quality') or ('linked' if ids else 'legacy_missing'),
        recent_overlap_ratio=float(row.get('recent_overlap_ratio') or 0),
        metadata=meta,
    )


def from_recall_result(recall_result) -> List[RecallCandidate]:
    if not recall_result:
        return []
    out = []
    for fact in recall_result.get('facts') or []:
        cand = _candidate_from_row('fact', fact, {'priority': 70})
        if cand:
            out.append(cand)
        for bond in fact.get('bonds') or []:
            linked = dict(bond)
            if not linked.get('source_event_ids') and fact.get('source_event_ids'):
                linked['source_event_ids'] = fact.get('source_event_ids')
                linked['provenance_quality'] = fact.get('provenance_quality')
            bc = _candidate_from_row('bond', linked, {'priority': 45, 'linked_fact_id': fact.get('id')})
            if bc:
                out.append(bc)
    for row in recall_result.get('loose_bonds') or []:
        cand = _candidate_from_row('bond', row, {'priority': 45})
        if cand:
            out.append(cand)
    for row in recall_result.get('tolds') or []:
        cand = _candidate_from_row('told', row, {'priority': 45})
        if cand:
            out.append(cand)
    for row in recall_result.get('lifecycle_memories') or []:
        kind = 'episodic' if (row.get('memory_kind') or '') in ('episodic', 'consolidated') else 'lifecycle'
        cand = _candidate_from_row(kind, row, {'priority': 40})
        if cand:
            out.append(cand)
    for row in recall_result.get('episodes') or []:
        cand = _candidate_from_row('episode_index', row, {'priority': 50})
        if cand:
            out.append(cand)
    for row in recall_result.get('sticky_notes') or []:
        cand = _candidate_from_row('cognitive_sticky', row, {'priority': 30})
        if cand:
            out.append(cand)
    for row in recall_result.get('diary_memories') or []:
        cand = _candidate_from_row('diary', row, {'priority': 20})
        if cand:
            cand.subjective = True
            cand.metadata['semantic_role'] = 'subjective'
            out.append(cand)
    return out


def _source_related(a: RecallCandidate, b: RecallCandidate) -> bool:
    sa, sb = set(a.source_event_ids), set(b.source_event_ids)
    if not sa or not sb:
        return False
    # A partly-overlapping episode is still a different stretch of experience.
    # Only a complete source subset can compete for a deduplication decision.
    if 'episode_index' in (a.candidate_type, b.candidate_type):
        return sa <= sb or sb <= sa
    if sa & sb:
        if sa <= sb or sb <= sa:
            return True
        return len(sa & sb) / float(min(len(sa), len(sb))) >= 0.5
    return False


def _prefer(a: RecallCandidate, b: RecallCandidate) -> RecallCandidate:
    if a.subjective != b.subjective:
        return b if a.subjective else a
    ra = FACTUAL_RANK.get(a.candidate_type, 50)
    rb = FACTUAL_RANK.get(b.candidate_type, 50)
    if ra != rb:
        return a if ra < rb else b
    if a.provenance_quality != b.provenance_quality:
        return a if a.provenance_quality == 'linked' else b
    if a.relevance_score != b.relevance_score:
        return a if a.relevance_score > b.relevance_score else b
    return a


def _same_conclusion(a: RecallCandidate, b: RecallCandidate) -> bool:
    if 'episode_index' in (a.candidate_type, b.candidate_type):
        # An episode is broader than a fact/bond.  Keep both unless their text
        # is nearly the same conclusion, rather than erasing an independent
        # factual assertion that happens to cite one source in the episode.
        return text_overlap(a.text, b.text) >= 0.85
    if a.semantic_role != b.semantic_role:
        # factual vs relational may still be the same stated fact
        roles = {a.semantic_role, b.semantic_role}
        if roles != {'factual', 'relational'}:
            return False
        return text_overlap(a.text, b.text) >= 0.55
    if a.provenance_quality == 'legacy_missing' and b.provenance_quality == 'legacy_missing':
        return a.candidate_type == b.candidate_type and text_overlap(a.text, b.text) >= 0.85
    return text_overlap(a.text, b.text) >= 0.5


def collapse_candidates(candidates: Sequence[RecallCandidate]) -> List[RecallCandidate]:
    """Drop duplicate conclusions. Keep different semantic roles of the same source."""
    rows = [c for c in candidates if c and (c.text or '').strip()]
    n = len(rows)
    if n <= 1:
        return list(rows)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[rj] = ri

    for i in range(n):
        for j in range(i + 1, n):
            if not _source_related(rows[i], rows[j]) and not (
                rows[i].provenance_quality == 'legacy_missing'
                and rows[j].provenance_quality == 'legacy_missing'
                and _same_conclusion(rows[i], rows[j])
            ):
                continue
            if _same_conclusion(rows[i], rows[j]):
                union(i, j)

    groups: Dict[int, List[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)

    kept = []
    for indexes in groups.values():
        members = [rows[i] for i in indexes]
        winner = members[0]
        extras = []
        for other in members[1:]:
            if _same_conclusion(winner, other):
                winner = _prefer(winner, other)
            else:
                extras.append(other)
        kept.append(winner)
        kept.extend(extras)
    # uniqueness by candidate_id
    seen = set()
    out = []
    for cand in kept:
        if cand.candidate_id in seen:
            continue
        seen.add(cand.candidate_id)
        out.append(cand)
    return out


def to_recall_result(candidates: Sequence[RecallCandidate], original=None) -> dict:
    base = dict(original or {})
    facts, loose_bonds, tolds = [], [], []
    lifecycle, episodes, sticky, diary = [], [], [], []
    fact_by_id = {}
    for cand in candidates:
        raw = dict(cand.metadata or {})
        raw['content'] = cand.text
        raw['source_event_ids'] = cand.source_event_ids
        raw['provenance_quality'] = cand.provenance_quality
        raw['recent_overlap_ratio'] = cand.recent_overlap_ratio
        raw['subjective'] = cand.subjective
        if cand.candidate_type == 'fact':
            raw.setdefault('bonds', [])
            raw['bonds'] = []
            facts.append(raw)
            if raw.get('id') is not None:
                fact_by_id[raw['id']] = raw
        elif cand.candidate_type == 'bond':
            linked = (cand.metadata or {}).get('linked_fact_id')
            if linked in fact_by_id:
                fact_by_id[linked].setdefault('bonds', []).append(raw)
            else:
                loose_bonds.append(raw)
        elif cand.candidate_type == 'told':
            tolds.append(raw)
        elif cand.candidate_type in ('lifecycle', 'episodic', 'habit'):
            lifecycle.append(raw)
        elif cand.candidate_type == 'episode_index':
            episodes.append(raw)
        elif cand.candidate_type == 'cognitive_sticky':
            sticky.append(raw)
        elif cand.candidate_type == 'diary':
            diary.append(raw)
    base['facts'] = facts
    base['loose_bonds'] = loose_bonds
    base['tolds'] = tolds
    base['lifecycle_memories'] = lifecycle
    base['episodes'] = episodes
    base['sticky_notes'] = sticky
    base['diary_memories'] = diary
    base['collapsed'] = True
    return base
