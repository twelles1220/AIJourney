#!/usr/bin/env python3
"""
Build a SAM merge queue JSON from a Duplicate Records report CSV export.

Usage:
  python sam_queue_from_csv.py path\\to\\duplicates.csv -o outputs\\sam_merge_queue.json
  python sam_queue_from_csv.py path\\to\\duplicates.csv -o outputs\\sam_merge_queue.json --approve-name

Then run merges in batches:
  python sam_rpa_local.py --queue outputs\\sam_merge_queue.json --live --confirm-live --limit 3
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path
from typing import Any

from agents.sam_playbook import choose_master


# Flexible header aliases from SAM / Excel exports
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


def _norm_header(h: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (h or "").strip().lower()).strip("_")


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


def _map_headers(fieldnames: list[str] | None) -> dict[str, str]:
    if not fieldnames:
        return {}
    by_norm = {_norm_header(h): h for h in fieldnames}
    mapping: dict[str, str] = {}
    for key, aliases in _ALIASES.items():
        for alias in aliases:
            if alias in by_norm:
                mapping[key] = by_norm[alias]
                break
    return mapping


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
        # Default unknown/blank match type to name when names match closely
        if left.get("full_name") and right.get("full_name"):
            if _clean_text(left["full_name"]).lower() == _clean_text(right["full_name"]).lower():
                return "name"
        return "name" if not text else text
    return text


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


def build_queue(
    csv_path: Path,
    *,
    approve_name: bool,
    start_index: int = 1,
) -> tuple[dict[str, Any], dict[str, int], dict[str, str]]:
    with csv_path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        fieldnames = list(reader.fieldnames or [])
        mapping = _map_headers(fieldnames)
        rows = list(reader)

    required = {"left_id", "right_id"}
    missing = sorted(required - set(mapping))
    if missing:
        raise SystemExit(
            "CSV is missing required columns for: "
            + ", ".join(missing)
            + "\nFound headers: "
            + ", ".join(fieldnames)
            + "\nNeeded (any alias): left/right birth mother IDs. "
            "Paste the header row here if you want aliases added."
        )

    items, stats = rows_to_items(
        rows, mapping, approve_name=approve_name, start_index=start_index
    )
    queue = {
        "crm_system": "sam",
        "policy_version": "loom-v1",
        "source_csv": str(csv_path),
        "notes": (
            "Generated by sam_queue_from_csv.py. "
            "Name matches are approved only when --approve-name was used. "
            "Phone matches stay review_needed (manual in v1)."
        ),
        "items": items,
    }
    return queue, stats, mapping


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
        help="Mark name-match pairs as decision=approved (ready for --live)",
    )
    parser.add_argument(
        "--start-index",
        type=int,
        default=1,
        help="Starting pair number (pair-001, pair-002, ...)",
    )
    args = parser.parse_args(argv)

    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"CSV not found: {csv_path}", file=sys.stderr)
        return 2

    queue, stats, mapping = build_queue(
        csv_path, approve_name=args.approve_name, start_index=args.start_index
    )
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(queue, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"Wrote {out} with {stats['queued']} item(s)")
    print(f"Column mapping used: {mapping}")
    print(
        "Stats: "
        f"rows={stats['rows']} approved_name={stats['approved_name']} "
        f"phone_manual={stats['phone_manual']} review_needed={stats['review_needed']} "
        f"skipped_notes={stats['skipped_notes']} skipped_missing_id={stats['skipped_missing_id']}"
    )
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
