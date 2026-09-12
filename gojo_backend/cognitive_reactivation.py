"""Dormant-question ranking with JSON/TEXT embeddings and in-process cosine."""
import json
import math

from cognitive_config import (
    COGNITIVE_MAX_QUESTIONS_PER_CYCLE,
    COGNITIVE_REACTIVATION_COSINE_THRESHOLD,
)
from cognitive_triggers import create_trigger_occurrence


def parse_embedding(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return None
    if not isinstance(value, (list, tuple)) or not value:
        return None
    try:
        return [float(item) for item in value]
    except (TypeError, ValueError):
        return None


def cosine_similarity(left, right):
    a = parse_embedding(left)
    b = parse_embedding(right)
    if not a or not b or len(a) != len(b):
        return None
    norm_a = math.sqrt(sum(value * value for value in a))
    norm_b = math.sqrt(sum(value * value for value in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return None
    return sum(x * y for x, y in zip(a, b)) / (norm_a * norm_b)


def rank_dormant_questions(
    evidence_embedding,
    questions,
    *,
    threshold=None,
    top_n=None,
):
    cutoff = (
        COGNITIVE_REACTIVATION_COSINE_THRESHOLD
        if threshold is None
        else float(threshold)
    )
    limit = (
        COGNITIVE_MAX_QUESTIONS_PER_CYCLE if top_n is None else int(top_n)
    )
    ranked = []
    for question in questions:
        if question.get('status') != 'dormant':
            continue
        similarity = cosine_similarity(
            evidence_embedding, question.get('embedding_json'),
        )
        if similarity is None or similarity < cutoff:
            continue
        ranked.append({
            'question_id': question['id'],
            'question_key': question.get('question_key'),
            'question_text': question.get('question_text'),
            'similarity': similarity,
        })

    ranked.sort(key=lambda item: (-item['similarity'], item['question_id']))
    return ranked[:limit]


def reactivate_dormant_questions(
    conn,
    *,
    user_id,
    character_id,
    event_id,
    evidence_embedding,
    threshold=None,
    top_n=None,
):
    """Create possibly-related occurrences without changing question status."""
    if parse_embedding(evidence_embedding) is None:
        return []

    cur = conn.cursor()
    try:
        cur.execute(
            '''SELECT id, question_key, question_text, status, embedding_json
               FROM cognitive_questions
               WHERE user_id = %s AND character_id = %s
                 AND status = 'dormant' AND embedding_json IS NOT NULL''',
            (user_id, character_id),
        )
        questions = [
            {
                'id': row[0],
                'question_key': row[1],
                'question_text': row[2],
                'status': row[3],
                'embedding_json': row[4],
            }
            for row in cur.fetchall()
        ]
    finally:
        cur.close()

    ranked = rank_dormant_questions(
        evidence_embedding,
        questions,
        threshold=threshold,
        top_n=top_n,
    )
    for item in ranked:
        create_trigger_occurrence(
            conn,
            event_id=event_id,
            user_id=user_id,
            character_id=character_id,
            trigger_class='question_reactivation',
            occurrence_key=f'question:{item["question_id"]}',
            payload={
                'question_id': item['question_id'],
                'similarity': item['similarity'],
                'relation': 'possibly_related',
            },
        )
    return ranked
