"""Behavioral observations and anomaly baselines.

Facts only. Never a relationship delta. Recalling an anomaly is not new
evidence and must not update baseline or confidence.
"""
from __future__ import annotations

import json
import math
import threading
import uuid
from datetime import datetime, timezone
from typing import Iterable, List, Optional, Sequence


OBSERVATION_TYPES = (
    'response_latency_ms',
    'seen_to_reply_ms',
    'message_length_chars',
    'defer_count',
    'phone_check_count',
    'proactive_count',
    'reply_turn_count',
    'time_to_seen_ms',
)

MIN_BASELINE_SAMPLES = 8
BASELINE_WINDOW = 80
ANOMALY_MAD_Z = 3.5

_LOCK = threading.Lock()
_USE_MEMORY = False
_OBS = []
_BASELINES = {}
_ANOMALIES = []


def use_memory_store(enabled=True):
    global _USE_MEMORY
    _USE_MEMORY = bool(enabled)
    if enabled:
        reset_memory_store()


def reset_memory_store():
    with _LOCK:
        _OBS.clear()
        _BASELINES.clear()
        _ANOMALIES.clear()


def init_behavior_tables():
    from db import get_conn
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute('''CREATE TABLE IF NOT EXISTS behavior_observations (
            observation_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            character_id TEXT NOT NULL,
            observation_type TEXT NOT NULL,
            value DOUBLE PRECISION NOT NULL,
            unit TEXT NOT NULL DEFAULT 'ms',
            busy_state TEXT NOT NULL DEFAULT 'free',
            interaction_mode TEXT NOT NULL DEFAULT 'text',
            source_user_event_id TEXT,
            source_assistant_event_id TEXT,
            seen_event_id TEXT,
            phone_check_id TEXT,
            observed_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            metadata TEXT NOT NULL DEFAULT '{}'
        )''')
        cur.execute('''CREATE INDEX IF NOT EXISTS idx_behavior_obs_lookup
                       ON behavior_observations
                       (character_id, busy_state, interaction_mode, observation_type, observed_at DESC)''')
        cur.execute('''CREATE TABLE IF NOT EXISTS behavior_baselines (
            character_id TEXT NOT NULL,
            busy_state TEXT NOT NULL,
            interaction_mode TEXT NOT NULL,
            observation_type TEXT NOT NULL,
            median DOUBLE PRECISION,
            p25 DOUBLE PRECISION,
            p75 DOUBLE PRECISION,
            mad DOUBLE PRECISION,
            sample_count INTEGER NOT NULL DEFAULT 0,
            window_start TIMESTAMPTZ,
            window_end TIMESTAMPTZ,
            updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (character_id, busy_state, interaction_mode, observation_type)
        )''')
        cur.execute('''CREATE TABLE IF NOT EXISTS behavior_anomalies (
            anomaly_id TEXT PRIMARY KEY,
            observation_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            character_id TEXT NOT NULL,
            observation_type TEXT NOT NULL,
            direction TEXT NOT NULL,
            magnitude DOUBLE PRECISION,
            confidence DOUBLE PRECISION,
            busy_state TEXT NOT NULL,
            interaction_mode TEXT NOT NULL,
            baseline_snapshot TEXT NOT NULL DEFAULT '{}',
            created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
        )''')
        conn.commit()
    finally:
        cur.close()
        conn.close()
        print('[behavior] tables ready')


def _now(now=None):
    value = now or datetime.now(timezone.utc)
    if getattr(value, 'tzinfo', None) is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _percentile(sorted_vals: Sequence[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    idx = (len(sorted_vals) - 1) * p
    lo = int(math.floor(idx))
    hi = int(math.ceil(idx))
    if lo == hi:
        return float(sorted_vals[lo])
    frac = idx - lo
    return float(sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac)


def robust_stats(values: Sequence[float]) -> dict:
    rows = sorted(float(v) for v in values if v is not None)
    n = len(rows)
    if not n:
        return {
            'median': 0.0, 'p25': 0.0, 'p75': 0.0, 'mad': 0.0, 'sample_count': 0,
        }
    median = _percentile(rows, 0.5)
    deviations = sorted(abs(v - median) for v in rows)
    mad = _percentile(deviations, 0.5)
    return {
        'median': median,
        'p25': _percentile(rows, 0.25),
        'p75': _percentile(rows, 0.75),
        'mad': mad,
        'sample_count': n,
    }


def _baseline_key(character_id, busy_state, mode, obs_type):
    return (character_id, busy_state or 'free', mode or 'text', obs_type)


def record_observation(
    user_id,
    character_id,
    observation_type,
    value,
    *,
    unit='ms',
    busy_state='free',
    interaction_mode='text',
    source_user_event_id=None,
    source_assistant_event_id=None,
    seen_event_id=None,
    phone_check_id=None,
    observed_at=None,
    metadata=None,
    update_baseline=True,
):
    if observation_type not in OBSERVATION_TYPES:
        raise ValueError(f'unknown observation_type: {observation_type}')
    if value is None:
        return None
    oid = f'obs:{uuid.uuid4()}'
    now = _now(observed_at)
    row = {
        'observation_id': oid,
        'user_id': user_id,
        'character_id': character_id,
        'observation_type': observation_type,
        'value': float(value),
        'unit': unit,
        'busy_state': busy_state or 'free',
        'interaction_mode': interaction_mode or 'text',
        'source_user_event_id': source_user_event_id,
        'source_assistant_event_id': source_assistant_event_id,
        'seen_event_id': seen_event_id,
        'phone_check_id': str(phone_check_id) if phone_check_id is not None else None,
        'observed_at': now,
        'metadata': dict(metadata or {}),
    }
    if _USE_MEMORY:
        with _LOCK:
            _OBS.append(row)
        anomaly = None
        if update_baseline:
            prev_values = _values_memory(
                character_id, row['busy_state'], row['interaction_mode'],
                observation_type)[:-1]
            prev_stats = robust_stats(prev_values)
            _refresh_baseline_memory(
                character_id, row['busy_state'], row['interaction_mode'],
                observation_type)
            anomaly = _maybe_anomaly_memory(row, baseline=prev_stats)
        return {'observation': row, 'anomaly': anomaly}

    prev_stats = None
    if update_baseline:
        try:
            prev_stats = get_baseline(
                character_id, row['busy_state'], row['interaction_mode'],
                observation_type)
        except Exception:
            prev_stats = {
                'median': 0.0, 'p25': 0.0, 'p75': 0.0, 'mad': 0.0, 'sample_count': 0,
            }
    try:
        from db import get_conn
        conn = get_conn()
        cur = conn.cursor()
        try:
            cur.execute(
                '''INSERT INTO behavior_observations
                   (observation_id, user_id, character_id, observation_type, value, unit,
                    busy_state, interaction_mode, source_user_event_id,
                    source_assistant_event_id, seen_event_id, phone_check_id,
                    observed_at, metadata)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                (oid, user_id, character_id, observation_type, float(value), unit,
                 row['busy_state'], row['interaction_mode'], source_user_event_id,
                 source_assistant_event_id, seen_event_id, row['phone_check_id'],
                 now, json.dumps(row['metadata'], ensure_ascii=False)))
            conn.commit()
        finally:
            cur.close()
            conn.close()
    except Exception as exc:
        print(f'[behavior] observation insert skipped:{exc}')
        return {'observation': row, 'anomaly': None}
    anomaly = None
    if update_baseline:
        try:
            refresh_baseline(
                character_id, row['busy_state'], row['interaction_mode'],
                observation_type)
        except Exception as exc:
            print(f'[behavior] baseline refresh skipped:{exc}')
        anomaly = maybe_record_anomaly(row, baseline=prev_stats)
    return {'observation': row, 'anomaly': anomaly}


def _values_memory(character_id, busy_state, mode, obs_type):
    with _LOCK:
        rows = [
            item['value'] for item in _OBS
            if item['character_id'] == character_id
            and item['busy_state'] == busy_state
            and item['interaction_mode'] == mode
            and item['observation_type'] == obs_type
        ]
    return rows[-BASELINE_WINDOW:]


def _refresh_baseline_memory(character_id, busy_state, mode, obs_type):
    values = _values_memory(character_id, busy_state, mode, obs_type)
    stats = robust_stats(values)
    key = _baseline_key(character_id, busy_state, mode, obs_type)
    with _LOCK:
        _BASELINES[key] = stats
    return stats


def get_baseline(character_id, busy_state, mode, obs_type):
    if _USE_MEMORY:
        key = _baseline_key(character_id, busy_state, mode, obs_type)
        with _LOCK:
            cached = _BASELINES.get(key)
        return cached or _refresh_baseline_memory(character_id, busy_state, mode, obs_type)
    from db import get_conn
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            '''SELECT median, p25, p75, mad, sample_count
               FROM behavior_baselines
               WHERE character_id=%s AND busy_state=%s
                 AND interaction_mode=%s AND observation_type=%s''',
            (character_id, busy_state, mode, obs_type))
        row = cur.fetchone()
        if not row:
            return {'median': 0.0, 'p25': 0.0, 'p75': 0.0, 'mad': 0.0, 'sample_count': 0}
        return {
            'median': float(row[0] or 0), 'p25': float(row[1] or 0),
            'p75': float(row[2] or 0), 'mad': float(row[3] or 0),
            'sample_count': int(row[4] or 0),
        }
    finally:
        cur.close()
        conn.close()


def refresh_baseline(character_id, busy_state, mode, obs_type):
    if _USE_MEMORY:
        return _refresh_baseline_memory(character_id, busy_state, mode, obs_type)
    from db import get_conn
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            '''SELECT value FROM behavior_observations
               WHERE character_id=%s AND busy_state=%s
                 AND interaction_mode=%s AND observation_type=%s
               ORDER BY observed_at DESC LIMIT %s''',
            (character_id, busy_state, mode, obs_type, BASELINE_WINDOW))
        values = [float(r[0]) for r in cur.fetchall()]
        values.reverse()
        stats = robust_stats(values)
        cur.execute(
            '''INSERT INTO behavior_baselines
               (character_id, busy_state, interaction_mode, observation_type,
                median, p25, p75, mad, sample_count, updated_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,CURRENT_TIMESTAMP)
               ON CONFLICT (character_id, busy_state, interaction_mode, observation_type)
               DO UPDATE SET
                 median=EXCLUDED.median, p25=EXCLUDED.p25, p75=EXCLUDED.p75,
                 mad=EXCLUDED.mad, sample_count=EXCLUDED.sample_count,
                 updated_at=CURRENT_TIMESTAMP''',
            (character_id, busy_state, mode, obs_type,
             stats['median'], stats['p25'], stats['p75'], stats['mad'],
             stats['sample_count']))
        conn.commit()
        return stats
    finally:
        cur.close()
        conn.close()


def classify_anomaly(value, baseline, min_samples=MIN_BASELINE_SAMPLES):
    """Return anomaly dict or None. Does not interpret motive."""
    if not baseline or int(baseline.get('sample_count') or 0) < min_samples:
        return None
    median = float(baseline.get('median') or 0)
    mad = float(baseline.get('mad') or 0)
    if mad <= 1e-6:
        spread = max(1.0, abs(median) * 0.1, 1.0)
    else:
        spread = mad
    z = abs(float(value) - median) / (1.4826 * spread)
    if z < ANOMALY_MAD_Z:
        return None
    direction = 'faster' if float(value) < median else 'slower'
    if median and abs(median) > 0:
        magnitude = abs(float(value) - median) / abs(median)
    else:
        magnitude = z
    confidence = min(0.95, 0.45 + 0.1 * (z - ANOMALY_MAD_Z))
    return {
        'direction': direction,
        'magnitude': round(magnitude, 4),
        'confidence': round(confidence, 4),
        'z': round(z, 4),
    }


def _maybe_anomaly_memory(obs, baseline=None):
    baseline = baseline or get_baseline(
        obs['character_id'], obs['busy_state'],
        obs['interaction_mode'], obs['observation_type'])
    detected = classify_anomaly(obs['value'], baseline)
    if not detected:
        return None
    row = {
        'anomaly_id': f'an:{uuid.uuid4()}',
        'observation_id': obs['observation_id'],
        'user_id': obs['user_id'],
        'character_id': obs['character_id'],
        'observation_type': obs['observation_type'],
        'busy_state': obs['busy_state'],
        'interaction_mode': obs['interaction_mode'],
        'baseline_snapshot': dict(baseline),
        **detected,
    }
    with _LOCK:
        _ANOMALIES.append(row)
    return row


def maybe_record_anomaly(obs, baseline=None):
    if _USE_MEMORY:
        return _maybe_anomaly_memory(obs, baseline=baseline)
    baseline = baseline or get_baseline(
        obs['character_id'], obs['busy_state'],
        obs['interaction_mode'], obs['observation_type'])
    detected = classify_anomaly(obs['value'], baseline)
    if not detected:
        return None
    aid = f'an:{uuid.uuid4()}'
    try:
        from db import get_conn
        conn = get_conn()
        cur = conn.cursor()
        try:
            cur.execute(
                '''INSERT INTO behavior_anomalies
                   (anomaly_id, observation_id, user_id, character_id, observation_type,
                    direction, magnitude, confidence, busy_state, interaction_mode,
                    baseline_snapshot)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                (aid, obs['observation_id'], obs['user_id'], obs['character_id'],
                 obs['observation_type'], detected['direction'], detected['magnitude'],
                 detected['confidence'], obs['busy_state'], obs['interaction_mode'],
                 json.dumps(baseline, ensure_ascii=False)))
            conn.commit()
        finally:
            cur.close()
            conn.close()
    except Exception as exc:
        print(f'[behavior] anomaly persist skipped:{exc}')
    detected.update({
        'anomaly_id': aid,
        'observation_id': obs['observation_id'],
        'observation_type': obs['observation_type'],
        'busy_state': obs['busy_state'],
        'interaction_mode': obs['interaction_mode'],
    })
    return detected


def list_recent_anomalies(user_id, character_id, *, limit=6):
    """Read-only. Recalling these MUST NOT update baseline or confidence."""
    if _USE_MEMORY:
        with _LOCK:
            rows = [
                dict(item) for item in _ANOMALIES
                if item['user_id'] == user_id and item['character_id'] == character_id
            ]
        rows.sort(key=lambda item: item.get('created_at') or item.get('observation_id'), reverse=True)
        return rows[: max(1, int(limit or 6))]
    from db import get_conn
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            '''SELECT anomaly_id, observation_id, observation_type, direction,
                      magnitude, confidence, busy_state, interaction_mode, created_at
               FROM behavior_anomalies
               WHERE user_id=%s AND character_id=%s
               ORDER BY created_at DESC LIMIT %s''',
            (user_id, character_id, max(1, int(limit or 6))))
        return [
            {
                'anomaly_id': r[0], 'observation_id': r[1],
                'observation_type': r[2], 'direction': r[3],
                'magnitude': r[4], 'confidence': r[5],
                'busy_state': r[6], 'interaction_mode': r[7],
                'created_at': r[8],
            }
            for r in cur.fetchall()
        ]
    finally:
        cur.close()
        conn.close()


def recall_anomalies_without_reinforcement(user_id, character_id, *, limit=6):
    """Alias that documents the anti-self-reinforcement rule."""
    return list_recent_anomalies(user_id, character_id, limit=limit)


def ms_between(later, earlier):
    if later is None or earlier is None:
        return None
    try:
        a = _now(later)
        b = _now(earlier)
        return max(0.0, (a - b).total_seconds() * 1000.0)
    except Exception:
        return None


def record_reply_cycle(
    user_id,
    character_id,
    *,
    user_event_id=None,
    assistant_event_id=None,
    user_at=None,
    seen_at=None,
    replied_at=None,
    message_length=0,
    busy_state='free',
    interaction_mode='text',
    phone_check_id=None,
    defer_count=None,
):
    """Record measurable reply facts. Diary text must never call this."""
    replied_at = _now(replied_at)
    results = []
    latency = ms_between(replied_at, user_at)
    if latency is not None:
        results.append(record_observation(
            user_id, character_id, 'response_latency_ms', latency,
            busy_state=busy_state, interaction_mode=interaction_mode,
            source_user_event_id=user_event_id,
            source_assistant_event_id=assistant_event_id,
            seen_event_id=None,
            phone_check_id=phone_check_id,
            observed_at=replied_at,
        ))
    seen_ms = ms_between(replied_at, seen_at) if seen_at else None
    if seen_ms is not None:
        results.append(record_observation(
            user_id, character_id, 'seen_to_reply_ms', seen_ms,
            busy_state=busy_state, interaction_mode=interaction_mode,
            source_user_event_id=user_event_id,
            source_assistant_event_id=assistant_event_id,
            phone_check_id=phone_check_id,
            observed_at=replied_at,
        ))
    to_seen = ms_between(seen_at, user_at) if seen_at and user_at else None
    if to_seen is not None:
        results.append(record_observation(
            user_id, character_id, 'time_to_seen_ms', to_seen,
            busy_state=busy_state, interaction_mode=interaction_mode,
            source_user_event_id=user_event_id,
            phone_check_id=phone_check_id,
            observed_at=seen_at,
        ))
    if message_length:
        results.append(record_observation(
            user_id, character_id, 'message_length_chars', float(message_length),
            unit='chars', busy_state=busy_state, interaction_mode=interaction_mode,
            source_user_event_id=user_event_id,
            source_assistant_event_id=assistant_event_id,
            observed_at=replied_at,
        ))
    if defer_count is not None:
        results.append(record_observation(
            user_id, character_id, 'defer_count', float(defer_count),
            unit='count', busy_state=busy_state, interaction_mode=interaction_mode,
            phone_check_id=phone_check_id,
            observed_at=replied_at,
        ))
    return results
