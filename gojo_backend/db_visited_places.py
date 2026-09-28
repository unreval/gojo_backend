"""db_visited_places.py —— 角色探店记录表

角色日程里去了哪些真实店铺 → 存这里 → 地图上"亮灯"
"""
from db import get_conn


def init_visited_places_table():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('''CREATE TABLE IF NOT EXISTS char_visited_places (
        id SERIAL PRIMARY KEY,
        character_id TEXT NOT NULL,
        user_id TEXT NOT NULL DEFAULT 'default',
        place_name TEXT NOT NULL,
        place_address TEXT DEFAULT '',
        lat FLOAT NOT NULL,
        lng FLOAT NOT NULL,
        category TEXT DEFAULT 'cafe',
        city TEXT DEFAULT 'tokyo',
        char_review TEXT DEFAULT '',
        visit_date DATE,
        osm_id BIGINT,
        provider TEXT,
        provider_place_id TEXT,
        verified_category TEXT,
        provider_raw_type JSONB,
        fetched_at TIMESTAMPTZ,
        schedule_event_id INTEGER,
        provenance TEXT DEFAULT 'manual',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    cur.execute('ALTER TABLE char_visited_places ADD COLUMN IF NOT EXISTS provider TEXT')
    cur.execute('ALTER TABLE char_visited_places ADD COLUMN IF NOT EXISTS provider_place_id TEXT')
    cur.execute('ALTER TABLE char_visited_places ADD COLUMN IF NOT EXISTS verified_category TEXT')
    cur.execute('ALTER TABLE char_visited_places ADD COLUMN IF NOT EXISTS provider_raw_type JSONB')
    cur.execute('ALTER TABLE char_visited_places ADD COLUMN IF NOT EXISTS fetched_at TIMESTAMPTZ')
    cur.execute('ALTER TABLE char_visited_places ADD COLUMN IF NOT EXISTS schedule_event_id INTEGER')
    cur.execute("ALTER TABLE char_visited_places ADD COLUMN IF NOT EXISTS provenance TEXT DEFAULT 'manual'")
    cur.execute('''CREATE INDEX IF NOT EXISTS idx_visited_user_char
                   ON char_visited_places (user_id, character_id)''')
    cur.execute('''CREATE UNIQUE INDEX IF NOT EXISTS idx_visited_schedule_event
                   ON char_visited_places (character_id, user_id, schedule_event_id)
                   WHERE schedule_event_id IS NOT NULL''')
    conn.commit()
    cur.close()
    conn.close()
    print('[init] 探店记录表已就绪：char_visited_places')


def add_visited(character_id, user_id, place, review='', visit_date=None,
                schedule_event_id=None, provenance='manual'):
    """Record a completed visit, never a merely planned stop.

    `place` can be a resolver POI or a legacy manually entered place. Schedule
    completion always passes resolver provenance and an event id, which makes
    retries idempotent and keeps planned places separate from visited places.
    """
    import json
    conn = get_conn()
    cur = conn.cursor()
    name = place.get('canonical_name') or place.get('name', '')
    address = place.get('canonical_address') or place.get('address', '')
    category = place.get('verified_category') or place.get('category', 'cafe')
    cur.execute(
        '''INSERT INTO char_visited_places
           (character_id, user_id, place_name, place_address, lat, lng,
            category, city, char_review, visit_date, osm_id, provider,
            provider_place_id, verified_category, provider_raw_type, fetched_at,
            schedule_event_id, provenance)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                   %s, %s, %s, %s::jsonb, %s, %s, %s)
           ON CONFLICT (character_id, user_id, schedule_event_id)
             WHERE schedule_event_id IS NOT NULL
             DO NOTHING
           RETURNING id''',
        (character_id, user_id,
         name, address,
         place.get('lat', 0), place.get('lng', 0),
         category, place.get('city', 'tokyo'),
         review, visit_date, place.get('osm_id'),
         place.get('provider'), place.get('provider_place_id'),
         place.get('verified_category'),
         json.dumps(place.get('provider_raw_type') or {}, ensure_ascii=False)
         if place.get('provider_raw_type') is not None else None,
         place.get('fetched_at'), schedule_event_id, provenance)
    )
    row = cur.fetchone()
    new_id = row[0] if row else None
    conn.commit()
    cur.close()
    conn.close()
    return new_id


def list_visited(user_id, character_id=None, city=None, limit=200):
    """列出探店记录(地图打点用)。"""
    conn = get_conn()
    cur = conn.cursor()
    sql = '''SELECT id, character_id, place_name, place_address, lat, lng,
                    category, city, char_review, visit_date, created_at,
                    provider, provider_place_id, verified_category,
                    schedule_event_id, provenance
             FROM char_visited_places WHERE user_id=%s'''
    params = [user_id]
    if character_id:
        sql += ' AND character_id=%s'
        params.append(character_id)
    if city:
        sql += ' AND city=%s'
        params.append(city)
    sql += ' ORDER BY visit_date DESC NULLS LAST, created_at DESC LIMIT %s'
    params.append(limit)
    cur.execute(sql, params)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [{
        'id': r[0], 'character_id': r[1],
        'place_name': r[2], 'place_address': r[3],
        'lat': r[4], 'lng': r[5],
        'category': r[6], 'city': r[7],
        'char_review': r[8] or '',
        'visit_date': str(r[9]) if r[9] else None,
        'created_at': str(r[10]) if r[10] else None,
        'provider': r[11], 'provider_place_id': r[12],
        'verified_category': r[13], 'schedule_event_id': r[14],
        'provenance': r[15] or 'manual',
    } for r in rows]


def count_visited(user_id, character_id=None):
    conn = get_conn()
    cur = conn.cursor()
    if character_id:
        cur.execute('SELECT COUNT(*) FROM char_visited_places WHERE user_id=%s AND character_id=%s',
                    (user_id, character_id))
    else:
        cur.execute('SELECT COUNT(*) FROM char_visited_places WHERE user_id=%s', (user_id,))
    n = cur.fetchone()[0]
    cur.close()
    conn.close()
    return int(n or 0)
