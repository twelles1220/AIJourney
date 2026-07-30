"""Tests for specialist agents + Watchdog routing / recommend-build."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agents.fuzzy_match import FuzzyMatchAgent, score_pairs
from agents.orchestrator import list_agents, route_findings_batch, route_task
from agents.registry import AgentRegistry, build_default_registry, registry
from agents.schema_mapper import SchemaMapperAgent, map_headers
from agents.synthetic_data import SyntheticDataAgent
from agents.rpa_bypass import RPABypassAgent
from agents.executive_reporting import ExecutiveReportingAgent


SAMPLE_CSV = Path("samples/bloomerang_messy_export.csv")


class RegistryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Ensure clean default registry
        registry._agents.clear()
        build_default_registry()

    def test_five_agents_registered(self):
        specs = list_agents()
        ids = {s["id"] for s in specs}
        self.assertEqual(
            ids,
            {
                "schema_mapper",
                "fuzzy_match",
                "synthetic_data",
                "rpa_bypass",
                "executive_report",
            },
        )

    def test_route_schema_task(self):
        result = route_task(
            "Map Bloomerang CSV columns and normalize mashed names",
            execute=False,
        )
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["agent"]["id"], "schema_mapper")

    def test_route_finding_duplicate_to_fuzzy(self):
        result = route_task(
            "Duplicate donor email",
            finding_category="duplicate",
            execute=False,
        )
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["agent"]["id"], "fuzzy_match")

    def test_recommend_build_when_unknown(self):
        result = route_task(
            "Train a drone to deliver thank-you cookies to major donors",
            execute=False,
        )
        self.assertEqual(result["status"], "recommend_build")
        rec = result["recommendation"]
        self.assertIn("proposed_id", rec)
        self.assertIn("rationale", rec)
        self.assertTrue(rec["suggested_capabilities"])

    def test_recommend_build_missing_capability_agent(self):
        empty = AgentRegistry()
        from agents.base import DispatchRequest

        resolution = empty.resolve(
            DispatchRequest(task="merge via UI", capability="rpa_merge")
        )
        self.assertEqual(resolution["status"], "recommend_build")
        self.assertEqual(resolution["recommendation"]["proposed_id"], "agent_rpa_merge")


class SchemaMapperTests(unittest.TestCase):
    def test_maps_messy_headers_and_splits_name(self):
        plan = map_headers(
            ["Donor Name", "Email Address", "Mailing Address", "Status"]
        )
        self.assertGreaterEqual(plan["coverage"], 0.4)
        self.assertTrue(
            any(t["type"] == "split_full_name" for t in plan["transforms"])
        )
        self.assertTrue(
            any(t["type"] == "extract_postal_from_address" for t in plan["transforms"])
        )

    def test_runs_on_sample_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = SchemaMapperAgent().run(
                {"csv_path": str(SAMPLE_CSV), "output_dir": tmp}
            )
            self.assertTrue(result.ok)
            self.assertTrue(Path(result.artifacts["normalized_csv"]).exists())
            self.assertTrue(Path(result.artifacts["cleaner_script"]).exists())
            self.assertGreater(result.artifacts["row_count"], 0)


class FuzzyMatchTests(unittest.TestCase):
    def test_exact_email_is_auto_merge(self):
        rows = [
            {"first_name": "Alex", "last_name": "Smith", "email": "a@x.com"},
            {"first_name": "Alexander", "last_name": "Smith", "email": "a@x.com"},
        ]
        result = score_pairs(rows)
        self.assertEqual(result["auto_merge_count"], 1)
        self.assertEqual(result["auto_merge"][0]["score"], 100.0)

    def test_typo_names_land_in_review_or_auto(self):
        rows = [
            {
                "first_name": "Jon",
                "last_name": "Smyth",
                "email": "j1@x.com",
                "address_line1": "100 Main St",
                "postal_code": "80202",
            },
            {
                "first_name": "John",
                "last_name": "Smith",
                "email": "j2@x.com",
                "address_line1": "100 Main Street",
                "postal_code": "80202",
            },
        ]
        result = score_pairs(rows, auto_threshold=98, review_threshold=50)
        self.assertGreaterEqual(
            result["auto_merge_count"] + result["review_count"], 1
        )

    def test_agent_writes_review_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = FuzzyMatchAgent().run(
                {"csv_path": str(SAMPLE_CSV), "output_dir": tmp, "review_threshold": 60}
            )
            self.assertTrue(result.ok)
            self.assertTrue(Path(result.artifacts["review_needed_csv"]).exists())


class SyntheticAndRpaAndExecTests(unittest.TestCase):
    def test_synthetic_generator(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = SyntheticDataAgent().run(
                {
                    "csv_path": str(SAMPLE_CSV),
                    "row_count": 50,
                    "output_dir": tmp,
                    "seed": 7,
                }
            )
            self.assertTrue(result.ok)
            self.assertEqual(result.artifacts["report"]["row_count"], 50)
            self.assertTrue(Path(result.artifacts["synthetic_csv"]).exists())

    def test_rpa_refuses_unreviewed(self):
        result = RPABypassAgent().run(
            {
                "merge_queue": [
                    {
                        "decision": "review_needed",
                        "left": {"email": "a@x.com"},
                        "right": {"email": "b@x.com"},
                        "score": 90,
                    }
                ]
            }
        )
        self.assertFalse(result.ok)
        self.assertIn("unreviewed_pairs_present", result.errors)

    def test_rpa_simulator_on_auto_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = RPABypassAgent().run(
                {
                    "merge_queue": [
                        {
                            "decision": "auto_merge",
                            "left": {"email": "a@x.com"},
                            "right": {"email": "a@x.com"},
                            "score": 100,
                        }
                    ],
                    "output_dir": tmp,
                    "crm_system": "sam",
                }
            )
            self.assertTrue(result.ok)
            self.assertEqual(result.artifacts["merged_count"], 1)

    def test_executive_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = ExecutiveReportingAgent().run(
                {
                    "client_name": "Demo Nonprofit",
                    "metrics": {
                        "corporate_links": 135,
                        "family_links": 804,
                        "auto_merge_count": 12,
                        "review_count": 40,
                        "merged_count": 12,
                    },
                    "output_dir": tmp,
                }
            )
            self.assertTrue(result.ok)
            text = Path(result.artifacts["executive_summary_md"]).read_text()
            self.assertIn("135", text)
            self.assertIn("804", text)


class OrchestratorBatchTests(unittest.TestCase):
    def test_route_findings_batch(self):
        registry._agents.clear()
        build_default_registry()
        findings = [
            {"category": "duplicate", "title": "Duplicate donor email"},
            {"category": "incomplete", "title": "Incomplete donor record #4"},
            {"category": "security", "title": "Payment card in notes"},
        ]
        batch = route_findings_batch(findings, execute=False)
        self.assertEqual(batch["finding_count"], 3)
        self.assertIn("fuzzy_match", batch["by_agent"])
        self.assertIn("schema_mapper", batch["by_agent"])


class PipelineIntegrationTests(unittest.TestCase):
    def test_end_to_end_pipeline_artifacts(self):
        registry._agents.clear()
        build_default_registry()
        with tempfile.TemporaryDirectory() as tmp:
            mapped = route_task(
                "Normalize Bloomerang export",
                payload={"csv_path": str(SAMPLE_CSV), "output_dir": f"{tmp}/map"},
            )
            self.assertEqual(mapped["status"], "completed")
            norm = mapped["result"]["artifacts"]["normalized_csv"]
            fuzzy = route_task(
                "Fuzzy match duplicates",
                payload={"csv_path": norm, "output_dir": f"{tmp}/fuzzy"},
            )
            self.assertEqual(fuzzy["status"], "completed")
            queue = fuzzy["result"]["artifacts"]["auto_merge"]
            rpa = route_task(
                "RPA merge approved pairs",
                payload={
                    "merge_queue": queue,
                    "output_dir": f"{tmp}/rpa",
                    "crm_system": "sam",
                },
            )
            # OK even if queue empty
            self.assertIn(rpa["status"], {"completed", "failed"})
            exec_r = route_task(
                "Executive summary email",
                payload={
                    "client_name": "Demo",
                    "metrics": {
                        "corporate_links": 135,
                        "family_links": 804,
                        "auto_merge_count": fuzzy["result"]["artifacts"]["auto_merge_count"],
                        "review_count": fuzzy["result"]["artifacts"]["review_count"],
                    },
                    "output_dir": f"{tmp}/exec",
                },
            )
            self.assertEqual(exec_r["status"], "completed")


if __name__ == "__main__":
    unittest.main()
