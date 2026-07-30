"""
Sample CRM-style database with intentional data bloat for Watchdog demos.

Uses an in-memory SQLite database so scans feel like real table/column work
without requiring external credentials. Replace `connect()` with a real
CRM/warehouse connector in production.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator


SCHEMA_SQL = """
CREATE TABLE donors (
    id INTEGER PRIMARY KEY,
    first_name TEXT,
    last_name TEXT,
    email TEXT,
    phone TEXT,
    status TEXT,
    address_line1 TEXT,
    city TEXT,
    state TEXT,
    postal_code TEXT,
    ssn_last4 TEXT,
    notes TEXT,
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE donations (
    id INTEGER PRIMARY KEY,
    donor_id INTEGER,
    amount REAL,
    currency TEXT,
    campaign TEXT,
    payment_method TEXT,
    card_last4 TEXT,
    donated_at TEXT,
    FOREIGN KEY (donor_id) REFERENCES donors(id)
);

CREATE TABLE interactions (
    id INTEGER PRIMARY KEY,
    donor_id INTEGER,
    channel TEXT,
    summary TEXT,
    created_at TEXT,
    FOREIGN KEY (donor_id) REFERENCES donors(id)
);
"""

SEED_DONORS = [
    # Clean records
    (1, "Alex", "Smith", "alex.smith@example.com", "555-0101", "active",
     "12 Oak St", "Austin", "TX", "78701", "1234", "Major donor prospect",
     "2024-01-10", "2026-06-01"),
    (2, "Jordan", "Lee", "jordan.lee@example.org", "555-0102", "active",
     "88 Pine Ave", "Denver", "CO", "80202", "5678", None,
     "2024-03-15", "2026-05-20"),
    # Duplicate of Alex (same email + near-identical identity)
    (3, "Alexander", "Smith", "alex.smith@example.com", "555-0199", "active",
     "12 Oak Street", "Austin", "TX", "78701", "1234", "Duplicate profile?",
     "2025-11-02", "2025-11-02"),
    # Incomplete: missing email, phone, address
    (4, "Sam", "Nguyen", None, None, "active",
     None, None, None, None, None, "Walk-in volunteer lead",
     "2025-08-01", "2025-08-01"),
    # Unknown / placeholder values
    (5, "Unknown", "Donor", "unknown@unknown.com", "000-000-0000", "unknown",
     "N/A", "N/A", "NA", "00000", "XXXX", "TBD — imported from legacy CSV",
     "2023-02-01", "2023-02-01"),
    (6, "Test", "User", "test@test.com", "555-0000", "inactive",
     "n/a", "Unknown", "XX", "99999", None, "QA fixture left in prod",
     "2022-01-01", "2022-01-01"),
    # Security concerns: SSN-like + card data in notes
    (7, "Casey", "Brooks", "casey.brooks@mail.com", "555-0144", "active",
     "401 River Rd", "Seattle", "WA", "98101", "987654321",
     "Called about gift. Card on file: 4111-1111-1111-1111 exp 09/27 CVV 123",
     "2024-07-22", "2026-04-11"),
    # Near-duplicate of Jordan (same phone, different email typo domain)
    (8, "Jordan", "Lee", "jordan.lee@examp1e.org", "555-0102", "active",
     "88 Pine Avenue", "Denver", "CO", "80202", "5678", None,
     "2026-01-05", "2026-01-05"),
    # Incomplete + stale
    (9, "Riley", None, "riley@", None, "prospect",
     "", "", "", "", None, None,
     "2021-05-01", "2021-05-01"),
    # Orphan-ish: will have no donations/interactions
    (10, "Morgan", "Patel", "morgan.patel@example.com", "555-0188", "lapsed",
     "9 Cedar Ct", "Chicago", "IL", "60601", "4321", "Do not email — preference noted",
     "2020-09-12", "2020-09-12"),
]

SEED_DONATIONS = [
    (1, 1, 250.0, "USD", "Spring Gala", "card", "4242", "2026-03-01"),
    (2, 1, 100.0, "USD", "Annual Fund", "ach", None, "2025-11-15"),
    (3, 2, 50.0, "USD", "Annual Fund", "card", "1111", "2026-02-10"),
    (4, 3, 250.0, "USD", "Spring Gala", "card", "4242", "2026-03-01"),  # dup gift on dup donor
    (5, 7, 1000.0, "USD", "Capital Campaign", "card", "1111", "2026-04-11"),
    (6, 999, 75.0, "USD", "Unknown", "cash", None, "2024-01-01"),  # orphan FK
    (7, 5, 0.0, "USD", "N/A", "unknown", None, "2023-02-01"),  # incomplete/zero
]

SEED_INTERACTIONS = [
    (1, 1, "email", "Confirmed gala attendance", "2026-02-20"),
    (2, 2, "sms", "Volunteer signup follow-up", "2026-05-18"),
    (3, 7, "phone", "Discussed capital campaign pledge; stored card verbally", "2026-04-10"),
    (4, 3, "email", "Welcome email bounced — duplicate account?", "2025-11-03"),
]


def _seed(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    conn.executemany(
        """
        INSERT INTO donors (
            id, first_name, last_name, email, phone, status,
            address_line1, city, state, postal_code, ssn_last4, notes,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        SEED_DONORS,
    )
    conn.executemany(
        """
        INSERT INTO donations (
            id, donor_id, amount, currency, campaign, payment_method,
            card_last4, donated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        SEED_DONATIONS,
    )
    conn.executemany(
        """
        INSERT INTO interactions (id, donor_id, channel, summary, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        SEED_INTERACTIONS,
    )
    conn.commit()


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """Yield a seeded in-memory SQLite connection (fresh each call)."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        _seed(conn)
        yield conn
    finally:
        conn.close()


def list_tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    return [r["name"] for r in rows]


def _assert_known_table(conn: sqlite3.Connection, table: str) -> str:
    """Allow only real tables from sqlite_master (prevents SQL injection via names)."""
    known = set(list_tables(conn))
    if table not in known:
        raise ValueError(f"Unknown table: {table!r}")
    return table


def table_schema(conn: sqlite3.Connection, table: str) -> list[dict]:
    table = _assert_known_table(conn, table)
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [
        {
            "cid": r["cid"],
            "name": r["name"],
            "type": r["type"],
            "notnull": bool(r["notnull"]),
            "pk": bool(r["pk"]),
        }
        for r in rows
    ]


def fetch_all(conn: sqlite3.Connection, table: str) -> list[dict]:
    table = _assert_known_table(conn, table)
    rows = conn.execute(f"SELECT * FROM {table}").fetchall()
    return [dict(r) for r in rows]


def row_count(conn: sqlite3.Connection, table: str) -> int:
    table = _assert_known_table(conn, table)
    return conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
