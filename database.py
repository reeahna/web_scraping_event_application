import csv
import sqlite3
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
                end_date TEXT,
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
        conn.execute("ALTER TABLE events ADD COLUMN end_date TEXT" if not any(
            row[1] == "end_date"
            for row in conn.execute("PRAGMA table_info(events)").fetchall()
        ) else "SELECT 1")
        conn.commit()


def upsert_event(
    title: str,
    url: str,
    source: str,
    description: Optional[str] = None,
    date: Optional[str] = None,
    end_date: Optional[str] = None,
    time: Optional[str] = None,
    venue: Optional[str] = None,
    address: Optional[str] = None,
    image_url: Optional[str] = None,
    category: Optional[str] = None,
):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO events (title, description, date, end_date, time, venue, address, url, image_url, source, category, scraped_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(url) DO UPDATE SET
                title=excluded.title,
                description=excluded.description,
                date=excluded.date,
                end_date=excluded.end_date,
                time=excluded.time,
                venue=excluded.venue,
                address=excluded.address,
                image_url=excluded.image_url,
                category=excluded.category,
                scraped_at=excluded.scraped_at
        """, (title, description, date, end_date, time, venue, address, url, image_url, source, category))
        conn.commit()


def export_csv(path: str = "events.csv") -> None:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM events ORDER BY date ASC, scraped_at DESC").fetchall()
    if not rows:
        return
    dicts = [dict(r) for r in rows]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=dicts[0].keys())
        writer.writeheader()
        writer.writerows(dicts)


def get_all_events() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM events
            WHERE
                date IS NULL
                OR date = 'Multiple Dates'
                OR date >= date('now')
                OR (end_date IS NOT NULL AND end_date >= date('now'))
            ORDER BY
                CASE WHEN date IS NULL THEN 1 ELSE 0 END,
                date ASC,
                scraped_at DESC
        """).fetchall()
    return [dict(row) for row in rows]
