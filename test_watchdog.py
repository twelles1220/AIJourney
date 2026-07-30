"""Lightweight tests for Watchdog detectors (no Anthropic API key required)."""

from __future__ import annotations

import unittest

from watchdog_db import connect, list_tables
from watchdog_detectors import run_full_audit, suggest_actions
from watchdog_agent import run_audit_report


class WatchdogAuditTests(unittest.TestCase):
    def test_sample_schema_seeded(self):
        with connect() as conn:
            tables = list_tables(conn)
        self.assertEqual(set(tables), {"donations", "donors", "interactions"})

    def test_full_audit_finds_critical_security(self):
        with connect() as conn:
            report = run_full_audit(conn)
        self.assertGreater(report["finding_count"], 0)
        self.assertIn("critical", report["by_severity"])
        categories = {f["category"] for f in report["findings"]}
        for expected in ("security", "duplicate", "incomplete", "unknown_value", "integrity"):
            self.assertIn(expected, categories)

    def test_card_data_in_notes_flagged(self):
        with connect() as conn:
            report = run_full_audit(conn)
        security = [f for f in report["findings"] if f["category"] == "security"]
        self.assertTrue(any("card" in f["title"].lower() or "pan" in f["detail"].lower()
                            or "Payment card" in f["title"] for f in security))

    def test_duplicate_email_flagged(self):
        with connect() as conn:
            report = run_full_audit(conn)
        dupes = [f for f in report["findings"] if f["category"] == "duplicate"]
        self.assertTrue(any("email" in f["title"].lower() for f in dupes))
        email_dupe = next(f for f in dupes if "email" in f["title"].lower())
        self.assertIn(1, email_dupe["record_ids"])
        self.assertIn(3, email_dupe["record_ids"])

    def test_orphan_donation_flagged(self):
        with connect() as conn:
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


if __name__ == "__main__":
    unittest.main()
