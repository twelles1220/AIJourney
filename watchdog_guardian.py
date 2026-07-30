"""
Watchdog Guardian — keeps data quality, integrity, and security enforced over time.

Policy tiers
------------
AUTO        Safe mechanical cleans applied without human approval
            (redact PAN/CVV, truncate full SSN→last4, null placeholders,
             purge test fixtures, delete orphan child rows).
QUARANTINE  Soft-isolate records (exclude from outreach/exports) when data
            is incomplete or tainted but still worth keeping for review.
ESCALATE    Needs a human (duplicate merges, identity collisions).
REJECT      Block at ingest — never let the bad row land.

The guardian can run:
- as an ingest filter in front of CRM writes
- as a one-shot remediation pass
- as a continuous daemon (scan → remediate → sleep → repeat)
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Callable

from watchdog_db import (
    DEFAULT_DB_PATH,
    fetch_all,
    open_db,
    utc_now,
    write_audit,
    write_escalation,
)
from watchdog_detectors import (
    CARD_RE,
    CVV_RE,
    PLACEHOLDER_EMAILS,
    PLACEHOLDER_VALUES,
    SSN_FULL_RE,
    run_full_audit,
)

# Fields we will auto-null when they contain placeholder junk
NORMALIZABLE_DONOR_FIELDS = (
    "first_name",
    "last_name",
    "phone",
    "status",
    "address_line1",
    "city",
    "state",
    "postal_code",
    "ssn_last4",
    "notes",
)


def _is_placeholder(value: Any) -> bool:
    if value is None:
        return False
    return str(value).strip().lower() in PLACEHOLDER_VALUES


def _redact_sensitive_text(text: str) -> tuple[str, bool]:
    """Strip PAN/CVV-like material from free text. Returns (new_text, changed)."""
    if not text:
        return text, False
    new = CARD_RE.sub("[REDACTED_PAN]", text)
    new = CVV_RE.sub("[REDACTED_CVV]", new)
    return new, new != text


def _normalize_ssn(value: Any) -> tuple[Any, bool, str | None]:
    """
    If a full 9-digit SSN landed in ssn_last4, keep only last4.
    Returns (value, changed, reject_reason).
    """
    if value is None:
        return None, False, None
    raw = str(value).strip()
    if _is_placeholder(raw):
        return None, True, None
    digits = re.sub(r"\D", "", raw)
    if SSN_FULL_RE.match(digits):
        return digits[-4:], True, None
    if len(digits) > 4:
        # Ambiguous — reject at ingest rather than guess
        return value, False, "ssn_last4 must be at most 4 digits (full SSN not allowed)"
    return raw, False, None


def guard_donor_ingest(record: dict, *, existing_emails: set[str] | None = None) -> dict:
    """
    Validate + sanitize a donor payload before it is written.

    Returns:
      {
        "decision": "accept" | "accept_sanitized" | "reject",
        "record": <possibly cleaned record>,
        "violations": [...],
        "auto_fixes": [...],
      }
    """
    cleaned = dict(record)
    violations: list[str] = []
    auto_fixes: list[str] = []

    # --- hard rejects ---
    notes = cleaned.get("notes") or ""
    if CARD_RE.search(notes) or CVV_RE.search(notes):
        violations.append("notes contain payment card / CVV data (PCI)")
    email = (cleaned.get("email") or "").strip().lower()
    if email in PLACEHOLDER_EMAILS or email.startswith("test@"):
        violations.append(f"email '{email}' looks like a test/placeholder address")
    if email and ("@" not in email or email.endswith("@") or email.startswith("@")):
        violations.append(f"email '{email}' has invalid format")
    name = f"{cleaned.get('first_name') or ''} {cleaned.get('last_name') or ''}".strip().lower()
    if name in {"test user", "unknown donor", "test test"}:
        violations.append(f"name '{name}' looks like a test/placeholder identity")

    ssn, ssn_changed, ssn_reject = _normalize_ssn(cleaned.get("ssn_last4"))
    if ssn_reject:
        violations.append(ssn_reject)
    elif ssn_changed:
        cleaned["ssn_last4"] = ssn
        auto_fixes.append("truncated/normalized ssn_last4")

    if existing_emails is not None and email and email in existing_emails:
        violations.append(f"duplicate email '{email}' already exists")

    if violations:
        return {
            "decision": "reject",
            "record": cleaned,
            "violations": violations,
            "auto_fixes": auto_fixes,
        }

    # --- soft sanitization ---
    for field in NORMALIZABLE_DONOR_FIELDS:
        if field == "ssn_last4":
            continue
        val = cleaned.get(field)
        if _is_placeholder(val):
            cleaned[field] = None
            auto_fixes.append(f"nullified placeholder {field}")

    if email in PLACEHOLDER_EMAILS:
        # already rejected above; keep for clarity
        pass

    notes_new, notes_changed = _redact_sensitive_text(cleaned.get("notes") or "")
    if notes_changed:
        cleaned["notes"] = notes_new
        auto_fixes.append("redacted sensitive material from notes")

    # Require minimum identity for a trusted active record
    if not (cleaned.get("last_name") and cleaned.get("email")):
        cleaned["quarantined"] = 1
        cleaned["quarantine_reason"] = cleaned.get("quarantine_reason") or (
            "incomplete identity at ingest"
        )
        auto_fixes.append("quarantined incomplete identity")

    decision = "accept_sanitized" if auto_fixes else "accept"
    return {
        "decision": decision,
        "record": cleaned,
        "violations": [],
        "auto_fixes": auto_fixes,
    }


def insert_donor_guarded(conn, record: dict) -> dict:
    """Run ingest guard, then insert if accepted. Logs every decision."""
    existing = {
        (r["email"] or "").strip().lower()
        for r in fetch_all(conn, "donors")
        if r.get("email")
    }
    result = guard_donor_ingest(record, existing_emails=existing)
    decision = result["decision"]

    if decision == "reject":
        write_audit(
            conn,
            action="ingest_reject",
            policy_tier="REJECT",
            table_name="donors",
            detail="; ".join(result["violations"]),
            before=record,
        )
        conn.commit()
        return {**result, "inserted_id": None}

    cleaned = result["record"]
    now = utc_now()
    cur = conn.execute(
        """
        INSERT INTO donors (
            first_name, last_name, email, phone, status,
            address_line1, city, state, postal_code, ssn_last4, notes,
            quarantined, quarantine_reason, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            cleaned.get("first_name"),
            cleaned.get("last_name"),
            cleaned.get("email"),
            cleaned.get("phone"),
            cleaned.get("status") or "prospect",
            cleaned.get("address_line1"),
            cleaned.get("city"),
            cleaned.get("state"),
            cleaned.get("postal_code"),
            cleaned.get("ssn_last4"),
            cleaned.get("notes"),
            int(cleaned.get("quarantined") or 0),
            cleaned.get("quarantine_reason"),
            now,
            now,
        ),
    )
    new_id = cur.lastrowid
    write_audit(
        conn,
        action="ingest_accept",
        policy_tier="AUTO" if result["auto_fixes"] else "PASS",
        table_name="donors",
        record_id=new_id,
        detail="; ".join(result["auto_fixes"]) or "accepted clean",
        before=record,
        after={**cleaned, "id": new_id},
    )
    conn.commit()
    return {**result, "inserted_id": new_id}


def _quarantine_donor(conn, donor_id: int, reason: str) -> dict | None:
    row = conn.execute("SELECT * FROM donors WHERE id=?", (donor_id,)).fetchone()
    if not row:
        return None
    before = dict(row)
    if before.get("quarantined"):
        return None
    conn.execute(
        """
        UPDATE donors
        SET quarantined=1, quarantine_reason=?, updated_at=?
        WHERE id=?
        """,
        (reason, utc_now(), donor_id),
    )
    after = dict(conn.execute("SELECT * FROM donors WHERE id=?", (donor_id,)).fetchone())
    write_audit(
        conn,
        action="quarantine",
        policy_tier="QUARANTINE",
        table_name="donors",
        record_id=donor_id,
        detail=reason,
        before=before,
        after=after,
    )
    return {"action": "quarantine", "record_id": donor_id, "reason": reason}


def auto_remediate(conn, *, dry_run: bool = False) -> dict:
    """
    Apply safe automatic cleans for findings in the current DB.

    Returns a structured report of applied / escalated / skipped actions.
    """
    audit = run_full_audit(conn)
    applied: list[dict] = []
    escalated: list[dict] = []
    skipped: list[dict] = []

    # Track donors already handled this pass
    touched: set[int] = set()

    for finding in audit["findings"]:
        cat = finding["category"]
        table = finding["table"]
        ids = list(finding.get("record_ids") or [])

        # ---- SECURITY: redact + quarantine (AUTO) ----
        if cat == "security" and table == "donors":
            for donor_id in ids:
                row = conn.execute(
                    "SELECT * FROM donors WHERE id=?", (donor_id,)
                ).fetchone()
                if not row:
                    continue
                before = dict(row)
                after = dict(before)
                changes = []

                ssn, ssn_changed, _ = _normalize_ssn(before.get("ssn_last4"))
                if ssn_changed:
                    after["ssn_last4"] = ssn
                    changes.append("truncated_ssn_to_last4")

                notes_new, notes_changed = _redact_sensitive_text(before.get("notes") or "")
                if notes_changed:
                    after["notes"] = notes_new
                    changes.append("redact_pan_cvv_in_notes")

                if not changes and before.get("quarantined"):
                    skipped.append({"finding": finding["title"], "reason": "already clean/quarantined"})
                    continue

                after["quarantined"] = 1
                after["quarantine_reason"] = (
                    before.get("quarantine_reason")
                    or finding["title"]
                )
                changes.append("quarantine_security")

                entry = {
                    "tier": "AUTO",
                    "action": "secure_and_quarantine",
                    "record_id": donor_id,
                    "changes": changes,
                    "finding": finding["title"],
                }
                if dry_run:
                    applied.append({**entry, "dry_run": True})
                else:
                    conn.execute(
                        """
                        UPDATE donors
                        SET ssn_last4=?, notes=?, quarantined=1,
                            quarantine_reason=?, updated_at=?
                        WHERE id=?
                        """,
                        (
                            after["ssn_last4"],
                            after["notes"],
                            after["quarantine_reason"],
                            utc_now(),
                            donor_id,
                        ),
                    )
                    write_audit(
                        conn,
                        action="auto_secure",
                        policy_tier="AUTO",
                        table_name="donors",
                        record_id=donor_id,
                        detail=", ".join(changes),
                        before=before,
                        after=after,
                    )
                    applied.append(entry)
                touched.add(donor_id)
            continue

        # ---- UNKNOWN / TEST FIXTURES: purge (AUTO) ----
        if cat == "unknown_value" and "test/QA" in finding["title"]:
            for donor_id in ids:
                row = conn.execute(
                    "SELECT * FROM donors WHERE id=?", (donor_id,)
                ).fetchone()
                if not row:
                    continue
                before = dict(row)
                entry = {
                    "tier": "AUTO",
                    "action": "purge_test_fixture",
                    "record_id": donor_id,
                    "finding": finding["title"],
                }
                if dry_run:
                    applied.append({**entry, "dry_run": True})
                else:
                    # Soft-delete: quarantine + status marker (keep audit trail)
                    conn.execute(
                        """
                        UPDATE donors
                        SET status='purged_test', quarantined=1,
                            quarantine_reason=?, email=NULL, phone=NULL,
                            updated_at=?
                        WHERE id=?
                        """,
                        ("auto-purged test/QA fixture", utc_now(), donor_id),
                    )
                    after = dict(
                        conn.execute(
                            "SELECT * FROM donors WHERE id=?", (donor_id,)
                        ).fetchone()
                    )
                    write_audit(
                        conn,
                        action="purge_test_fixture",
                        policy_tier="AUTO",
                        table_name="donors",
                        record_id=donor_id,
                        detail="soft-deleted test fixture",
                        before=before,
                        after=after,
                    )
                    applied.append(entry)
                touched.add(donor_id)
            continue

        # ---- PLACEHOLDERS: nullify (AUTO) ----
        if cat == "unknown_value" and table == "donors":
            for donor_id in ids:
                if donor_id in touched:
                    continue
                row = conn.execute(
                    "SELECT * FROM donors WHERE id=?", (donor_id,)
                ).fetchone()
                if not row:
                    continue
                before = dict(row)
                after = dict(before)
                changed_fields = []
                for field in NORMALIZABLE_DONOR_FIELDS:
                    if _is_placeholder(after.get(field)):
                        after[field] = None
                        changed_fields.append(field)
                email = (after.get("email") or "").strip().lower()
                if email in PLACEHOLDER_EMAILS:
                    after["email"] = None
                    changed_fields.append("email")
                if not changed_fields:
                    continue
                entry = {
                    "tier": "AUTO",
                    "action": "nullify_placeholders",
                    "record_id": donor_id,
                    "fields": changed_fields,
                    "finding": finding["title"],
                }
                if dry_run:
                    applied.append({**entry, "dry_run": True})
                else:
                    sets = ", ".join(f"{f}=?" for f in changed_fields)
                    values = [after[f] for f in changed_fields] + [utc_now(), donor_id]
                    conn.execute(
                        f"UPDATE donors SET {sets}, updated_at=? WHERE id=?",
                        values,
                    )
                    write_audit(
                        conn,
                        action="nullify_placeholders",
                        policy_tier="AUTO",
                        table_name="donors",
                        record_id=donor_id,
                        detail=", ".join(changed_fields),
                        before=before,
                        after=after,
                    )
                    applied.append(entry)
            continue

        # ---- INTEGRITY orphans: delete child rows (AUTO) ----
        if cat == "integrity" and table in {"donations", "interactions"}:
            for row_id in ids:
                row = conn.execute(
                    f"SELECT * FROM {table} WHERE id=?", (row_id,)
                ).fetchone()
                if not row:
                    continue
                before = dict(row)
                entry = {
                    "tier": "AUTO",
                    "action": "delete_orphan",
                    "table": table,
                    "record_id": row_id,
                    "finding": finding["title"],
                }
                if dry_run:
                    applied.append({**entry, "dry_run": True})
                else:
                    conn.execute(f"DELETE FROM {table} WHERE id=?", (row_id,))
                    write_audit(
                        conn,
                        action="delete_orphan",
                        policy_tier="AUTO",
                        table_name=table,
                        record_id=row_id,
                        detail=finding["detail"],
                        before=before,
                    )
                    applied.append(entry)
            continue

        # ---- INCOMPLETE: quarantine (QUARANTINE) ----
        if cat == "incomplete" and table == "donors":
            for donor_id in ids:
                if donor_id in touched:
                    continue
                if dry_run:
                    applied.append(
                        {
                            "tier": "QUARANTINE",
                            "action": "quarantine_incomplete",
                            "record_id": donor_id,
                            "finding": finding["title"],
                            "dry_run": True,
                        }
                    )
                else:
                    result = _quarantine_donor(
                        conn, donor_id, f"incomplete: {finding['detail']}"
                    )
                    if result:
                        applied.append(
                            {
                                "tier": "QUARANTINE",
                                **result,
                                "finding": finding["title"],
                            }
                        )
                    else:
                        skipped.append(
                            {
                                "finding": finding["title"],
                                "record_id": donor_id,
                                "reason": "already quarantined or missing",
                            }
                        )
            continue

        # ---- DUPLICATES / BLOAT: escalate (needs human) ----
        if cat in {"duplicate", "bloat"}:
            entry = {
                "tier": "ESCALATE",
                "action": "needs_human_review",
                "finding": finding["title"],
                "record_ids": ids,
                "detail": finding["detail"],
            }
            escalated.append(entry)
            if not dry_run:
                write_escalation(
                    conn,
                    category=cat,
                    severity=finding["severity"],
                    table_name=table,
                    record_ids=ids,
                    title=finding["title"],
                    detail=finding["detail"],
                )
            continue

        # Everything else left for humans
        skipped.append(
            {
                "finding": finding["title"],
                "category": cat,
                "reason": "no auto policy",
            }
        )

    if not dry_run:
        conn.commit()

    return {
        "dry_run": dry_run,
        "finding_count": audit["finding_count"],
        "by_severity": audit["by_severity"],
        "applied_count": len(applied),
        "escalated_count": len(escalated),
        "skipped_count": len(skipped),
        "applied": applied,
        "escalated": escalated,
        "skipped": skipped,
    }


def run_guardian_cycle(
    *,
    db_path: str | None = None,
    dry_run: bool = False,
    seed_if_empty: bool = True,
) -> dict:
    """One scan → remediate → report cycle against the persistent (or demo) DB."""
    path = db_path if db_path is not None else str(DEFAULT_DB_PATH)
    conn = open_db(path, seed=seed_if_empty, memory=False)
    try:
        before = run_full_audit(conn)
        remediation = auto_remediate(conn, dry_run=dry_run)
        after = run_full_audit(conn) if not dry_run else before
        open_escalations = fetch_all(conn, "watchdog_escalations")
        open_escalations = [e for e in open_escalations if e.get("status") == "open"]
        audit_tail = conn.execute(
            """
            SELECT * FROM watchdog_audit_log
            ORDER BY id DESC LIMIT 25
            """
        ).fetchall()
        return {
            "db_path": path,
            "ts": utc_now(),
            "before_finding_count": before["finding_count"],
            "after_finding_count": after["finding_count"],
            "findings_resolved": before["finding_count"] - after["finding_count"],
            "remediation": remediation,
            "open_escalations": open_escalations,
            "recent_audit_log": [dict(r) for r in audit_tail],
        }
    finally:
        conn.close()


def run_continuous(
    *,
    interval_seconds: float = 30.0,
    max_cycles: int | None = None,
    db_path: str | None = None,
    dry_run: bool = False,
    on_cycle: Callable[[dict], None] | None = None,
    stop_when_clean: bool = False,
) -> list[dict]:
    """
    Keep Watchdog resident: repeatedly scan and auto-clean.

    - interval_seconds: sleep between cycles
    - max_cycles: stop after N cycles (None = forever)
    - stop_when_clean: exit early when no findings remain and no open escalations
    """
    cycles: list[dict] = []
    n = 0
    while max_cycles is None or n < max_cycles:
        n += 1
        report = run_guardian_cycle(db_path=db_path, dry_run=dry_run)
        cycles.append(report)
        if on_cycle:
            on_cycle(report)
        else:
            print(
                json.dumps(
                    {
                        "cycle": n,
                        "ts": report["ts"],
                        "before": report["before_finding_count"],
                        "after": report["after_finding_count"],
                        "applied": report["remediation"]["applied_count"],
                        "escalated": report["remediation"]["escalated_count"],
                        "open_escalations": len(report["open_escalations"]),
                    },
                    indent=2,
                )
            )

        if stop_when_clean and report["after_finding_count"] == 0 and not report["open_escalations"]:
            break
        # Even with remaining escalations, auto-cleanable issues may be done
        if stop_when_clean and report["remediation"]["applied_count"] == 0 and n > 1:
            # Steady state: only human escalations left
            break

        if max_cycles is not None and n >= max_cycles:
            break
        time.sleep(interval_seconds)
    return cycles
