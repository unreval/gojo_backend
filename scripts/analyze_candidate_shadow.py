"""Read shadow JSONL without a database/model; optional human labels add metrics.

Labels are JSONL objects with user_id, character_id, source_event_id,
module_version, should_remember (bool), and optional summary_correct (bool).
Only human labels determine recall/precision; yield alone is not recall.
"""
import argparse
from collections import Counter
import json
from pathlib import Path


def identity(row, source_id=None):
    return (row['user_id'], row['character_id'],
            source_id if source_id is not None else row['source_event_id'],
            row['module_version'])


def read_jsonl(path):
    for line_number, line in enumerate(Path(path).read_text(encoding='utf-8-sig').splitlines(), 1):
        if line.strip():
            try:
                yield json.loads(line)
            except ValueError as exc:
                raise ValueError(f'{path}:{line_number}: invalid JSONL') from exc


def analyze(rows, labels=()):
    candidates, batches, inputs = {}, {}, set()
    failures = Counter()
    for row in rows:
        if row.get('log_type') != 'memory_candidate_shadow':
            continue
        if row.get('kind') == 'candidate':
            # Stable ID allows merging rotated/replicated log exports.
            candidates.setdefault(row['record_id'], row)
        elif row.get('kind') == 'batch':
            batches[row['batch_id']] = row
            if row['status'] == 'succeeded':
                inputs.update(identity(row, sid) for sid in row['input_source_event_ids'])
        elif row.get('kind') == 'batch_failure':
            failures[(row['stage'], row['reason'])] += 1
    by_source = {identity(row): row for row in candidates.values()
                 if row.get('source_in_batch') and identity(row) in inputs}
    gate_counts = {gate: dict(Counter(row['gates'][gate]['status'] for row in candidates.values()))
                   for gate in ('G1', 'G2', 'G3', 'G4', 'G5', 'G6', 'G7', 'G8', 'G9')}
    expected_records = sum(row['candidate_count'] for row in batches.values()
                           if row['status'] == 'succeeded')
    result = {
        'successful_input_messages': len(inputs), 'candidate_records': len(candidates),
        'passed_candidates': sum(bool(row['accepted']) for row in candidates.values()),
        'perspective_output_empty': sum(row['perspective_output_empty'] for row in candidates.values()),
        'perspective_after_gate_empty': sum(row['perspective_empty'] for row in candidates.values()),
        'batch_status_counts': dict(Counter(row['status'] for row in batches.values())),
        'gate_status_counts': gate_counts,
        'batch_failure_counts': {f'{stage}:{reason}': count for (stage, reason), count in failures.items()},
        'candidate_rows_expected_from_summaries': expected_records,
        'candidate_log_row_difference': len(candidates) - expected_records,
        'human_metrics': None,
    }
    labels = {identity(row): row for row in labels}
    expected = {key for key, label in labels.items()
                if key in inputs and label.get('should_remember') is True}
    assessed = [row for key, row in by_source.items() if key in labels
                and isinstance(labels[key].get('summary_correct'), bool)]
    if labels:
        result['human_metrics'] = {
            'labelled_inputs': len(set(labels) & inputs), 'should_remember': len(expected),
            'candidate_recall': len(expected & by_source.keys()) / len(expected) if expected else None,
            'passed_recall': sum(bool(by_source[key]['accepted']) for key in expected & by_source.keys())
                             / len(expected) if expected else None,
            'assessed_summaries': len(assessed),
            'summary_precision': sum(labels[identity(row)]['summary_correct'] for row in assessed)
                                 / len(assessed) if assessed else None,
        }
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('log', help='JSONL lines whose log_type is memory_candidate_shadow')
    parser.add_argument('--labels', help='Optional human-label JSONL')
    args = parser.parse_args()
    result = analyze(read_jsonl(args.log), read_jsonl(args.labels) if args.labels else ())
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
