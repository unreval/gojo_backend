"""Disposable memory schema for SQL authority acceptance."""
from memory_authority import MEMORY_AUTHORITY_DDL


def create_memory_tables(cur):
    cur.execute("""CREATE TABLE long_memory (
        id SERIAL PRIMARY KEY,user_id TEXT,character_id TEXT,content TEXT,category TEXT,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP, lifecycle_kind TEXT DEFAULT 'long_fact',
        recall_status TEXT DEFAULT 'active',recall_weight REAL DEFAULT 1,
        expires_at TIMESTAMPTZ,source_event_refs JSONB DEFAULT '[]',
        mention_count INTEGER DEFAULT 1,last_mentioned TIMESTAMP,pinned BOOLEAN DEFAULT FALSE)""")
    cur.execute("""CREATE TABLE bond_memory (
        id SERIAL PRIMARY KEY,user_id TEXT,character_id TEXT,kind TEXT DEFAULT 'between',content TEXT,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,recall_status TEXT DEFAULT 'active',
        expires_at TIMESTAMPTZ,linked_fact_id INTEGER)""")
    cur.execute("""CREATE TABLE memory_source_events (
        memory_type TEXT,memory_id INTEGER,source_event_id TEXT,
        UNIQUE(memory_type,memory_id,source_event_id))""")
    for ddl in MEMORY_AUTHORITY_DDL:
        cur.execute(ddl)
