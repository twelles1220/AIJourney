"""
SAM UI playbook derived from the Loom merge walkthrough.

Selectors are intentionally label/role-oriented and may need tightening
against your live SAM DOM. Update SELECTORS after your first dry-run.
"""

from __future__ import annotations

from typing import Any

# Human-readable steps (always logged, even in dry-run)
MERGE_STEPS = [
    "Confirm Duplicate Records report/alert page is open in the attached browser",
    "Open Duplicate Records Alert → Duplicate Record for the target duplicate",
    "Switch to / open the master profile (SF Migration Contact ID when available)",
    "Copy master Birth Mother ID",
    "Return to the duplicate profile",
    "Open Advanced Options → Merge Birth Mother",
    "Paste master Birth Mother ID into the merge field",
    "Click Save → confirm Yes, merge these records",
    "Wait for SAM to finish (can be slow)",
    "Refresh and verify the duplicate no longer appears on the alert list",
]

# Best-effort selectors — tuned from live --inspect on Spence Chapin SAM.
# Prefer get_by_role / get_by_label text from your UI when possible.
SELECTORS: dict[str, Any] = {
    "duplicate_records_alert_link": {
        "role": "link",
        "name": "Duplicate Records Alert",
    },
    "duplicate_record_link": {
        "role": "link",
        "name": "Duplicate Record",
    },
    # Inspect showed this as a LINK named "ADVANCED OPTIONS" (all caps)
    "advanced_options": {
        "role": "link",
        "name": "ADVANCED OPTIONS",
    },
    "merge_birth_mother": {
        "text": "Merge Birth Mother",
    },
    # Exact control label may differ — update after inspecting the dialog
    "master_id_input": {
        "label": "Birth Mother ID",
    },
    "save_button": {
        "role": "button",
        "name": "Save",
    },
    "confirm_merge_button": {
        "role": "button",
        "name": "Yes, merge these records",
    },
}

# Profile URL pattern observed in inspect:
# https://spencechapin.mysamdb.com/SAM/Ch/Ch_M_Vw.aspx?chmid=7172
PROFILE_PATH_TEMPLATE = "/SAM/Ch/Ch_M_Vw.aspx?chmid={chmid}"


def choose_master(left: dict, right: dict) -> dict[str, Any]:
    """
    Apply Loom v1 master-selection rules.

    Returns {master, duplicate, reason, skip_reason}.
    """
    # Explicit notes conflict → skip
    if left.get("has_notes") and right.get("has_notes"):
        return {
            "master": None,
            "duplicate": None,
            "reason": None,
            "skip_reason": "both_profiles_have_notes",
        }

    left_sf = bool(left.get("sf_migration_contact_id"))
    right_sf = bool(right.get("sf_migration_contact_id"))

    if left_sf and not right_sf:
        return {"master": left, "duplicate": right, "reason": "sf_migration_on_left", "skip_reason": None}
    if right_sf and not left_sf:
        return {"master": right, "duplicate": left, "reason": "sf_migration_on_right", "skip_reason": None}

    if left_sf and right_sf:
        # Prefer lower birth mother ID
        try:
            left_id = int(str(left.get("birth_mother_id")))
            right_id = int(str(right.get("birth_mother_id")))
        except (TypeError, ValueError):
            left_id = right_id = None
        if left_id is not None and right_id is not None and left_id != right_id:
            if left_id < right_id:
                master, dup, reason = left, right, "lower_birth_mother_id_left"
            else:
                master, dup, reason = right, left, "lower_birth_mother_id_right"
            # Prefer children as a reinforcing signal
            if master.get("has_children"):
                reason += "+has_children"
            return {"master": master, "duplicate": dup, "reason": reason, "skip_reason": None}

    # Fallback: prefer children, else left-as-stated master
    if left.get("has_children") and not right.get("has_children"):
        return {"master": left, "duplicate": right, "reason": "has_children_left", "skip_reason": None}
    if right.get("has_children") and not left.get("has_children"):
        return {"master": right, "duplicate": left, "reason": "has_children_right", "skip_reason": None}

    return {
        "master": left,
        "duplicate": right,
        "reason": "explicit_or_fallback_left_as_master",
        "skip_reason": None,
    }


def should_skip_item(item: dict) -> str | None:
    """Return skip reason or None if runnable."""
    decision = (item.get("decision") or "").lower()
    if decision not in {"approved", "auto_merge"}:
        return f"decision_not_approved:{decision or 'missing'}"
    if (item.get("match_reason") or "").lower() == "phone":
        return "phone_matches_are_manual_in_v1"
    if item.get("skip_if"):
        return "flagged_in_skip_if"

    master = item.get("master") or {}
    duplicate = item.get("duplicate") or {}
    if not master.get("birth_mother_id") or not duplicate.get("birth_mother_id"):
        # Allow unresolved pairs if left/right present for chooser
        if item.get("left") and item.get("right"):
            choice = choose_master(item["left"], item["right"])
            return choice.get("skip_reason")
        return "missing_birth_mother_id"

    if master.get("has_notes") and duplicate.get("has_notes"):
        return "both_profiles_have_notes"
    return None
