import sqlite3
from datetime import datetime
from typing import Optional
import os

DB_PATH = os.getenv("DB_PATH", "events.db")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT,
                date TEXT,
                time TEXT,
                venue TEXT,
                address TEXT,
                url TEXT UNIQUE,
                image_url TEXT,
                source TEXT,
                category TEXT,
                scraped_at TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.commit()


def upsert_event(
    title: str,
    url: str,
    source: str,
    description: Optional[str] = None,
    date: Optional[str] = None,
    time: Optional[str] = None,
    venue: Optional[str] = None,
    address: Optional[str] = None,
    image_url: Optional[str] = None,
    category: Optional[str] = None,
):
    """Insert or update an event by URL (unique key)."""
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO events (title, description, date, time, venue, address, url, image_url, source, category, scraped_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(url) DO UPDATE SET
                title=excluded.title,
                description=excluded.description,
                date=excluded.date,
                time=excluded.time,
                venue=excluded.venue,
                address=excluded.address,
                image_url=excluded.image_url,
                category=excluded.category,
                scraped_at=excluded.scraped_at
        """, (title, description, date, time, venue, address, url, image_url, source, category))
        conn.commit()


def get_all_events(limit: int = 200) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM events
            ORDER BY
                CASE WHEN date IS NULL THEN 1 ELSE 0 END,
                date ASC,
                scraped_at DESC
            LIMIT ?
        """, (limit,)).fetchall()
    return [dict(row) for row in rows]
