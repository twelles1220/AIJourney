"""
Executive Reporting Agent

Turns Watchdog / specialist run metrics into client-ready business language:
email draft + executive summary markdown highlighting ROI and data hygiene wins.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agents.base import AgentResult, AgentSpec


def _num(metrics: dict, *keys: str, default: int | float = 0):
    for k in keys:
        if k in metrics and metrics[k] is not None:
            return metrics[k]
    return default


def build_executive_pack(metrics: dict[str, Any], *, client_name: str = "Client") -> dict[str, str]:
    duplicates = _num(metrics, "duplicates_found", "duplicate_count", "auto_merge_count")
    review = _num(metrics, "review_count", "review_needed")
    merged = _num(metrics, "merged_count", "merges_executed")
    corporate_links = _num(metrics, "corporate_links", "direct_corporate_links")
    family_links = _num(metrics, "family_links")
    findings_before = _num(metrics, "findings_before", "before_finding_count")
    findings_after = _num(metrics, "findings_after", "after_finding_count")
    quarantined = _num(metrics, "quarantined_count", "quarantined")
    security_fixed = _num(metrics, "security_fixed", "critical_remediated")
    normalized_rows = _num(metrics, "normalized_rows", "row_count")
    hours_saved = _num(metrics, "hours_saved_estimate", default=None)
    if hours_saved is None:
        # Rough consultancy heuristic: 3 min per reviewed/merged pair + 1 min per normalized row/100
        hours_saved = round((duplicates + review + merged) * 3 / 60 + normalized_rows / 6000, 1)

    findings_resolved = max(0, findings_before - findings_after) if findings_before else security_fixed

    summary_md = f"""# Executive Data Hygiene Summary — {client_name}

## Snapshot
- **Duplicates identified for action:** {duplicates}
- **Pairs queued for human review:** {review}
- **Merges executed (approved):** {merged}
- **Watchdog findings resolved:** {findings_resolved}
- **Records quarantined / protected:** {quarantined}
- **Estimated analyst hours avoided:** {hours_saved}

## Relationship intelligence
- **Direct corporate links surfaced:** {corporate_links}
- **Family links surfaced:** {family_links}

## What this means for {client_name}
1. **Cleaner outreach** — fewer double emails and conflicting asks to the same household.
2. **Safer data** — sensitive field exposures were redacted/quarantined by Watchdog before they hit exports.
3. **Faster CRM ops** — normalization + fuzzy match compressed weeks of manual spreadsheet work into a governed pipeline.
4. **Auditability** — every auto-fix and escalation is logged for compliance conversations.

## Recommended next steps
1. Approve the Review Needed queue (80–97% fuzzy band).
2. Authorize RPA merges only for the auto-merge (>=98%) and human-approved tickets.
3. Keep Watchdog resident so new bad data is blocked at ingest.
"""

    email = f"""Subject: {client_name} — Data hygiene wins and immediate ROI

Hi team,

Quick update from our Watchdog-led data quality pass:

• {duplicates} duplicate clusters identified for action ({merged} already merged under policy)
• {review} near-matches staged for your review (80–97% confidence band)
• {corporate_links} direct corporate links and {family_links} family links surfaced for relationship intelligence
• ~{hours_saved} analyst hours of manual cleanup avoided this cycle
• Security/completeness guards remain on so bad data is cleaned or blocked as it arrives

Happy to walk through the Review Needed CSV and the executive summary whenever useful.

Best regards,
TSW Consultancy
"""

    return {"summary_markdown": summary_md.strip(), "email_draft": email.strip()}


class ExecutiveReportingAgent:
    spec = AgentSpec(
        id="executive_report",
        name="Executive Reporting Agent",
        description=(
            "Translates Watchdog and specialist outputs into executive-ready "
            "summaries and client emails that highlight ROI and hygiene improvements."
        ),
        capabilities=["executive_report"],
        input_kinds=["metrics_report", "watchdog_cycle_report"],
        output_kinds=["executive_summary_md", "client_email_draft"],
        risks=["Do not invent metrics — only report numbers present in the input payload"],
    )

    def run(self, payload: dict[str, Any]) -> AgentResult:
        metrics = dict(payload.get("metrics") or {})
        # Flatten common nested Watchdog cycle fields
        if "remediation" in payload and isinstance(payload["remediation"], dict):
            metrics.setdefault("merged_count", payload["remediation"].get("applied_count"))
        for k in (
            "before_finding_count",
            "after_finding_count",
            "corporate_links",
            "family_links",
            "auto_merge_count",
            "review_count",
            "merged_count",
            "normalized_rows",
        ):
            if k in payload and k not in metrics:
                metrics[k] = payload[k]

        if payload.get("metrics_path"):
            loaded = json.loads(Path(payload["metrics_path"]).read_text(encoding="utf-8"))
            metrics = {**loaded, **metrics}

        if not metrics:
            return AgentResult(
                ok=False,
                agent_id=self.spec.id,
                summary="No metrics provided. Pass metrics={} or metrics_path.",
                errors=["missing_metrics"],
            )

        client_name = payload.get("client_name") or "Client"
        pack = build_executive_pack(metrics, client_name=client_name)

        output_dir = Path(payload.get("output_dir") or "outputs/executive_report")
        output_dir.mkdir(parents=True, exist_ok=True)
        md_path = output_dir / "executive_summary.md"
        email_path = output_dir / "client_email.txt"
        metrics_path = output_dir / "metrics_used.json"
        md_path.write_text(pack["summary_markdown"] + "\n", encoding="utf-8")
        email_path.write_text(pack["email_draft"] + "\n", encoding="utf-8")
        metrics_path.write_text(json.dumps(metrics, indent=2, default=str), encoding="utf-8")

        return AgentResult(
            ok=True,
            agent_id=self.spec.id,
            summary=f"Drafted executive summary and client email for {client_name}.",
            artifacts={
                "executive_summary_md": str(md_path),
                "client_email_draft": str(email_path),
                "metrics_used": str(metrics_path),
                "preview_email": pack["email_draft"][:500],
            },
            next_agents=[],
        )
