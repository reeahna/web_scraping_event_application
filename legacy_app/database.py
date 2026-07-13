import csv
import re
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
                city TEXT DEFAULT 'Bloomington, IN',
                scraped_at TEXT DEFAULT (datetime('now'))
            )
        """)
        existing = {row[1] for row in conn.execute("PRAGMA table_info(events)").fetchall()}
        for col, defn in [
            ("end_date", "TEXT"),
            ("city", "TEXT DEFAULT 'Bloomington, IN'"),
            ("lat", "REAL"),
            ("lng", "REAL"),
        ]:
            if col not in existing:
                conn.execute(f"ALTER TABLE events ADD COLUMN {col} {defn}")
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
    city: Optional[str] = None,
):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO events (title, description, date, end_date, time, venue, address, url, image_url, source, category, city, scraped_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
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
                city=excluded.city,
                scraped_at=excluded.scraped_at
        """, (title, description, date, end_date, time, venue, address, url, image_url, source, category, city))
        conn.commit()


def update_geocode(url: str, lat: float, lng: float) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE events SET lat = ?, lng = ? WHERE url = ?",
            (lat, lng, url),
        )
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


def get_all_events(city: Optional[str] = None) -> list[dict]:
    where = """
        (date IS NULL OR date = 'Multiple Dates' OR date >= date('now')
        OR (end_date IS NOT NULL AND end_date >= date('now')))
    """
    params: list = []
    if city:
        where += " AND city = ?"
        params.append(city)
    with get_conn() as conn:
        rows = conn.execute(f"""
            SELECT * FROM events WHERE {where}
            ORDER BY
                CASE WHEN date IS NULL OR date = 'Multiple Dates' THEN 1 ELSE 0 END,
                CASE WHEN date < date('now') THEN date('now') ELSE date END ASC,
                scraped_at DESC
        """, params).fetchall()
    return [dict(row) for row in rows]


def get_event_count(city: Optional[str] = None) -> int:
    where = """
        (date IS NULL OR date = 'Multiple Dates' OR date >= date('now')
        OR (end_date IS NOT NULL AND end_date >= date('now')))
    """
    params: list = []
    if city:
        where += " AND city = ?"
        params.append(city)
    with get_conn() as conn:
        row = conn.execute(f"SELECT COUNT(*) FROM events WHERE {where}", params).fetchone()
    return row[0]


def get_cities() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT city, COUNT(*) as count FROM events
            WHERE date IS NULL OR date = 'Multiple Dates' OR date >= date('now')
                OR (end_date IS NOT NULL AND end_date >= date('now'))
            GROUP BY city ORDER BY city
        """).fetchall()
    def _slug(name: str | None) -> str:
        if not name:
            return ""
        return re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')

    return [
        {"name": row["city"], "count": row["count"], "slug": _slug(row["city"])}
        for row in rows if row["city"]
    ]
