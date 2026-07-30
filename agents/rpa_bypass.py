"""
API Bypass (RPA) Agent

Executes approved duplicate merges by driving a CRM UI when APIs are unusable.
Default mode is a safe simulator; set use_playwright=true to attempt a real
Playwright session when credentials + login_url are provided.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from agents.base import AgentResult, AgentSpec


def _load_merge_queue(payload: dict[str, Any]) -> list[dict]:
    if payload.get("merge_queue"):
        return list(payload["merge_queue"])
    path = payload.get("merge_queue_path") or payload.get("auto_merge_queue")
    if path:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    raise ValueError("Provide merge_queue or merge_queue_path (approved pairs only)")


def simulate_ui_merges(queue: list[dict], *, crm_system: str = "sam") -> list[dict]:
    """Deterministic stand-in for headless browser clicks."""
    results = []
    for i, pair in enumerate(queue):
        time.sleep(0)  # placeholder for pacing
        left = pair.get("left") or {}
        right = pair.get("right") or {}
        survivor_email = (left.get("email") or right.get("email") or f"row-{i}").lower()
        results.append(
            {
                "index": i,
                "crm_system": crm_system,
                "action": "merge_duplicate",
                "status": "simulated_success",
                "survivor_email": survivor_email,
                "score": pair.get("score"),
                "steps": [
                    f"Navigate to {crm_system} contacts search",
                    f"Open duplicate pair for {survivor_email}",
                    "Select survivor record",
                    "Confirm merge dialog",
                    "Verify post-merge search returns single hit",
                ],
            }
        )
    return results


def try_playwright_merges(queue: list[dict], payload: dict[str, Any]) -> dict[str, Any]:
    """
    Optional real RPA path. Requires playwright installed + login_url.
    Still refuses to run unless confirm_live=true (safety latch).
    """
    if not payload.get("confirm_live"):
        return {
            "ok": False,
            "error": "Live Playwright merges require confirm_live=true",
            "results": [],
        }
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except ImportError:
        return {
            "ok": False,
            "error": "playwright not installed; ran simulator instead",
            "results": simulate_ui_merges(queue, crm_system=payload.get("crm_system", "sam")),
            "fell_back_to_simulator": True,
        }

    login_url = payload.get("login_url")
    if not login_url:
        return {"ok": False, "error": "login_url required for live RPA", "results": []}

    # Skeleton only — real selectors are client-specific.
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(login_url)
        results.append(
            {
                "status": "opened_login",
                "note": (
                    "Client-specific selectors not configured. "
                    "Extend RPABypassAgent with CRM playbooks before production use."
                ),
                "queue_size": len(queue),
            }
        )
        browser.close()
    return {"ok": True, "results": results, "live": True}


class RPABypassAgent:
    spec = AgentSpec(
        id="rpa_bypass",
        name="API Bypass (RPA) Agent",
        description=(
            "Drives CRM UIs (Playwright/Selenium-ready) to execute approved "
            "duplicate merges when legacy APIs are unusable. Defaults to a safe simulator."
        ),
        capabilities=["rpa_merge"],
        input_kinds=["approved_merge_queue"],
        output_kinds=["merge_execution_report"],
        risks=[
            "Irreversible merges — only accept approved auto_merge or human-reviewed pairs",
            "UI selectors are brittle; maintain per-CRM playbooks",
            "Needs credentials; never log secrets",
        ],
    )

    def run(self, payload: dict[str, Any]) -> AgentResult:
        try:
            queue = _load_merge_queue(payload)
        except ValueError as exc:
            return AgentResult(
                ok=False,
                agent_id=self.spec.id,
                summary=str(exc),
                errors=[str(exc)],
            )

        # Safety: refuse review_needed pairs unless explicitly allowed
        if not payload.get("allow_unreviewed"):
            unsafe = [
                p for p in queue
                if p.get("decision") == "review_needed"
            ]
            if unsafe:
                return AgentResult(
                    ok=False,
                    agent_id=self.spec.id,
                    summary=(
                        f"Refused {len(unsafe)} unreviewed pairs. "
                        "Only auto_merge / approved tickets are executable."
                    ),
                    errors=["unreviewed_pairs_present"],
                    artifacts={"refused_count": len(unsafe)},
                )

        approved = [
            p for p in queue
            if p.get("decision") in {None, "auto_merge", "approved"}
        ]
        if not approved:
            return AgentResult(
                ok=False,
                agent_id=self.spec.id,
                summary="Merge queue empty after filtering to approved pairs.",
                errors=["empty_approved_queue"],
            )

        use_playwright = bool(payload.get("use_playwright"))
        crm_system = payload.get("crm_system") or "sam"

        if use_playwright:
            live = try_playwright_merges(approved, payload)
            results = live.get("results") or []
            ok = bool(live.get("ok") or live.get("fell_back_to_simulator"))
            summary = (
                f"Playwright path processed {len(results)} merge step(s) "
                f"for {crm_system}."
                + (" (simulator fallback)" if live.get("fell_back_to_simulator") else "")
            )
            errors = [live["error"]] if live.get("error") and not live.get("fell_back_to_simulator") else []
        else:
            results = simulate_ui_merges(approved, crm_system=crm_system)
            ok = True
            summary = (
                f"Simulated {len(results)} UI merges in {crm_system}. "
                "Set use_playwright=true + confirm_live=true for real browser runs."
            )
            errors = []

        output_dir = Path(payload.get("output_dir") or "outputs/rpa_bypass")
        output_dir.mkdir(parents=True, exist_ok=True)
        report_path = output_dir / "merge_execution_report.json"
        report = {
            "crm_system": crm_system,
            "mode": "playwright" if use_playwright else "simulator",
            "attempted": len(approved),
            "results": results,
        }
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

        return AgentResult(
            ok=ok and not errors,
            agent_id=self.spec.id,
            summary=summary,
            artifacts={
                "merge_execution_report": str(report_path),
                "report": report,
                "merged_count": len(results),
            },
            next_agents=["executive_report"],
            errors=errors,
        )
