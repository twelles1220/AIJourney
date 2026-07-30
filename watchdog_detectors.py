"""
Deterministic data-bloat detectors for the Watchdog agent.

Each detector returns structured Finding dicts the LLM can reason over
when recommending remediation. Severity ranks security risks highest.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from watchdog_db import fetch_all, list_tables, row_count, table_schema

PLACEHOLDER_VALUES = {
    "",
    "n/a",
    "na",
    "none",
    "null",
    "unknown",
    "tbd",
    "test",
    "xxx",
    "xxxx",
    "00000",
    "99999",
    "000-000-0000",
    "555-0000",
}

PLACEHOLDER_EMAILS = {
    "unknown@unknown.com",
    "test@test.com",
    "n/a@n/a.com",
}

# Patterns that suggest sensitive data stored unsafely
SSN_FULL_RE = re.compile(r"^\d{9}$")
CARD_RE = re.compile(r"\b(?:\d[ -]*?){13,19}\b")
CVV_RE = re.compile(r"\bCVV\s*:?\s*\d{3,4}\b", re.IGNORECASE)


def _finding(
    category: str,
    severity: str,
    table: str,
    title: str,
    detail: str,
    record_ids: list[Any] | None = None,
    fields: list[str] | None = None,
    evidence: dict | None = None,
) -> dict:
    return {
        "category": category,
        "severity": severity,  # critical | high | medium | low
        "table": table,
        "title": title,
        "detail": detail,
        "record_ids": record_ids or [],
        "fields": fields or [],
        "evidence": evidence or {},
    }


def _active_donors(conn: sqlite3.Connection) -> list[dict]:
    """Donors still in play (exclude auto-purged test fixtures)."""
    return [
        d
        for d in fetch_all(conn, "donors")
        if (d.get("status") or "") != "purged_test"
    ]


def scan_duplicates(conn: sqlite3.Connection) -> list[dict]:
    findings: list[dict] = []
    donors = _active_donors(conn)

    # Exact email duplicates (ignore null/blank)
    by_email: dict[str, list[dict]] = {}
    for d in donors:
        email = (d.get("email") or "").strip().lower()
        if not email:
            continue
        by_email.setdefault(email, []).append(d)

    for email, group in by_email.items():
        if len(group) < 2:
            continue
        ids = [g["id"] for g in group]
        findings.append(
            _finding(
                category="duplicate",
                severity="high",
                table="donors",
                title="Duplicate donor email",
                detail=(
                    f"{len(group)} donor records share email '{email}'. "
                    "This risks double-counting gifts and conflicting outreach."
                ),
                record_ids=ids,
                fields=["email"],
                evidence={"email": email, "names": [f"{g['first_name']} {g['last_name']}" for g in group]},
            )
        )

    # Exact phone duplicates
    by_phone: dict[str, list[dict]] = {}
    for d in donors:
        phone = (d.get("phone") or "").strip()
        if not phone or phone.lower() in PLACEHOLDER_VALUES:
            continue
        by_phone.setdefault(phone, []).append(d)

    for phone, group in by_phone.items():
        if len(group) < 2:
            continue
        # Skip if already covered by same-email group with identical ids
        ids = [g["id"] for g in group]
        findings.append(
            _finding(
                category="duplicate",
                severity="medium",
                table="donors",
                title="Duplicate donor phone",
                detail=f"{len(group)} donor records share phone '{phone}'.",
                record_ids=ids,
                fields=["phone"],
                evidence={"phone": phone},
            )
        )

    # Near-name + same last4 SSN (identity collision)
    by_ssn: dict[str, list[dict]] = {}
    for d in donors:
        ssn = (d.get("ssn_last4") or "").strip()
        if not ssn or ssn.lower() in PLACEHOLDER_VALUES:
            continue
        by_ssn.setdefault(ssn, []).append(d)
    for ssn, group in by_ssn.items():
        if len(group) < 2:
            continue
        findings.append(
            _finding(
                category="duplicate",
                severity="high",
                table="donors",
                title="Possible identity collision on SSN last4",
                detail=(
                    f"{len(group)} records share ssn_last4 '{ssn}'. "
                    "Verify before merge — could be twins/family or true duplicates."
                ),
                record_ids=[g["id"] for g in group],
                fields=["ssn_last4", "first_name", "last_name"],
                evidence={"ssn_last4": ssn},
            )
        )

    return findings


def scan_incomplete(conn: sqlite3.Connection) -> list[dict]:
    findings: list[dict] = []
    donors = _active_donors(conn)
    requiredish = ["first_name", "last_name", "email", "phone", "status"]

    for d in donors:
        missing = []
        for field in requiredish:
            val = d.get(field)
            if val is None or (isinstance(val, str) and not val.strip()):
                missing.append(field)
        # Broken email shape
        email = (d.get("email") or "").strip()
        if email and ("@" not in email or email.endswith("@") or email.startswith("@")):
            missing.append("email(invalid_format)")

        if missing:
            severity = "high" if "email" in missing or "email(invalid_format)" in missing else "medium"
            findings.append(
                _finding(
                    category="incomplete",
                    severity=severity,
                    table="donors",
                    title=f"Incomplete donor record #{d['id']}",
                    detail=f"Missing or invalid fields: {', '.join(missing)}",
                    record_ids=[d["id"]],
                    fields=[m.split("(")[0] for m in missing],
                    evidence={"missing": missing},
                )
            )

    donations = fetch_all(conn, "donations")
    for don in donations:
        problems = []
        if don.get("amount") is None or don.get("amount") <= 0:
            problems.append("amount")
        if not don.get("campaign") or str(don["campaign"]).strip().lower() in PLACEHOLDER_VALUES:
            problems.append("campaign")
        if problems:
            findings.append(
                _finding(
                    category="incomplete",
                    severity="medium",
                    table="donations",
                    title=f"Incomplete donation record #{don['id']}",
                    detail=f"Weak/missing fields: {', '.join(problems)}",
                    record_ids=[don["id"]],
                    fields=problems,
                )
            )

    return findings


def scan_unknown_values(conn: sqlite3.Connection) -> list[dict]:
    findings: list[dict] = []
    donors = _active_donors(conn)
    text_fields = [
        "first_name", "last_name", "email", "phone", "status",
        "address_line1", "city", "state", "postal_code", "ssn_last4", "notes",
    ]

    for d in donors:
        hits = []
        for field in text_fields:
            raw = d.get(field)
            if raw is None:
                continue
            val = str(raw).strip().lower()
            if val in PLACEHOLDER_VALUES:
                hits.append(field)
            elif field == "email" and val in PLACEHOLDER_EMAILS:
                hits.append(field)
        if hits:
            findings.append(
                _finding(
                    category="unknown_value",
                    severity="medium",
                    table="donors",
                    title=f"Placeholder / unknown values on donor #{d['id']}",
                    detail=f"Fields with unknown/placeholder content: {', '.join(hits)}",
                    record_ids=[d["id"]],
                    fields=hits,
                )
            )

    # Test / QA fixtures in production-like data
    for d in donors:
        name = f"{d.get('first_name') or ''} {d.get('last_name') or ''}".strip().lower()
        email = (d.get("email") or "").lower()
        if "test" in name or email.startswith("test@"):
            findings.append(
                _finding(
                    category="unknown_value",
                    severity="high",
                    table="donors",
                    title=f"Likely test/QA fixture still in dataset (donor #{d['id']})",
                    detail="Test identities inflate counts and can leak into outreach.",
                    record_ids=[d["id"]],
                    fields=["first_name", "last_name", "email"],
                    evidence={"name": name, "email": email},
                )
            )

    return findings


def scan_referential_integrity(conn: sqlite3.Connection) -> list[dict]:
    findings: list[dict] = []
    all_donors = fetch_all(conn, "donors")
    donor_ids = {d["id"] for d in all_donors}

    for table, fk in (("donations", "donor_id"), ("interactions", "donor_id")):
        for row in fetch_all(conn, table):
            parent = row.get(fk)
            if parent not in donor_ids:
                findings.append(
                    _finding(
                        category="integrity",
                        severity="high",
                        table=table,
                        title=f"Orphan {table} row #{row['id']}",
                        detail=f"{fk}={parent} does not exist in donors.",
                        record_ids=[row["id"]],
                        fields=[fk],
                        evidence={fk: parent},
                    )
                )

    # Donors with no activity (stale / possible bloat) — skip purged fixtures
    donation_donors = {r["donor_id"] for r in fetch_all(conn, "donations")}
    interaction_donors = {r["donor_id"] for r in fetch_all(conn, "interactions")}
    active = donation_donors | interaction_donors
    for d in _active_donors(conn):
        if d["id"] not in active:
            findings.append(
                _finding(
                    category="bloat",
                    severity="low",
                    table="donors",
                    title=f"Inactive / orphaned donor profile #{d['id']}",
                    detail=(
                        "No donations or interactions linked. "
                        "Review for archival vs. enrichment."
                    ),
                    record_ids=[d["id"]],
                    fields=[],
                    evidence={"status": d.get("status"), "updated_at": d.get("updated_at")},
                )
            )

    return findings


def scan_security_risks(conn: sqlite3.Connection) -> list[dict]:
    findings: list[dict] = []
    donors = _active_donors(conn)

    for d in donors:
        ssn = (d.get("ssn_last4") or "").strip()
        if SSN_FULL_RE.match(ssn):
            findings.append(
                _finding(
                    category="security",
                    severity="critical",
                    table="donors",
                    title=f"Full SSN stored in ssn_last4 (donor #{d['id']})",
                    detail=(
                        "Field intended for last-4 appears to hold a full 9-digit SSN. "
                        "Rotate/redact immediately and restrict column access."
                    ),
                    record_ids=[d["id"]],
                    fields=["ssn_last4"],
                )
            )

        notes = d.get("notes") or ""
        # Ignore already-redacted markers from prior guardian passes
        if "[REDACTED_PAN]" in notes or "[REDACTED_CVV]" in notes:
            live_notes = notes.replace("[REDACTED_PAN]", "").replace("[REDACTED_CVV]", "")
        else:
            live_notes = notes
        if CARD_RE.search(live_notes) or CVV_RE.search(live_notes):
            findings.append(
                _finding(
                    category="security",
                    severity="critical",
                    table="donors",
                    title=f"Payment card data found in free-text notes (donor #{d['id']})",
                    detail=(
                        "PAN/CVV-like data in notes violates PCI expectations. "
                        "Purge from notes, invalidate the credential if live, "
                        "and move retention to a compliant vault."
                    ),
                    record_ids=[d["id"]],
                    fields=["notes"],
                    evidence={"notes_excerpt": notes[:120]},
                )
            )

    # Card last4 present is OK; flag if payment_method unknown with card_last4
    for don in fetch_all(conn, "donations"):
        method = (don.get("payment_method") or "").lower()
        if don.get("card_last4") and method in ("unknown", "cash", ""):
            findings.append(
                _finding(
                    category="security",
                    severity="medium",
                    table="donations",
                    title=f"Card metadata inconsistent with payment method (donation #{don['id']})",
                    detail="card_last4 present while payment_method is non-card/unknown.",
                    record_ids=[don["id"]],
                    fields=["payment_method", "card_last4"],
                )
            )

    return findings


def inspect_inventory(conn: sqlite3.Connection) -> dict:
    tables = list_tables(conn)
    inventory = {}
    for t in tables:
        inventory[t] = {
            "row_count": row_count(conn, t),
            "columns": table_schema(conn, t),
        }
    return inventory


def run_full_audit(conn: sqlite3.Connection) -> dict:
    """Run all detectors and return a ranked, dedupe-light report."""
    findings: list[dict] = []
    findings.extend(scan_security_risks(conn))
    findings.extend(scan_duplicates(conn))
    findings.extend(scan_incomplete(conn))
    findings.extend(scan_unknown_values(conn))
    findings.extend(scan_referential_integrity(conn))

    severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    findings.sort(key=lambda f: (severity_order.get(f["severity"], 9), f["category"], f["table"]))

    summary = {
        "tables": inspect_inventory(conn),
        "finding_count": len(findings),
        "by_severity": {},
        "by_category": {},
        "findings": findings,
    }
    for f in findings:
        summary["by_severity"][f["severity"]] = summary["by_severity"].get(f["severity"], 0) + 1
        summary["by_category"][f["category"]] = summary["by_category"].get(f["category"], 0) + 1
    return summary


# Default remediation playbook keyed by category — agent may refine further.
REMEDIATION_PLAYBOOK = {
    "security": [
        "Quarantine affected records from marketing exports immediately.",
        "Redact or tokenize sensitive fields; never store PAN/CVV in CRM notes.",
        "Rotate any exposed credentials and notify compliance/security owners.",
        "Add column-level encryption / access controls and field validation.",
    ],
    "duplicate": [
        "Pick a survivor record using recency + completeness scoring.",
        "Merge gifts/interactions onto the survivor; soft-delete or archive losers.",
        "Add unique constraints (email) and a pre-insert dedupe check.",
    ],
    "incomplete": [
        "Backfill via enrichment or outreach only where consent allows.",
        "Block writes that omit required identity fields at the adapter layer.",
        "Flag records below a completeness threshold so agents don't treat them as trusted.",
    ],
    "unknown_value": [
        "Normalize placeholders to NULL; ban 'Unknown'/'N/A' at ingest.",
        "Purge leftover test/QA fixtures from production datasets.",
        "Require enumerated statuses instead of free-text unknowns.",
    ],
    "integrity": [
        "Delete or re-parent orphan child rows; add FK constraints if the store supports them.",
        "Run nightly referential integrity checks in the Watchdog schedule.",
    ],
    "bloat": [
        "Archive long-inactive profiles per retention policy.",
        "Separate prospects needing enrichment from true dead weight.",
    ],
}


def suggest_actions(findings: list[dict], max_actions: int = 8) -> list[dict]:
    """
    Turn findings into a prioritized next-course-of-action list.

    Security findings always float to the top. Actions are deduped by
    (category, title pattern) so the agent gets a crisp plan.
    """
    severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    ranked = sorted(
        findings,
        key=lambda f: (severity_order.get(f["severity"], 9), f["category"]),
    )

    actions: list[dict] = []
    seen_keys: set[str] = set()

    for f in ranked:
        key = f"{f['category']}:{f['title'].split('(')[0].strip().lower()}"
        # Collapse many incomplete/unknown row-level findings into one action class
        class_key = f["category"]
        if class_key in seen_keys and f["category"] in {
            "incomplete", "unknown_value", "bloat", "duplicate"
        }:
            # Still allow distinct security/integrity titles through
            if f["category"] != "security":
                continue
        seen_keys.add(class_key)

        playbook = REMEDIATION_PLAYBOOK.get(f["category"], ["Investigate and remediate."])
        actions.append(
            {
                "priority": len(actions) + 1,
                "severity": f["severity"],
                "category": f["category"],
                "headline": f["title"],
                "why": f["detail"],
                "affects": {
                    "table": f["table"],
                    "record_ids": f.get("record_ids", [])[:20],
                    "fields": f.get("fields", []),
                },
                "recommended_steps": playbook,
                "quality_goals": _goals_for(f["category"]),
            }
        )
        if len(actions) >= max_actions:
            break

    return actions


def _goals_for(category: str) -> list[str]:
    mapping = {
        "security": ["security", "integrity"],
        "duplicate": ["integrity", "quality"],
        "incomplete": ["quality"],
        "unknown_value": ["quality", "integrity"],
        "integrity": ["integrity"],
        "bloat": ["quality"],
    }
    return mapping.get(category, ["quality"])
