"""Read-only replacement for the retired model-scored relationship backfill.

The model prompt, estimate_baseline, apply_baseline and mutating CLI have been
removed. Historical records may be inspected, never promoted into cognition
or relationship scores. New judgments use canonical evidence ingress.
"""
import argparse
import json
from typing import Dict

from db import get_conn
from memory_authority import authoritative_memory_sql


def collect_history(user_id: str, character_id: str) -> Dict:
    """Read current canonical memory for inspection; never infer a relationship."""
    conn = get_conn()
    cur = conn.cursor()

    # Only current, qualified canonical reports and actual utterances.
    cur.execute(f'''SELECT content, category, timestamp FROM long_memory
                   WHERE user_id = %s AND character_id IN (%s, 'shared')
                     AND {authoritative_memory_sql('long_memory')}
                   ORDER BY timestamp ASC''',
                (user_id, character_id))
    long_mem = [{'content': r[0], 'category': r[1], 'ts': str(r[2])} for r in cur.fetchall()]

    cur.execute(f'''SELECT content, timestamp FROM bond_memory
                   WHERE user_id = %s AND character_id = %s AND kind = 'between'
                     AND {authoritative_memory_sql('bond_memory')}
                   ORDER BY timestamp ASC''',
                (user_id, character_id))
    bond_between = [{'content': r[0], 'ts': str(r[1])} for r in cur.fetchall()]

    cur.execute(f'''SELECT content, timestamp FROM bond_memory
                   WHERE user_id = %s AND character_id = %s AND kind = 'told'
                     AND {authoritative_memory_sql('bond_memory')}
                   ORDER BY timestamp ASC''',
                (user_id, character_id))
    bond_told = [{'content': r[0], 'ts': str(r[1])} for r in cur.fetchall()]

    # Descriptive history only; counts never set relationship scores.
    cur.execute(f'''SELECT COALESCE(total_days, 1) FROM user_stats WHERE user_id = %s''',
                (user_id,))
    row = cur.fetchone()
    total_days = row[0] if row else 1

    cur.close()
    conn.close()

    return {
        'long_memory': long_mem,
        'bond_between': bond_between,
        'bond_told': bond_told,
        'total_days': total_days,
    }


def main():
    parser = argparse.ArgumentParser(description='Inspect canonical memory; relationship backfill is retired')
    parser.add_argument('--user_id', required=True)
    parser.add_argument('--character_id', required=True)
    args = parser.parse_args()
    print(json.dumps(collect_history(args.user_id, args.character_id),
                     ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
