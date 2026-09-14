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
                      cycle_summary, belief_updates, hypothesis_updates,
                      new_predictions, evidence_refs,
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
                'belief_updates': _json_value(row[6], []),
                'hypothesis_updates': _json_value(row[7], []),
                'new_predictions': _json_value(row[8], []),
                'evidence_refs': _json_value(row[9], []),
                'sticky_note_updates': _json_value(row[10], []),
                'diary_entries': _json_value(row[11], []),
                'worker_model': row[12],
                'failure_code': row[13],
                'queued_at': row[14],
                'started_at': row[15],
                'completed_at': row[16],
            })

        cur.execute(
            '''SELECT belief_key, statement, confidence, status,
                      evidence_refs, updated_by_cycle_id, updated_at
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
                'evidence_refs': _json_value(row[4], []),
                'updated_by_cycle_id': row[5],
                'updated_at': row[6],
            }
            for row in cur.fetchall()
        ]

        cur.execute(
            '''SELECT hypothesis_key, statement, status, evidence,
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
                'evidence': _json_value(row[3], []),
                'updated_by_cycle_id': row[4],
                'updated_at': row[5],
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
            '''SELECT note_key, content, status, source_event_refs,
                      expires_at, completed_at, updated_at
               FROM cognitive_sticky_notes
               WHERE user_id = %s AND character_id = %s
               ORDER BY updated_at DESC, id DESC''',
            (user_id, character_id),
        )
        sticky_notes = [
            {
                'note_key': row[0],
                'content': row[1],
                'status': row[2],
                'source_event_refs': _json_value(row[3], []),
                'expires_at': row[4],
                'completed_at': row[5],
                'updated_at': row[6],
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
