"""
Watchdog Agent — scans CRM/database tables for data bloat and recommends
next actions that protect data quality, integrity, and security.

Mirrors the CRM chat agent's agentic loop + prompt caching pattern, but
exposes read-only audit tools over a sample (or pluggable) database.
"""

from __future__ import annotations

import json
from typing import Any

from watchdog_db import connect
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

_client: Any = None


def _get_client():
    """Lazy Anthropic client so detector-only paths need no SDK/API key."""
    global _client
    if _client is None:
        import anthropic

        _client = anthropic.Anthropic()
    return _client

# ---------------------------------------------------------------------------
# Tools — the bridge between Claude and the database scanners
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
]


def _run_scans(categories: list[str]) -> dict:
    with connect() as conn:
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
    """Dispatch a tool call against the sample DB. Swap connect() for prod DBs."""
    print(f"\n[WATCHDOG TOOL] → {name}")
    print(json.dumps(tool_input, indent=2))

    if name == "inspect_database":
        with connect() as conn:
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
                "integrity, then completeness and bloat reduction."
            ),
            "action_count": len(actions),
            "actions": actions,
        }
        return json.dumps(plan, indent=2, default=str)

    return json.dumps({"error": f"Unknown tool: {name}"})


SYSTEM_PROMPT = """\
You are Watchdog, an expert data-quality agent for nonprofit CRM and operational databases \
(Salesforce, Bloomerang, Kindful, and similar warehouses).

Your job:
1. Inspect the connected database schema.
2. Sift for data bloat and quality defects — duplicates, incomplete records, \
placeholder/unknown values, orphaned rows, stale profiles, and security exposures \
(PII/PAN in free text, over-retained identifiers).
3. Recommend a concrete next course of action that best protects \
**data quality**, **integrity**, and **security**.

CRITICAL BEHAVIORS:
- Grounding: Only cite findings returned by your tools. Never invent record IDs or counts.
- Priority: Security (critical) > integrity/duplicates > incompleteness/unknowns > low-impact bloat.
- Actionability: Every recommendation must name who/what to change (table, fields, record ids) \
and the control to prevent recurrence (constraint, validation, retention rule).
- Safety: You are read-only. Do not claim you deleted or merged data — propose the change.
- Formatting: Lead with an executive summary (counts by severity), then a numbered action plan, \
then optional detail. Use scannable markdown.
"""


def run_watchdog_agent(
    user_message: str,
    chat_history: list | None = None,
) -> str:
    """
    Run a single turn of the Watchdog agent.

    Returns the agent's final text reply after audit tool calls complete.
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
    """
    Non-LLM entry point: run detectors + remediation plan deterministically.

    Useful for cron/CI jobs and for demos without an API key.
    """
    audit = _run_scans(categories or ["all"])
    audit["remediation_plan"] = suggest_actions(audit["findings"])
    return audit


if __name__ == "__main__":
    import os
    import sys

    # Default: deterministic audit (no API key required) so the demo always works.
    # Pass --agent to exercise the full Claude tool loop.
    if "--agent" in sys.argv:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            print("ANTHROPIC_API_KEY is required for --agent mode.", file=sys.stderr)
            sys.exit(1)
        reply = run_watchdog_agent(
            "Audit the CRM database for data bloat and tell me what to do next "
            "to improve quality, integrity, and security."
        )
        print("\nWatchdog reply:")
        print(reply)
    else:
        report = run_audit_report()
        print(json.dumps(report, indent=2, default=str))
