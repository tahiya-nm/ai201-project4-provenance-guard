# database.py
# ─────────────────────────────────────────────────────────────────────────────
# SQLite database layer for Provenance Guard.
#
# Steps performed by this module:
#   1. init_db()            — Creates 3 tables on first run (submissions, appeals, certificates)
#   2. insert_submission()  — Writes a new classification result to the DB
#   3. get_submission()     — Fetches a single submission by content_id (LEFT JOINs cert)
#   4. update_status()      — Updates a submission's status field
#   5. insert_appeal()      — Logs a new appeal row
#   6. insert_certificate() — Issues a provenance certificate (stretch)
#   7. get_log()            — Returns recent audit log entries with appeal + cert data
#   8. get_analytics()      — Returns aggregated stats for the dashboard (stretch)
#
# Time complexity:  O(1) for single-row ops; O(n) for log/analytics scans
# Space complexity: O(n) where n = total stored submissions
# ─────────────────────────────────────────────────────────────────────────────

import sqlite3
from datetime import datetime, timezone

DB_PATH = "provenance.db"


def init_db():
    """
    Creates all required tables if they don't already exist.
    Called once at Flask app startup — safe to call repeatedly (IF NOT EXISTS).
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # One row per POST /submit
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS submissions (
            content_id        TEXT PRIMARY KEY,
            creator_id        TEXT NOT NULL,
            timestamp         TEXT NOT NULL,
            content_type      TEXT DEFAULT 'text',
            text              TEXT NOT NULL,
            attribution       TEXT NOT NULL,
            confidence        REAL NOT NULL,
            llm_score         REAL,
            stylometric_score REAL,
            burstiness_score  REAL,
            label_text        TEXT NOT NULL,
            status            TEXT DEFAULT 'classified'
        )
    """)

    # One row per POST /appeal
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS appeals (
            appeal_id         TEXT PRIMARY KEY,
            content_id        TEXT NOT NULL,
            creator_reasoning TEXT NOT NULL,
            timestamp         TEXT NOT NULL,
            FOREIGN KEY (content_id) REFERENCES submissions(content_id)
        )
    """)

    # One row per POST /verify/<content_id> — stretch: provenance certificate
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS certificates (
            certificate_id TEXT PRIMARY KEY,
            content_id     TEXT NOT NULL,
            creator_id     TEXT NOT NULL,
            issued_at      TEXT NOT NULL,
            FOREIGN KEY (content_id) REFERENCES submissions(content_id)
        )
    """)

    conn.commit()
    conn.close()


def _now_utc() -> str:
    """Returns the current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def insert_submission(content_id, creator_id, text, attribution, confidence,
                      llm_score, stylometric_score, burstiness_score,
                      label_text, content_type="text") -> str:
    """
    Inserts a new classification result. Returns the timestamp string.
    Time: O(1) — single indexed insert
    """
    timestamp = _now_utc()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO submissions
        (content_id, creator_id, timestamp, content_type, text, attribution,
         confidence, llm_score, stylometric_score, burstiness_score, label_text, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'classified')
    """, (content_id, creator_id, timestamp, content_type, text, attribution,
          confidence, llm_score, stylometric_score, burstiness_score, label_text))
    conn.commit()
    conn.close()
    return timestamp


def get_submission(content_id: str) -> dict | None:
    """
    Fetches a submission by content_id.
    LEFT JOINs certificates so certificate_id is included if one has been issued.
    Returns a dict or None if not found.
    Time: O(1) — primary key lookup
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("""
        SELECT s.*, c.certificate_id
        FROM submissions s
        LEFT JOIN certificates c ON s.content_id = c.content_id
        WHERE s.content_id = ?
    """, (content_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def update_status(content_id: str, status: str):
    """
    Updates the status of a submission.
    Valid values: 'classified', 'under_review', 'verified'
    Time: O(1) — primary key update
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE submissions SET status = ? WHERE content_id = ?",
        (status, content_id)
    )
    conn.commit()
    conn.close()


def insert_appeal(appeal_id: str, content_id: str, creator_reasoning: str) -> str:
    """
    Inserts an appeal record. Returns the timestamp string.
    Time: O(1) — single insert
    """
    timestamp = _now_utc()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO appeals (appeal_id, content_id, creator_reasoning, timestamp)
        VALUES (?, ?, ?, ?)
    """, (appeal_id, content_id, creator_reasoning, timestamp))
    conn.commit()
    conn.close()
    return timestamp


def insert_certificate(certificate_id: str, content_id: str, creator_id: str) -> str:
    """
    Issues a provenance certificate. Returns the issued_at timestamp.
    Stretch feature. Time: O(1) — single insert.
    """
    issued_at = _now_utc()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO certificates (certificate_id, content_id, creator_id, issued_at)
        VALUES (?, ?, ?, ?)
    """, (certificate_id, content_id, creator_id, issued_at))
    conn.commit()
    conn.close()
    return issued_at


def get_log(limit: int = 50) -> list[dict]:
    """
    Returns the most recent `limit` audit log entries, newest first.
    Each entry includes the submission, any associated appeal, and any certificate.
    Time: O(n) where n = limit
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("""
        SELECT
            s.content_id, s.creator_id, s.timestamp, s.content_type,
            s.attribution, s.confidence, s.llm_score, s.stylometric_score,
            s.burstiness_score, s.label_text, s.status,
            a.appeal_id,
            a.creator_reasoning  AS appeal_reasoning,
            a.timestamp          AS appeal_timestamp,
            c.certificate_id,
            c.issued_at          AS certificate_issued_at
        FROM submissions s
        LEFT JOIN appeals      a ON s.content_id = a.content_id
        LEFT JOIN certificates c ON s.content_id = c.content_id
        ORDER BY s.timestamp DESC
        LIMIT ?
    """, (limit,))
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_analytics() -> dict:
    """
    Returns aggregated analytics for the dashboard (stretch feature).
    Time: O(n) — full table scans; acceptable for a small audit log.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) AS total FROM submissions")
    total = cursor.fetchone()["total"]

    cursor.execute("""
        SELECT attribution,
               COUNT(*) AS count,
               ROUND(AVG(confidence), 3) AS avg_confidence
        FROM submissions
        GROUP BY attribution
    """)
    by_attribution = [dict(r) for r in cursor.fetchall()]

    cursor.execute("SELECT COUNT(*) AS total_appeals FROM appeals")
    total_appeals = cursor.fetchone()["total_appeals"]

    cursor.execute("SELECT COUNT(*) AS total_certs FROM certificates")
    total_certs = cursor.fetchone()["total_certs"]

    cursor.execute("""
        SELECT DATE(timestamp) AS day, COUNT(*) AS count
        FROM submissions
        WHERE timestamp >= DATETIME('now', '-7 days')
        GROUP BY day
        ORDER BY day
    """)
    recent_trend = [dict(r) for r in cursor.fetchall()]

    conn.close()

    return {
        "total_submissions":        total,
        "by_attribution":           by_attribution,
        "total_appeals":            total_appeals,
        "appeal_rate":              round(total_appeals / total, 3) if total > 0 else 0.0,
        "total_certificates_issued": total_certs,
        "recent_trend_7d":          recent_trend,
    }