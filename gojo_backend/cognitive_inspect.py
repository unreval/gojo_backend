"""Read-only inspection of persisted Slow Loop conclusions."""
import argparse
import json


def _json_value(value, fallback):
    if value is None:
        return fallback
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def fetch_cognitive_snapshot(
    user_id, character_id='gojo', *, limit=5, conn=None,
):
    """Return auditable cycle outputs and current cognitive state."""
    owns_connection = conn is None
    if conn is None:
        from db import get_conn
        conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            '''SELECT id, status, primary_trigger_class,
                      input_state_version, output_state_version,
                      cycle_summary, question_updates, belief_updates,
                      belief_commit_decisions, hypothesis_updates,
                      new_predictions, evidence_refs, reflection_note,
                      sticky_note_updates, diary_entries, worker_model,
                      failure_code, queued_at, started_at, completed_at
               FROM cognitive_cycles
               WHERE user_id = %s AND character_id = %s
               ORDER BY id DESC
               LIMIT %s''',
            (user_id, character_id, max(1, min(int(limit), 50))),
        )
        cycles = []
        for row in cur.fetchall():
            cycles.append({
                'cycle_id': row[0],
                'status': row[1],
                'primary_trigger_class': row[2],
                'input_state_version': row[3],
                'output_state_version': row[4],
                'cycle_summary': _json_value(row[5], None),
                'question_updates': _json_value(row[6], []),
                'belief_updates': _json_value(row[7], []),
                'belief_commit_decisions': _json_value(row[8], []),
                'hypothesis_updates': _json_value(row[9], []),
                'new_predictions': _json_value(row[10], []),
                'evidence_refs': _json_value(row[11], []),
                'reflection_note': _json_value(row[12], None),
                'sticky_note_updates': _json_value(row[13], []),
                'diary_entries': _json_value(row[14], []),
                'worker_model': row[15],
                'failure_code': row[16],
                'queued_at': row[17],
                'started_at': row[18],
                'completed_at': row[19],
            })

        cur.execute(
            '''SELECT question_key, question_text, status, source_event_refs,
                      updated_by_cycle_id, updated_at
               FROM cognitive_questions
               WHERE user_id = %s AND character_id = %s
               ORDER BY updated_at DESC, id DESC''',
            (user_id, character_id),
        )
        questions = [
            {
                'question_key': row[0],
                'question_text': row[1],
                'status': row[2],
                'source_event_refs': _json_value(row[3], []),
                'updated_by_cycle_id': row[4],
                'updated_at': row[5],
            }
            for row in cur.fetchall()
        ]

        cur.execute(
            '''SELECT belief_key, statement, confidence, status, belief_type,
                      evidence_refs, committed_from_hypothesis_id,
                      metadata, updated_by_cycle_id, updated_at
               FROM cognitive_beliefs
               WHERE user_id = %s AND character_id = %s
               ORDER BY updated_at DESC, id DESC''',
            (user_id, character_id),
        )
        beliefs = [
            {
                'belief_key': row[0],
                'statement': row[1],
                'confidence': row[2],
                'status': row[3],
                'belief_type': row[4],
                'evidence_refs': _json_value(row[5], []),
                'committed_from_hypothesis_id': row[6],
                'metadata': _json_value(row[7], {}),
                'updated_by_cycle_id': row[8],
                'updated_at': row[9],
            }
            for row in cur.fetchall()
        ]

        cur.execute(
            '''SELECT hypothesis_key, statement, status, hypothesis_type,
                      confidence, supporting_evidence_refs,
                      contradicting_evidence_refs, evidence,
                      updated_by_cycle_id, updated_at
               FROM cognitive_hypotheses
               WHERE user_id = %s AND character_id = %s
               ORDER BY updated_at DESC, id DESC''',
            (user_id, character_id),
        )
        hypotheses = [
            {
                'hypothesis_key': row[0],
                'statement': row[1],
                'status': row[2],
                'hypothesis_type': row[3],
                'confidence': row[4],
                'supporting_evidence_refs': _json_value(row[5], []),
                'contradicting_evidence_refs': _json_value(row[6], []),
                'evidence': _json_value(row[7], []),
                'updated_by_cycle_id': row[8],
                'updated_at': row[9],
            }
            for row in cur.fetchall()
        ]

        cur.execute(
            '''SELECT prediction_key, status, resolver_name,
                      fulfillment_operator, fulfillment_value,
                      violation_operator, violation_value, observed_value,
                      expires_at, settled_at, created_by_cycle_id, metadata
               FROM cognitive_predictions
               WHERE user_id = %s AND character_id = %s
               ORDER BY created_at DESC, id DESC''',
            (user_id, character_id),
        )
        predictions = [
            {
                'prediction_key': row[0],
                'status': row[1],
                'resolver_name': row[2],
                'fulfillment_operator': row[3],
                'fulfillment_value': row[4],
                'violation_operator': row[5],
                'violation_value': row[6],
                'observed_value': row[7],
                'expires_at': row[8],
                'settled_at': row[9],
                'created_by_cycle_id': row[10],
                'metadata': _json_value(row[11], {}),
            }
            for row in cur.fetchall()
        ]
        cur.execute(
            '''SELECT id, note_key, content, status, source,
                      source_event_refs, created_by_cycle_id,
                      updated_by_cycle_id, expires_at, completed_at,
                      viewed, viewed_at, user_hidden_at, updated_at
               FROM cognitive_sticky_notes
               WHERE user_id = %s AND character_id = %s
               ORDER BY updated_at DESC, id DESC''',
            (user_id, character_id),
        )
        sticky_notes = [
            {
                'id': row[0],
                'note_key': row[1],
                'content': row[2],
                'status': row[3],
                'source': row[4],
                'source_event_refs': _json_value(row[5], []),
                'created_by_cycle_id': row[6],
                'updated_by_cycle_id': row[7],
                'expires_at': row[8],
                'completed_at': row[9],
                'viewed': bool(row[10]),
                'viewed_at': row[11],
                'user_hidden_at': row[12],
                'updated_at': row[13],
            }
            for row in cur.fetchall()
        ]
        cur.execute(
            '''SELECT diary_key, content, reflection_kind,
                      source_event_refs, occurred_at, created_at
               FROM cognitive_diary_entries
               WHERE user_id = %s AND character_id = %s
               ORDER BY occurred_at DESC, id DESC''',
            (user_id, character_id),
        )
        diary_entries = [
            {
                'diary_key': row[0],
                'content': row[1],
                'reflection_kind': row[2],
                'source_event_refs': _json_value(row[3], []),
                'occurred_at': row[4],
                'created_at': row[5],
            }
            for row in cur.fetchall()
        ]
        return {
            'user_id': user_id,
            'character_id': character_id,
            'cycles': cycles,
            'questions': questions,
            'beliefs': beliefs,
            'hypotheses': hypotheses,
            'predictions': predictions,
            'sticky_notes': sticky_notes,
            'diary_entries': diary_entries,
        }
    finally:
        cur.close()
        if owns_connection:
            conn.close()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Print persisted Slow Loop conclusions as JSON.',
    )
    parser.add_argument('--user-id', required=True)
    parser.add_argument('--character-id', default='gojo')
    parser.add_argument('--limit', type=int, default=5)
    args = parser.parse_args(argv)
    snapshot = fetch_cognitive_snapshot(
        args.user_id, args.character_id, limit=args.limit,
    )
    print(json.dumps(snapshot, ensure_ascii=False, indent=2, default=str))


if __name__ == '__main__':
    main()
