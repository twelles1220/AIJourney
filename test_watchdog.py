"""Tests for Watchdog detectors + continuous guardian (no Anthropic API key)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from watchdog_agent import run_audit_report
from watchdog_db import connect, list_tables, open_db
from watchdog_detectors import run_full_audit, suggest_actions
from watchdog_guardian import (
    auto_remediate,
    guard_donor_ingest,
    insert_donor_guarded,
    run_continuous,
    run_guardian_cycle,
)


CRM_TABLES = {"donations", "donors", "interactions"}
GUARDIAN_TABLES = {"watchdog_audit_log", "watchdog_escalations"}


class WatchdogAuditTests(unittest.TestCase):
    def test_sample_schema_seeded(self):
        with connect(memory=True) as conn:
            tables = set(list_tables(conn))
        self.assertTrue(CRM_TABLES.issubset(tables))
        self.assertTrue(GUARDIAN_TABLES.issubset(tables))

    def test_full_audit_finds_critical_security(self):
        with connect(memory=True) as conn:
            report = run_full_audit(conn)
        self.assertGreater(report["finding_count"], 0)
        self.assertIn("critical", report["by_severity"])
        categories = {f["category"] for f in report["findings"]}
        for expected in ("security", "duplicate", "incomplete", "unknown_value", "integrity"):
            self.assertIn(expected, categories)

    def test_card_data_in_notes_flagged(self):
        with connect(memory=True) as conn:
            report = run_full_audit(conn)
        security = [f for f in report["findings"] if f["category"] == "security"]
        self.assertTrue(
            any(
                "card" in f["title"].lower()
                or "pan" in f["detail"].lower()
                or "Payment card" in f["title"]
                for f in security
            )
        )

    def test_duplicate_email_flagged(self):
        with connect(memory=True) as conn:
            report = run_full_audit(conn)
        dupes = [f for f in report["findings"] if f["category"] == "duplicate"]
        self.assertTrue(any("email" in f["title"].lower() for f in dupes))
        email_dupe = next(f for f in dupes if "email" in f["title"].lower())
        self.assertIn(1, email_dupe["record_ids"])
        self.assertIn(3, email_dupe["record_ids"])

    def test_orphan_donation_flagged(self):
        with connect(memory=True) as conn:
            report = run_full_audit(conn)
        integrity = [f for f in report["findings"] if f["category"] == "integrity"]
        self.assertTrue(any(f["record_ids"] == [6] for f in integrity))

    def test_remediation_plan_prioritizes_security(self):
        report = run_audit_report()
        plan = report["remediation_plan"]
        self.assertGreaterEqual(len(plan), 1)
        self.assertEqual(plan[0]["category"], "security")
        self.assertIn("security", plan[0]["quality_goals"])
        actions = suggest_actions(report["findings"], max_actions=5)
        self.assertLessEqual(len(actions), 5)


class WatchdogGuardianTests(unittest.TestCase):
    def test_ingest_rejects_card_data(self):
        result = guard_donor_ingest(
            {
                "first_name": "Bad",
                "last_name": "Card",
                "email": "bad@example.com",
                "notes": "Card 4111-1111-1111-1111 CVV 999",
            }
        )
        self.assertEqual(result["decision"], "reject")
        self.assertTrue(any("PCI" in v or "card" in v.lower() for v in result["violations"]))

    def test_ingest_rejects_test_fixture(self):
        result = guard_donor_ingest(
            {"first_name": "Test", "last_name": "User", "email": "test@test.com"}
        )
        self.assertEqual(result["decision"], "reject")

    def test_ingest_sanitizes_full_ssn_and_placeholders(self):
        result = guard_donor_ingest(
            {
                "first_name": "Pat",
                "last_name": "Lee",
                "email": "pat.lee@example.com",
                "ssn_last4": "123456789",
                "city": "N/A",
            }
        )
        self.assertIn(result["decision"], {"accept", "accept_sanitized"})
        self.assertEqual(result["record"]["ssn_last4"], "6789")
        self.assertIsNone(result["record"]["city"])

    def test_ingest_insert_writes_audit(self):
        with connect(memory=True) as conn:
            result = insert_donor_guarded(
                conn,
                {
                    "first_name": "Clean",
                    "last_name": "Donor",
                    "email": "clean.donor@example.com",
                    "notes": "Hello",
                },
            )
            self.assertEqual(result["decision"], "accept")
            self.assertIsNotNone(result["inserted_id"])
            logs = conn.execute(
                "SELECT action FROM watchdog_audit_log WHERE record_id=?",
                (result["inserted_id"],),
            ).fetchall()
            self.assertTrue(any(r["action"] == "ingest_accept" for r in logs))

    def test_auto_remediate_cleans_security_and_orphans(self):
        with connect(memory=True) as conn:
            before = run_full_audit(conn)["finding_count"]
            report = auto_remediate(conn, dry_run=False)
            after = run_full_audit(conn)["finding_count"]

            self.assertGreater(report["applied_count"], 0)
            self.assertGreater(report["escalated_count"], 0)
            self.assertLess(after, before)

            donor7 = dict(
                conn.execute("SELECT * FROM donors WHERE id=7").fetchone()
            )
            self.assertEqual(donor7["ssn_last4"], "4321")
            self.assertIn("[REDACTED_PAN]", donor7["notes"] or "")
            self.assertEqual(donor7["quarantined"], 1)

            orphan = conn.execute(
                "SELECT COUNT(*) AS n FROM donations WHERE id=6"
            ).fetchone()["n"]
            self.assertEqual(orphan, 0)

            # Duplicates must not be auto-merged
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) AS n FROM donors WHERE email='alex.smith@example.com'"
                ).fetchone()["n"],
                2,
            )

    def test_guardian_cycle_persistent_and_continuous_steady_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "guard.db")
            # Seed persistent DB
            conn = open_db(db_path, seed=True, memory=False)
            conn.close()

            first = run_guardian_cycle(db_path=db_path, dry_run=False)
            self.assertGreater(first["remediation"]["applied_count"], 0)
            self.assertGreater(first["findings_resolved"], 0)
            self.assertGreater(len(first["open_escalations"]), 0)

            cycles = run_continuous(
                interval_seconds=0.01,
                max_cycles=3,
                db_path=db_path,
                dry_run=False,
                stop_when_clean=True,
                on_cycle=lambda _report: None,
            )
            self.assertGreaterEqual(len(cycles), 1)
            # Later cycles should not keep applying the same auto-fixes
            if len(cycles) > 1:
                self.assertEqual(cycles[-1]["remediation"]["applied_count"], 0)


if __name__ == "__main__":
    unittest.main()
