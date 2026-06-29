import os
import sqlite3
from datetime import datetime, UTC

DB_PATH = os.path.join(os.path.dirname(__file__), "provenance.db")


def _connect() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH, timeout=5)


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
        for col_def in (
            "appeal_reasoning TEXT",
            "appeal_timestamp TEXT",
            "signal_3_score REAL",
            "content_type TEXT DEFAULT 'text'",
            "certificate_id TEXT",
        ):
            try:
                conn.execute(f"ALTER TABLE submissions ADD COLUMN {col_def}")
            except sqlite3.OperationalError:
                pass
        conn.execute("""
            CREATE TABLE IF NOT EXISTS certificates (
                cert_id    TEXT PRIMARY KEY,
                content_id TEXT NOT NULL,
                creator_id TEXT NOT NULL,
                issued_at  TEXT NOT NULL,
                statement  TEXT NOT NULL
            )
        """)
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
    signal_3_score: float | None = None,
    content_type: str = "text",
) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO submissions
                (content_id, creator_id, timestamp,
                 signal_1_score, signal_2_score, signal_3_score,
                 attribution, confidence, label, status, notes, content_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'classified', ?, ?)
            """,
            (
                content_id,
                creator_id,
                datetime.now(UTC).isoformat(),
                signal_1_score,
                signal_2_score,
                signal_3_score,
                attribution,
                confidence,
                label,
                notes,
                content_type,
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


def get_analytics() -> dict:
    with _connect() as conn:
        total = conn.execute("SELECT COUNT(*) FROM submissions").fetchone()[0]

        dist_rows = conn.execute(
            "SELECT attribution, COUNT(*) FROM submissions GROUP BY attribution"
        ).fetchall()
        distribution = {row[0]: row[1] for row in dist_rows}

        appealed = conn.execute(
            "SELECT COUNT(*) FROM submissions WHERE appeal_timestamp IS NOT NULL"
        ).fetchone()[0]

        # Signal agreement: s1 and s2 both vote same direction (AI or human).
        # Exclude rows where signal_2_score == 0.5 — that is the neutral guard
        # value returned when Signal 2 has insufficient data, not a real vote.
        eligible = conn.execute(
            """
            SELECT COUNT(*) FROM submissions
            WHERE signal_1_score IS NOT NULL
              AND signal_2_score IS NOT NULL
              AND signal_2_score != 0.5
            """
        ).fetchone()[0]
        agreed = conn.execute(
            """
            SELECT COUNT(*) FROM submissions
            WHERE signal_1_score IS NOT NULL
              AND signal_2_score IS NOT NULL
              AND signal_2_score != 0.5
              AND ((signal_1_score >= 0.5 AND signal_2_score >= 0.5)
                OR (signal_1_score < 0.5  AND signal_2_score < 0.5))
            """
        ).fetchone()[0]

        agreement_rate = round(agreed / eligible, 4) if eligible > 0 else None
        appeal_rate = round(appealed / total, 4) if total > 0 else 0.0

        return {
            "total_submissions": total,
            "attribution_distribution": distribution,
            "appeal_rate": appeal_rate,
            "signal_agreement_rate": agreement_rate,
        }


def issue_certificate(content_id: str, creator_id: str, statement: str) -> str:
    import uuid as _uuid
    cert_id = str(_uuid.uuid4())
    issued_at = datetime.now(UTC).isoformat()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO certificates (cert_id, content_id, creator_id, issued_at, statement)
            VALUES (?, ?, ?, ?, ?)
            """,
            (cert_id, content_id, creator_id, issued_at, statement),
        )
        conn.execute(
            "UPDATE submissions SET certificate_id = ? WHERE content_id = ?",
            (cert_id, content_id),
        )
        conn.commit()
    return cert_id


def get_certificate(cert_id: str) -> dict | None:
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        cert = conn.execute(
            "SELECT * FROM certificates WHERE cert_id = ?", (cert_id,)
        ).fetchone()
        if cert is None:
            return None
        cert_dict = dict(cert)
        sub = conn.execute(
            "SELECT attribution, confidence FROM submissions WHERE content_id = ?",
            (cert_dict["content_id"],),
        ).fetchone()
        if sub:
            cert_dict["attribution"] = sub["attribution"]
            cert_dict["confidence"] = sub["confidence"]
        return cert_dict
