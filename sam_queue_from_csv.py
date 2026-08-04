#!/usr/bin/env python3
"""
Build a SAM merge queue JSON from a Duplicate Records report CSV export.

Supports:
  1) Paired rows (left/right ID columns)
  2) Flat SAM export (one profile per row) — groups exact Full Name matches

Usage:
  python sam_queue_from_csv.py outputs\\duplicates.csv -o outputs\\sam_merge_queue.json --approve-name

Then batch:
  python sam_rpa_local.py --queue outputs\\sam_merge_queue.json --cdp http://127.0.0.1:9222 --live --confirm-live --limit 3
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from agents.sam_playbook import choose_master


# Paired-row header aliases
_ALIASES: dict[str, tuple[str, ...]] = {
    "left_id": (
        "left_birth_mother_id",
        "birth_mother_id_1",
        "bm_id_1",
        "id_1",
        "mother_id_1",
        "chmid_1",
        "left_id",
        "record_1_id",
        "profile_1_id",
    ),
    "right_id": (
        "right_birth_mother_id",
        "birth_mother_id_2",
        "bm_id_2",
        "id_2",
        "mother_id_2",
        "chmid_2",
        "right_id",
        "record_2_id",
        "profile_2_id",
    ),
    "left_name": (
        "left_name",
        "left_full_name",
        "name_1",
        "full_name_1",
        "birth_mother_1",
        "record_1_name",
        "profile_1_name",
    ),
    "right_name": (
        "right_name",
        "right_full_name",
        "name_2",
        "full_name_2",
        "birth_mother_2",
        "record_2_name",
        "profile_2_name",
    ),
    "left_sf": (
        "left_sf_migration_contact_id",
        "sf_migration_contact_id_1",
        "sf_id_1",
        "sf_1",
        "migration_contact_id_1",
    ),
    "right_sf": (
        "right_sf_migration_contact_id",
        "sf_migration_contact_id_2",
        "sf_id_2",
        "sf_2",
        "migration_contact_id_2",
    ),
    "match_reason": (
        "match_reason",
        "match_type",
        "match",
        "reason",
        "duplicate_type",
        "match_on",
    ),
    "left_notes": ("left_has_notes", "has_notes_1", "notes_1"),
    "right_notes": ("right_has_notes", "has_notes_2", "notes_2"),
    "left_children": ("left_has_children", "has_children_1", "children_1"),
    "right_children": ("right_has_children", "has_children_2", "children_2"),
}

# Flat SAM Duplicate Records export (one profile per row)
_FLAT_ALIASES: dict[str, tuple[str, ...]] = {
    "id": (
        "birth_mother_birth_mother_id",
        "birth_mother_id",
        "bm_id",
        "chmid",
    ),
    "name": (
        "birth_mother_birth_mother_full_name",
        "birth_mother_full_name",
        "full_name",
        "name",
    ),
    "sf": (
        "birth_mother_sf_migration_contact_id_bm",
        "birth_mother_sf_migration_contact_id",
        "sf_migration_contact_id_bm",
        "sf_migration_contact_id",
    ),
    "alerts": ("birth_mother_alerts", "alerts"),
    "case_count": ("birth_mother_case_case", "case"),
    "child_dob": (
        "birth_mother_case_child_date_of_birth",
        "child_date_of_birth",
    ),
    "phones": (
        "birth_mother_all_phone_numbers",
        "all_phone_numbers",
    ),
}


def _norm_header(h: str) -> str:
    # SAM uses "Birth Mother » Field" — treat » as a separator word
    h = (h or "").replace("»", " ").replace("¿", " ")
    return re.sub(r"[^a-z0-9]+", "_", h.strip().lower()).strip("_")


def _clean_text(value: Any) -> str:
    """Flatten Excel/CSV control characters so queue JSON stays valid."""
    if value is None:
        return ""
    text = str(value).replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    text = re.sub(r"[\x00-\x1f\x7f]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    s = _clean_text(value).lower()
    if not s:
        return False
    return s in {"1", "true", "yes", "y", "x", "has notes", "notes"}


def _sf_or_none(value: Any) -> str | None:
    s = _clean_text(value)
    if not s or s.lower() in {"null", "none", "n/a", "na", "-"}:
        return None
    return s


def _map_headers(fieldnames: list[str] | None, aliases: dict[str, tuple[str, ...]]) -> dict[str, str]:
    if not fieldnames:
        return {}
    by_norm = {_norm_header(h): h for h in fieldnames if h}
    mapping: dict[str, str] = {}
    for key, keys in aliases.items():
        for alias in keys:
            if alias in by_norm:
                mapping[key] = by_norm[alias]
                break
        if key in mapping:
            continue
        # suffix / contains fallback for SAM long headers
        for norm, original in by_norm.items():
            if norm == keys[0] or norm.endswith("_" + keys[0]) or keys[0] in norm:
                # Prefer exact-ish ends for id vs sf id
                if key == "id" and "sf_migration" in norm:
                    continue
                if key == "sf" and "org" in norm and "household" in norm:
                    continue
                mapping[key] = original
                break
    return mapping


def read_csv_rows(csv_path: Path) -> tuple[list[str], list[dict]]:
    raw = csv_path.read_bytes()
    text = None
    for enc in ("utf-8-sig", "utf-16", "utf-16-le", "cp1252", "latin-1"):
        try:
            candidate = raw.decode(enc)
            # UTF-16-LE mis-decode of UTF-8 usually has many NULs
            if enc.startswith("utf-16") and "\x00" in candidate[:200]:
                continue
            if enc == "utf-16-le" and not raw[:2] == b"\xff\xfe":
                # accept BOM-less UTF-16-LE (SAM export starts with '"\x00')
                if not (len(raw) >= 2 and raw[1] == 0):
                    continue
            text = candidate
            break
        except UnicodeError:
            continue
    if text is None:
        raise SystemExit(f"Could not decode CSV: {csv_path}")
    if text.startswith("\ufeff"):
        text = text[1:]
    reader = csv.DictReader(text.splitlines())
    fieldnames = [h for h in (reader.fieldnames or []) if h is not None]
    rows = list(reader)
    return fieldnames, rows


def _side(row: dict, mapping: dict[str, str], side: str) -> dict[str, Any]:
    id_key = f"{side}_id"
    name_key = f"{side}_name"
    sf_key = f"{side}_sf"
    notes_key = f"{side}_notes"
    children_key = f"{side}_children"
    return {
        "birth_mother_id": _clean_text(row.get(mapping.get(id_key, ""), "")),
        "full_name": _clean_text(row.get(mapping.get(name_key, ""), "")),
        "sf_migration_contact_id": _sf_or_none(row.get(mapping.get(sf_key, ""), "")),
        "has_notes": _truthy(row.get(mapping.get(notes_key, ""), "")),
        "has_children": _truthy(row.get(mapping.get(children_key, ""), "")),
    }


def _infer_match_reason(raw: str, left: dict, right: dict) -> str:
    text = raw.lower()
    if "phone" in text or "mobile" in text or "cell" in text:
        return "phone"
    if "email" in text:
        return "email"
    if "name" in text or "exact" in text or not text:
        if left.get("full_name") and right.get("full_name"):
            if _clean_text(left["full_name"]).lower() == _clean_text(right["full_name"]).lower():
                return "name"
        return "name" if not text else text
    return text


def _profile_sort_key(profile: dict) -> tuple:
    """Loom-ish ranking: SF Migration Contact ID first, then lower Birth Mother ID."""
    has_sf = 0 if profile.get("sf_migration_contact_id") else 1
    try:
        bid = int(str(profile.get("birth_mother_id")))
    except (TypeError, ValueError):
        bid = 10**12
    has_children = 0 if profile.get("has_children") else 1
    return (has_sf, bid, has_children)


def _pick_master(profiles: list[dict]) -> dict:
    return sorted(profiles, key=_profile_sort_key)[0]


def rows_to_items(
    rows: list[dict],
    mapping: dict[str, str],
    *,
    approve_name: bool,
    start_index: int = 1,
) -> tuple[list[dict], dict[str, int]]:
    items: list[dict] = []
    stats = {
        "rows": 0,
        "queued": 0,
        "approved_name": 0,
        "phone_manual": 0,
        "skipped_notes": 0,
        "skipped_missing_id": 0,
        "review_needed": 0,
    }

    for row in rows:
        stats["rows"] += 1
        left = _side(row, mapping, "left")
        right = _side(row, mapping, "right")
        if not left.get("birth_mother_id") or not right.get("birth_mother_id"):
            stats["skipped_missing_id"] += 1
            continue
        if left["birth_mother_id"] == right["birth_mother_id"]:
            stats["skipped_missing_id"] += 1
            continue

        match_raw = _clean_text(row.get(mapping.get("match_reason", ""), ""))
        match_reason = _infer_match_reason(match_raw, left, right)
        choice = choose_master(left, right)

        pair_id = f"pair-{start_index + len(items):03d}"
        item: dict[str, Any] = {
            "id": pair_id,
            "match_reason": match_reason,
            "left": left,
            "right": right,
            "skip_if": [],
            "comments": "",
        }

        if choice.get("skip_reason"):
            item["decision"] = "review_needed"
            item["skip_reason"] = choice["skip_reason"]
            item["comments"] = choice["skip_reason"]
            stats["skipped_notes"] += 1
        elif match_reason == "phone":
            item["decision"] = "review_needed"
            item["master"] = choice["master"]
            item["duplicate"] = choice["duplicate"]
            item["master_reason"] = choice["reason"]
            item["comments"] = "phone match — manual in v1"
            stats["phone_manual"] += 1
        elif match_reason == "name" and approve_name:
            item["decision"] = "approved"
            item["master"] = choice["master"]
            item["duplicate"] = choice["duplicate"]
            item["master_reason"] = choice["reason"]
            stats["approved_name"] += 1
        else:
            item["decision"] = "review_needed"
            item["master"] = choice["master"]
            item["duplicate"] = choice["duplicate"]
            item["master_reason"] = choice["reason"]
            item["comments"] = "set decision to approved after review"
            stats["review_needed"] += 1

        items.append(item)
        stats["queued"] += 1

    return items, stats


def _flat_profile(row: dict, mapping: dict[str, str]) -> dict[str, Any] | None:
    bid = _clean_text(row.get(mapping.get("id", ""), ""))
    if not bid:
        return None
    name = _clean_text(row.get(mapping.get("name", ""), ""))
    case_count = _clean_text(row.get(mapping.get("case_count", ""), ""))
    child_dob = _clean_text(row.get(mapping.get("child_dob", ""), ""))
    alerts = _clean_text(row.get(mapping.get("alerts", ""), ""))
    has_children = False
    if case_count.isdigit() and int(case_count) > 0:
        has_children = True
    if child_dob:
        has_children = True
    # Alerts mentioning notes are rare; keep False unless clearly notes-related
    has_notes = "note" in alerts.lower()
    return {
        "birth_mother_id": bid,
        "full_name": name,
        "sf_migration_contact_id": _sf_or_none(row.get(mapping.get("sf", ""), "")),
        "has_notes": has_notes,
        "has_children": has_children,
        "alerts": alerts or None,
    }


def flat_rows_to_items(
    rows: list[dict],
    mapping: dict[str, str],
    *,
    approve_name: bool,
    approve_multi: bool,
    start_index: int = 1,
) -> tuple[list[dict], dict[str, int]]:
    """
    Flat SAM export: one birth-mother profile per row.
    Pair profiles that share the same normalized Full Name.
    """
    stats = {
        "rows": 0,
        "unique_profiles": 0,
        "name_groups_2": 0,
        "name_groups_3plus": 0,
        "queued": 0,
        "approved_name": 0,
        "review_needed": 0,
        "skipped_notes": 0,
        "skipped_missing_id": 0,
        "singleton_names": 0,
    }

    by_id: dict[str, dict] = {}
    for row in rows:
        stats["rows"] += 1
        profile = _flat_profile(row, mapping)
        if not profile:
            stats["skipped_missing_id"] += 1
            continue
        # Prefer first occurrence (report can repeat an ID across case rows)
        by_id.setdefault(profile["birth_mother_id"], profile)

    stats["unique_profiles"] = len(by_id)

    by_name: dict[str, list[dict]] = defaultdict(list)
    for profile in by_id.values():
        key = _clean_text(profile.get("full_name")).lower()
        if not key:
            continue
        by_name[key].append(profile)

    items: list[dict] = []
    for name_key, profiles in sorted(by_name.items(), key=lambda kv: kv[0]):
        if len(profiles) == 1:
            stats["singleton_names"] += 1
            continue

        profiles = sorted(profiles, key=_profile_sort_key)
        master = _pick_master(profiles)
        duplicates = [p for p in profiles if p["birth_mother_id"] != master["birth_mother_id"]]
        multi = len(profiles) >= 3
        if multi:
            stats["name_groups_3plus"] += 1
        else:
            stats["name_groups_2"] += 1

        # Notes conflict across >1 profiles with notes
        noted = [p for p in profiles if p.get("has_notes")]
        if len(noted) >= 2:
            for dup in duplicates:
                pair_id = f"pair-{start_index + len(items):03d}"
                items.append(
                    {
                        "id": pair_id,
                        "decision": "review_needed",
                        "match_reason": "name",
                        "master": master,
                        "duplicate": dup,
                        "master_reason": "sf_then_lower_id",
                        "skip_reason": "both_profiles_have_notes",
                        "comments": f"exact name group '{name_key}' has multiple notes flags",
                        "skip_if": [],
                    }
                )
                stats["queued"] += 1
                stats["skipped_notes"] += 1
            continue

        for dup in duplicates:
            pair_id = f"pair-{start_index + len(items):03d}"
            approve = approve_name and (approve_multi or not multi)
            item: dict[str, Any] = {
                "id": pair_id,
                "match_reason": "name",
                "master": master,
                "duplicate": dup,
                "master_reason": "sf_then_lower_id",
                "group_size": len(profiles),
                "skip_if": [],
                "comments": (
                    f"exact name group size {len(profiles)}"
                    + ("; multi-match held for review" if multi and not approve_multi else "")
                ),
            }
            if approve:
                item["decision"] = "approved"
                stats["approved_name"] += 1
            else:
                item["decision"] = "review_needed"
                stats["review_needed"] += 1
            items.append(item)
            stats["queued"] += 1

    return items, stats


def build_queue(
    csv_path: Path,
    *,
    approve_name: bool,
    approve_multi: bool = False,
    start_index: int = 1,
) -> tuple[dict[str, Any], dict[str, int], dict[str, str], str]:
    fieldnames, rows = read_csv_rows(csv_path)
    paired_mapping = _map_headers(fieldnames, _ALIASES)
    flat_mapping = _map_headers(fieldnames, _FLAT_ALIASES)

    mode: str
    if "left_id" in paired_mapping and "right_id" in paired_mapping:
        mode = "paired"
        items, stats = rows_to_items(
            rows, paired_mapping, approve_name=approve_name, start_index=start_index
        )
        mapping = paired_mapping
    elif "id" in flat_mapping and "name" in flat_mapping:
        mode = "flat_name"
        items, stats = flat_rows_to_items(
            rows,
            flat_mapping,
            approve_name=approve_name,
            approve_multi=approve_multi,
            start_index=start_index,
        )
        mapping = flat_mapping
    else:
        raise SystemExit(
            "CSV not recognized as paired left/right rows or a flat SAM profile export.\n"
            f"Found headers: {', '.join(fieldnames)}\n"
            "Need either left/right Birth Mother IDs, or Birth Mother ID + Full Name columns."
        )

    queue = {
        "crm_system": "sam",
        "policy_version": "loom-v1",
        "source_csv": str(csv_path),
        "pair_mode": mode,
        "notes": (
            "Generated by sam_queue_from_csv.py. "
            "Flat mode pairs exact Full Name matches within the report. "
            "Master = SF Migration Contact ID if present, else lower Birth Mother ID. "
            "Size-2 name groups approved with --approve-name; size 3+ stay review_needed "
            "unless --approve-multi. Phone-only matching is not inferred in flat mode."
        ),
        "items": items,
    }
    return queue, stats, mapping, mode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build SAM merge queue JSON from a report CSV")
    parser.add_argument("csv", help="Path to Duplicate Records CSV export")
    parser.add_argument(
        "-o",
        "--output",
        default="outputs/sam_merge_queue.json",
        help="Output queue JSON path",
    )
    parser.add_argument(
        "--approve-name",
        action="store_true",
        help="Mark size-2 exact-name pairs as decision=approved (ready for --live)",
    )
    parser.add_argument(
        "--approve-multi",
        action="store_true",
        help="Also approve name groups with 3+ profiles (merges each dup into chosen master)",
    )
    parser.add_argument(
        "--start-index",
        type=int,
        default=1,
        help="Starting pair number (pair-001, pair-002, ...)",
    )
    parser.add_argument(
        "--approved-only",
        action="store_true",
        help="Write only decision=approved items to the output queue",
    )
    args = parser.parse_args(argv)

    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"CSV not found: {csv_path}", file=sys.stderr)
        return 2

    queue, stats, mapping, mode = build_queue(
        csv_path,
        approve_name=args.approve_name,
        approve_multi=args.approve_multi,
        start_index=args.start_index,
    )
    if args.approved_only:
        queue["items"] = [i for i in queue["items"] if i.get("decision") == "approved"]

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(queue, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"Mode: {mode}")
    print(f"Wrote {out} with {len(queue['items'])} item(s)")
    print(f"Column mapping used: {mapping}")
    print("Stats:", json.dumps(stats, sort_keys=True))
    if args.approve_name:
        print(
            f"\nNext: python sam_rpa_local.py --queue {out} "
            "--cdp http://127.0.0.1:9222 --live --confirm-live --limit 3"
        )
    else:
        print(
            "\nName matches are still review_needed. "
            "Re-run with --approve-name after spot-checking, or edit decisions in the JSON."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
