import os
import sqlite3
from datetime import datetime, UTC

DB_PATH = os.path.join(os.path.dirname(__file__), "provenance.db")


def _connect() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH)


def init_db() -> None:
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS submissions (
                content_id   TEXT PRIMARY KEY,
                creator_id   TEXT NOT NULL,
                timestamp    TEXT NOT NULL,
                signal_1_score REAL,
                signal_2_score REAL,
                attribution  TEXT NOT NULL,
                confidence   REAL NOT NULL,
                label        TEXT NOT NULL,
                status       TEXT NOT NULL DEFAULT 'classified',
                notes        TEXT
            )
        """)
        conn.commit()
        # MS5: add appeal columns to existing table; each wrapped in try/except
        # because SQLite raises OperationalError if the column already exists.
        for col_def in ("appeal_reasoning TEXT", "appeal_timestamp TEXT"):
            try:
                conn.execute(f"ALTER TABLE submissions ADD COLUMN {col_def}")
            except sqlite3.OperationalError:
                pass
        conn.commit()


def write_submission(
    content_id: str,
    creator_id: str,
    attribution: str,
    confidence: float,
    signal_1_score: float | None,
    signal_2_score: float | None,
    label: str,
    notes: str | None = None,
) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO submissions
                (content_id, creator_id, timestamp,
                 signal_1_score, signal_2_score,
                 attribution, confidence, label, status, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'classified', ?)
            """,
            (
                content_id,
                creator_id,
                datetime.now(UTC).isoformat(),
                signal_1_score,
                signal_2_score,
                attribution,
                confidence,
                label,
                notes,
            ),
        )
        conn.commit()


def get_submission(content_id: str) -> dict | None:
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM submissions WHERE content_id = ?", (content_id,)
        ).fetchone()
        return dict(row) if row else None


def file_appeal(content_id: str, creator_reasoning: str) -> None:
    with _connect() as conn:
        conn.execute(
            """
            UPDATE submissions
            SET status = 'under_review',
                appeal_reasoning = ?,
                appeal_timestamp = ?
            WHERE content_id = ?
            """,
            (creator_reasoning, datetime.now(UTC).isoformat(), content_id),
        )
        conn.commit()


def get_log(limit: int = 20) -> list[dict]:
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM submissions ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]
