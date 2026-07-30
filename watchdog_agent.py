"""
Watchdog Agent — continuous guardian for CRM data quality, integrity, and security.

Capabilities:
1. Audit databases for data bloat (duplicates, incomplete, unknowns, orphans, PII leaks)
2. Recommend prioritized remediation plans
3. Stay resident and auto-clean *safe* bad data (redact, normalize, purge fixtures, orphans)
4. Quarantine incomplete/tainted records and escalate merges that need a human
5. Guard new writes at ingest so bad data never lands

Mirrors the CRM chat agent's agentic loop + prompt caching pattern.
"""

from __future__ import annotations

import json
from typing import Any

from watchdog_db import connect, fetch_all, open_db
from watchdog_detectors import (
    inspect_inventory,
    run_full_audit,
    scan_duplicates,
    scan_incomplete,
    scan_referential_integrity,
    scan_security_risks,
    scan_unknown_values,
    suggest_actions,
)
from watchdog_guardian import (
    auto_remediate,
    guard_donor_ingest,
    insert_donor_guarded,
    run_continuous,
    run_guardian_cycle,
)

_client: Any = None


def _get_client():
    """Lazy Anthropic client so detector-only paths need no SDK/API key."""
    global _client
    if _client is None:
        import anthropic

        _client = anthropic.Anthropic()
    return _client


# ---------------------------------------------------------------------------
# Tools — audit + continuous guardian controls
# ---------------------------------------------------------------------------

WATCHDOG_TOOLS = [
    {
        "name": "inspect_database",
        "description": (
            "List tables, column schemas, and row counts for the connected database. "
            "Call this first to understand what you are auditing."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "scan_data_bloat",
        "description": (
            "Run one or more data-bloat detectors. Categories: "
            "duplicates, incomplete, unknown_values, integrity, security, or all. "
            "Returns structured findings with severity, record ids, and evidence."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "categories": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": [
                            "duplicates",
                            "incomplete",
                            "unknown_values",
                            "integrity",
                            "security",
                            "all",
                        ],
                    },
                    "description": "Which detectors to run. Use ['all'] for a full audit.",
                },
            },
            "required": ["categories"],
        },
    },
    {
        "name": "propose_remediation_plan",
        "description": (
            "Given findings from scan_data_bloat (or a fresh full audit if findings "
            "are omitted), produce a prioritized next-course-of-action plan focused "
            "on data quality, integrity, and security."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "findings": {
                    "type": "array",
                    "description": "Optional findings payload from a prior scan.",
                    "items": {"type": "object"},
                },
                "max_actions": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 20,
                    "description": "Max actions to return (default 8).",
                },
            },
        },
    },
    {
        "name": "apply_auto_remediation",
        "description": (
            "Apply the Watchdog guardian's safe auto-clean policies to the database: "
            "redact PAN/CVV, truncate full SSNs to last4, nullify placeholders, "
            "purge test fixtures, delete orphan child rows, quarantine incomplete records. "
            "Duplicate merges are escalated for humans — never auto-merged. "
            "Set dry_run=true to preview without writing."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dry_run": {
                    "type": "boolean",
                    "description": "If true, report what would change without mutating.",
                },
            },
        },
    },
    {
        "name": "guard_donor_write",
        "description": (
            "Validate a new donor record through the ingest guard. "
            "Rejects PCI/test/duplicate/invalid payloads; sanitizes placeholders; "
            "optionally inserts when decision is accept/accept_sanitized and insert=true."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "record": {
                    "type": "object",
                    "description": "Donor fields (first_name, last_name, email, phone, notes, ...).",
                },
                "insert": {
                    "type": "boolean",
                    "description": "If true, write the sanitized record when accepted.",
                },
            },
            "required": ["record"],
        },
    },
    {
        "name": "run_guardian_cycle",
        "description": (
            "Run one full resident-guardian cycle against the persistent DB: "
            "scan → auto-remediate → return before/after counts, applied actions, "
            "and open human escalations."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dry_run": {"type": "boolean"},
            },
        },
    },
    {
        "name": "list_escalations",
        "description": "List open human-escalation items (e.g. duplicate merges) from the guardian.",
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["open", "resolved", "all"],
                    "description": "Filter by escalation status (default open).",
                },
            },
        },
    },
]


def _run_scans(categories: list[str], *, memory: bool = True) -> dict:
    with connect(memory=memory) as conn:
        if "all" in categories:
            return run_full_audit(conn)

        findings = []
        mapping = {
            "duplicates": scan_duplicates,
            "incomplete": scan_incomplete,
            "unknown_values": scan_unknown_values,
            "integrity": scan_referential_integrity,
            "security": scan_security_risks,
        }
        for cat in categories:
            fn = mapping.get(cat)
            if fn:
                findings.extend(fn(conn))

        severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        findings.sort(
            key=lambda f: (severity_order.get(f["severity"], 9), f["category"], f["table"])
        )
        by_severity: dict[str, int] = {}
        by_category: dict[str, int] = {}
        for f in findings:
            by_severity[f["severity"]] = by_severity.get(f["severity"], 0) + 1
            by_category[f["category"]] = by_category.get(f["category"], 0) + 1
        return {
            "tables": inspect_inventory(conn),
            "finding_count": len(findings),
            "by_severity": by_severity,
            "by_category": by_category,
            "findings": findings,
        }


def _execute_tool(name: str, tool_input: dict) -> str:
    """Dispatch a tool call against the sample / persistent DB."""
    print(f"\n[WATCHDOG TOOL] → {name}")
    print(json.dumps(tool_input, indent=2))

    if name == "inspect_database":
        with connect(memory=True) as conn:
            result = inspect_inventory(conn)
        return json.dumps(result, indent=2)

    if name == "scan_data_bloat":
        categories = tool_input.get("categories") or ["all"]
        result = _run_scans(categories)
        return json.dumps(result, indent=2, default=str)

    if name == "propose_remediation_plan":
        findings = tool_input.get("findings")
        max_actions = int(tool_input.get("max_actions") or 8)
        if not findings:
            findings = _run_scans(["all"])["findings"]
        actions = suggest_actions(findings, max_actions=max_actions)
        plan = {
            "principle": (
                "Prioritize security exposure first, then referential/identity "
                "integrity, then completeness and bloat reduction. "
                "Auto-clean safe issues; escalate duplicate merges to humans."
            ),
            "action_count": len(actions),
            "actions": actions,
        }
        return json.dumps(plan, indent=2, default=str)

    if name == "apply_auto_remediation":
        dry_run = bool(tool_input.get("dry_run", False))
        with connect(memory=True) as conn:
            result = auto_remediate(conn, dry_run=dry_run)
        return json.dumps(result, indent=2, default=str)

    if name == "guard_donor_write":
        record = tool_input.get("record") or {}
        do_insert = bool(tool_input.get("insert", False))
        if do_insert:
            with connect(memory=True) as conn:
                result = insert_donor_guarded(conn, record)
            return json.dumps(result, indent=2, default=str)
        result = guard_donor_ingest(record)
        return json.dumps(result, indent=2, default=str)

    if name == "run_guardian_cycle":
        dry_run = bool(tool_input.get("dry_run", False))
        result = run_guardian_cycle(dry_run=dry_run)
        return json.dumps(result, indent=2, default=str)

    if name == "list_escalations":
        status = tool_input.get("status") or "open"
        conn = open_db(memory=False, seed=True)
        try:
            rows = fetch_all(conn, "watchdog_escalations")
            if status != "all":
                rows = [r for r in rows if r.get("status") == status]
            return json.dumps({"count": len(rows), "escalations": rows}, indent=2, default=str)
        finally:
            conn.close()

    return json.dumps({"error": f"Unknown tool: {name}"})


SYSTEM_PROMPT = """\
You are Watchdog, a resident data-quality guardian for nonprofit CRM and operational \
databases (Salesforce, Bloomerang, Kindful, and similar warehouses).

Your job:
1. Inspect schema and sift for data bloat — duplicates, incomplete records, \
placeholder/unknown values, orphaned rows, stale profiles, and security exposures.
2. Recommend (and when asked, execute) remediations that protect \
**data quality**, **integrity**, and **security**.
3. Stay in the system: use auto-remediation and ingest guards so bad data is cleaned \
or blocked as it appears. Escalate only what requires human judgment.

POLICY TIERS (never violate these):
- AUTO: redact PAN/CVV, truncate full SSN→last4, nullify placeholders, purge test fixtures, \
delete orphan child rows.
- QUARANTINE: soft-isolate incomplete/tainted donors from outreach/exports.
- ESCALATE: duplicate merges / identity collisions — propose a plan, do not auto-merge.
- REJECT: block PCI/test/invalid payloads at ingest.

CRITICAL BEHAVIORS:
- Grounding: Only cite findings/actions returned by your tools. Never invent record IDs.
- Priority: Security (critical) > integrity/duplicates > incompleteness/unknowns > bloat.
- Honesty: Distinguish what you auto-cleaned vs. what you escalated vs. what you only recommend.
- Formatting: Lead with an executive summary, then actions taken / escalations, then next steps.
"""


def run_watchdog_agent(
    user_message: str,
    chat_history: list | None = None,
) -> str:
    """
    Run a single turn of the Watchdog agent.

    Returns the agent's final text reply after tool calls complete.
    """
    messages = (chat_history or []) + [{"role": "user", "content": user_message}]

    client = _get_client()
    while True:
        with client.messages.stream(
            model="claude-opus-4-7",
            max_tokens=4096,
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=WATCHDOG_TOOLS,
            messages=messages,
        ) as stream:
            response = stream.get_final_message()

        tool_use_blocks = [b for b in response.content if b.type == "tool_use"]

        if not tool_use_blocks:
            return next((b.text for b in response.content if b.type == "text"), "")

        messages.append({"role": "assistant", "content": response.content})

        tool_results = []
        for block in tool_use_blocks:
            result = _execute_tool(block.name, block.input)
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                }
            )

        messages.append({"role": "user", "content": tool_results})


def run_audit_report(categories: list[str] | None = None) -> dict:
    """Deterministic audit + remediation plan (no API key required)."""
    audit = _run_scans(categories or ["all"])
    audit["remediation_plan"] = suggest_actions(audit["findings"])
    return audit


if __name__ == "__main__":
    import argparse
    import os
    import sys
    import tempfile
    from pathlib import Path

    parser = argparse.ArgumentParser(description="Watchdog data-quality guardian")
    parser.add_argument(
        "--agent",
        action="store_true",
        help="Run the Claude tool-loop (requires ANTHROPIC_API_KEY)",
    )
    parser.add_argument(
        "--remediate",
        action="store_true",
        help="Run one auto-remediation pass on an ephemeral seeded DB and print the report",
    )
    parser.add_argument(
        "--guardian-cycle",
        action="store_true",
        help="Run one persistent-DB guardian cycle (scan + auto-clean + escalate)",
    )
    parser.add_argument(
        "--continuous",
        action="store_true",
        help="Keep Watchdog resident: repeated guardian cycles",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=5.0,
        help="Seconds between continuous cycles (default 5)",
    )
    parser.add_argument(
        "--max-cycles",
        type=int,
        default=3,
        help="Max cycles for --continuous (default 3; use 0 for forever)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview remediations without writing",
    )
    parser.add_argument(
        "--ingest-demo",
        action="store_true",
        help="Demo ingest guard accepting/rejecting sample donor writes",
    )
    parser.add_argument(
        "--db",
        type=str,
        default=None,
        help="Persistent SQLite path (default: watchdog_crm.db)",
    )
    args = parser.parse_args()

    if args.agent:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            print("ANTHROPIC_API_KEY is required for --agent mode.", file=sys.stderr)
            sys.exit(1)
        reply = run_watchdog_agent(
            "Audit the CRM database, auto-clean anything safe, and tell me what "
            "still needs a human for quality, integrity, and security."
        )
        print("\nWatchdog reply:")
        print(reply)
    elif args.ingest_demo:
        samples = [
            {
                "first_name": "Clean",
                "last_name": "Donor",
                "email": "clean.donor@example.com",
                "phone": "555-0200",
                "notes": "Interested in volunteering",
            },
            {
                "first_name": "Bad",
                "last_name": "Card",
                "email": "bad.card@example.com",
                "notes": "Card 4111-1111-1111-1111 CVV 123",
            },
            {
                "first_name": "Test",
                "last_name": "User",
                "email": "test@test.com",
            },
            {
                "first_name": "Pat",
                "last_name": "Lee",
                "email": "pat.lee@example.com",
                "ssn_last4": "123456789",
                "city": "N/A",
            },
        ]
        with connect(memory=True) as conn:
            for sample in samples:
                result = insert_donor_guarded(conn, sample)
                print(json.dumps({"input": sample, "result": result}, indent=2, default=str))
    elif args.remediate:
        with connect(memory=True) as conn:
            before = run_full_audit(conn)["finding_count"]
            report = auto_remediate(conn, dry_run=args.dry_run)
            after = run_full_audit(conn)["finding_count"]
        print(
            json.dumps(
                {"before": before, "after": after, "remediation": report},
                indent=2,
                default=str,
            )
        )
    elif args.guardian_cycle or args.continuous:
        db_path = args.db
        if db_path is None:
            # Use a temp DB for demos so we don't clobber a developer's file unexpectedly
            db_path = str(Path(tempfile.gettempdir()) / "watchdog_guardian_demo.db")
            if Path(db_path).exists():
                Path(db_path).unlink()
        if args.continuous:
            max_cycles = None if args.max_cycles == 0 else args.max_cycles
            run_continuous(
                interval_seconds=args.interval,
                max_cycles=max_cycles,
                db_path=db_path,
                dry_run=args.dry_run,
                stop_when_clean=True,
            )
        else:
            report = run_guardian_cycle(db_path=db_path, dry_run=args.dry_run)
            print(json.dumps(report, indent=2, default=str))
    else:
        report = run_audit_report()
        print(json.dumps(report, indent=2, default=str))
