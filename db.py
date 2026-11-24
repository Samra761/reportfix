"""SQLite schema, connection and seed data."""
import sqlite3
import threading
import time

import config

LOCK = threading.RLock()  # one writer at a time; the app is tiny, this keeps SQLite simple and safe
_conn = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('student','staff','admin')),
    department TEXT, created_at REAL NOT NULL);
-- Append-only. Trust score = TRUST_START + sum(delta), clamped.
CREATE TABLE IF NOT EXISTS trust_events (
    id INTEGER PRIMARY KEY, student_id INTEGER NOT NULL, delta REAL NOT NULL, reason TEXT NOT NULL,
    issue_id INTEGER, report_id INTEGER, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS issues (
    id INTEGER PRIMARY KEY, location TEXT, category TEXT NOT NULL, department TEXT NOT NULL,
    is_safety INTEGER NOT NULL, confidential INTEGER NOT NULL, status TEXT NOT NULL,
    summary TEXT NOT NULL, reason TEXT NOT NULL, verify_round INTEGER NOT NULL DEFAULT 0,
    reopen_count INTEGER NOT NULL DEFAULT 0, resolved_at REAL, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY, student_id INTEGER NOT NULL, text TEXT NOT NULL, issue_id INTEGER,
    status TEXT NOT NULL,              -- linked | needs_clarification | duplicate
    location_mentioned TEXT, category TEXT NOT NULL, is_safety INTEGER NOT NULL, is_harassment INTEGER NOT NULL,
    injection_attempt INTEGER NOT NULL, reason TEXT NOT NULL, summary TEXT NOT NULL, triage_source TEXT NOT NULL,
    flagged_fake INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL);
-- Students affected by an issue (original reporters + "me too"). One row per student per issue.
CREATE TABLE IF NOT EXISTS supporters (
    issue_id INTEGER NOT NULL, student_id INTEGER NOT NULL, via TEXT NOT NULL, report_id INTEGER,
    created_at REAL NOT NULL, PRIMARY KEY (issue_id, student_id));
CREATE TABLE IF NOT EXISTS votes (
    issue_id INTEGER NOT NULL, round INTEGER NOT NULL, student_id INTEGER NOT NULL,
    vote TEXT NOT NULL CHECK(vote IN ('confirm','dispute')), created_at REAL NOT NULL,
    PRIMARY KEY (issue_id, round, student_id));
CREATE TABLE IF NOT EXISTS status_log (
    id INTEGER PRIMARY KEY, issue_id INTEGER NOT NULL, actor_role TEXT NOT NULL, from_status TEXT,
    to_status TEXT NOT NULL, note TEXT, created_at REAL NOT NULL);
"""


def conn():
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False, isolation_level=None)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(SCHEMA)
        if _conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
            seed()
    return _conn


def q(sql, args=()):
    return conn().execute(sql, args).fetchall()


def q1(sql, args=()):
    return conn().execute(sql, args).fetchone()


def ex(sql, args=()):
    return conn().execute(sql, args).lastrowid


def reset():
    with LOCK:
        c = conn()
        for t in ["users", "trust_events", "issues", "reports", "supporters", "votes", "status_log"]:
            c.execute(f"DELETE FROM {t}")
        c.execute("DELETE FROM sqlite_sequence") if c.execute(
            "SELECT name FROM sqlite_master WHERE name='sqlite_sequence'").fetchone() else None
        seed()


def seed():
    c = _conn
    old = time.time() - 30 * 24 * 3600  # seeded accounts are 30 days old (not on probation)
    students = [  # name, [(delta, reason)] -> gives a spread of trust levels with explainable history
        ("Ayesha (high trust)", [(+25, "seed: 5 past reports confirmed fixed"), (+15, "seed: accurate votes")]),
        ("Bilal", [(+15, "seed: 3 past reports confirmed fixed")]),
        ("Sara", []),
        ("Hamza", [(-20, "seed: past report flagged as spam by staff")]),
        ("Zain (spammer)", [(-30, "seed: 2 past reports flagged fake"), (-10, "seed: injection attempts")]),
    ]
    for name, events in students:
        sid = c.execute("INSERT INTO users(name, role, created_at) VALUES (?, 'student', ?)", (name, old)).lastrowid
        for delta, reason in events:
            c.execute("INSERT INTO trust_events(student_id, delta, reason, created_at) VALUES (?,?,?,?)",
                      (sid, delta, reason, old))
    for d in config.DEPARTMENTS:
        c.execute("INSERT INTO users(name, role, department, created_at) VALUES (?, 'staff', ?, ?)",
                  (f"{d} Staff", d, old))
    c.execute("INSERT INTO users(name, role, created_at) VALUES ('Admin', 'admin', ?)", (old,))
