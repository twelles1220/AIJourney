#!/usr/bin/env python3
"""
Local SAM RPA runner — attaches to YOUR already-logged-in Chrome session.

Usage:
  # Terminal A: start Chrome with --remote-debugging-port=9222 and log into SAM
  # Terminal B:
  python3 sam_rpa_local.py --queue outputs/sam_merge_queue.json --dry-run
  python3 sam_rpa_local.py --queue outputs/sam_merge_queue.json --live --confirm-live --limit 1

See docs/SAM_LOCAL_RPA_GUIDE.md for the full walkthrough.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agents.sam_playbook import MERGE_STEPS, SELECTORS, choose_master, should_skip_item


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_queue(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "items" in data:
        return list(data["items"])
    raise ValueError("Queue JSON must be a list or an object with an 'items' array")


def normalize_item(item: dict) -> dict:
    """Ensure master/duplicate exist, applying Loom chooser when only left/right given."""
    item = dict(item)
    if item.get("master") and item.get("duplicate"):
        return item
    if item.get("left") and item.get("right"):
        choice = choose_master(item["left"], item["right"])
        if choice.get("skip_reason"):
            item["_skip_reason"] = choice["skip_reason"]
            return item
        item["master"] = choice["master"]
        item["duplicate"] = choice["duplicate"]
        item["master_reason"] = choice["reason"]
        return item
    return item


def connect_browser(cdp_url: str):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise SystemExit(
            "Playwright is required for local SAM RPA.\n"
            "  pip install playwright && playwright install chromium"
        ) from exc

    pw = sync_playwright().start()
    browser = pw.chromium.connect_over_cdp(cdp_url)
    return pw, browser


def _maybe_click(page, spec: dict, *, dry_run: bool, log: list[str]) -> bool:
    """Best-effort click using role/label selectors from the playbook."""
    label = spec.get("name") or spec.get("label") or str(spec)
    if dry_run:
        log.append(f"DRY-RUN would click: {label}")
        return True
    try:
        if "role" in spec and "name" in spec:
            page.get_by_role(spec["role"], name=spec["name"]).first.click(timeout=8000)
        elif "label" in spec:
            page.get_by_label(spec["label"]).first.click(timeout=8000)
        else:
            log.append(f"No click strategy for selector spec: {spec}")
            return False
        log.append(f"Clicked: {label}")
        return True
    except Exception as exc:  # noqa: BLE001
        log.append(f"Click failed for '{label}': {exc}")
        return False


def _maybe_fill(page, spec: dict, value: str, *, dry_run: bool, log: list[str]) -> bool:
    label = spec.get("label") or spec.get("name") or "input"
    if dry_run:
        log.append(f"DRY-RUN would paste Birth Mother ID '{value}' into: {label}")
        return True
    try:
        if "label" in spec:
            page.get_by_label(spec["label"]).first.fill(value, timeout=8000)
        elif "role" in spec and "name" in spec:
            page.get_by_role(spec["role"], name=spec["name"]).first.fill(value, timeout=8000)
        else:
            log.append(f"No fill strategy for selector spec: {spec}")
            return False
        log.append(f"Filled '{label}' with {value}")
        return True
    except Exception as exc:  # noqa: BLE001
        log.append(f"Fill failed for '{label}': {exc}")
        return False


def run_one_merge(page, item: dict, *, dry_run: bool) -> dict[str, Any]:
    log: list[str] = []
    master = item.get("master") or {}
    duplicate = item.get("duplicate") or {}
    master_id = str(master.get("birth_mother_id") or "")
    dup_id = str(duplicate.get("birth_mother_id") or "")

    log.append(f"Item {item.get('id')}: master={master_id} duplicate={dup_id}")
    log.append(f"Master reason: {item.get('master_reason') or 'provided_in_queue'}")
    for step in MERGE_STEPS:
        log.append(f"PLAN: {step}")

    # Attempt the critical interactive bits. Navigation to specific records is
    # environment-specific; for v1 we operate from the page you already opened
    # and focus on the Advanced Options → Merge Birth Mother → paste → save path.
    ok = True
    ok = _maybe_click(page, SELECTORS["advanced_options"], dry_run=dry_run, log=log) and ok
    ok = _maybe_click(page, SELECTORS["merge_birth_mother"], dry_run=dry_run, log=log) and ok
    ok = _maybe_fill(page, SELECTORS["master_id_input"], master_id, dry_run=dry_run, log=log) and ok

    if dry_run:
        log.append("DRY-RUN stop point: would Save + confirm merge next")
        status = "dry_run_ok" if ok else "dry_run_selector_issues"
    else:
        ok = _maybe_click(page, SELECTORS["save_button"], dry_run=False, log=log) and ok
        ok = _maybe_click(page, SELECTORS["confirm_merge_button"], dry_run=False, log=log) and ok
        log.append("Waiting briefly for SAM (known to be slow)...")
        page.wait_for_timeout(5000)
        status = "merged_attempted" if ok else "failed_selectors"

    return {
        "id": item.get("id"),
        "status": status,
        "master_birth_mother_id": master_id,
        "duplicate_birth_mother_id": dup_id,
        "log": log,
        "ok": ok if dry_run else status == "merged_attempted",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local SAM RPA runner (attach to logged-in Chrome)")
    parser.add_argument("--queue", required=True, help="Path to approved merge queue JSON")
    parser.add_argument(
        "--cdp",
        default="http://127.0.0.1:9222",
        help="Chrome DevTools endpoint (default http://127.0.0.1:9222)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Plan/click-prep only; never confirm merge")
    parser.add_argument("--live", action="store_true", help="Attempt real merges (requires --confirm-live)")
    parser.add_argument(
        "--confirm-live",
        action="store_true",
        help="Safety latch required for --live",
    )
    parser.add_argument("--limit", type=int, default=0, help="Max items to process (0 = all)")
    parser.add_argument(
        "--output-dir",
        default="outputs/sam_rpa",
        help="Where to write the run audit log",
    )
    args = parser.parse_args(argv)

    if args.live and args.dry_run:
        print("Choose one of --dry-run or --live, not both.", file=sys.stderr)
        return 2
    if not args.live and not args.dry_run:
        print("Specify --dry-run (recommended first) or --live --confirm-live.", file=sys.stderr)
        return 2
    if args.live and not args.confirm_live:
        print("Refusing --live without --confirm-live.", file=sys.stderr)
        return 2

    queue_path = Path(args.queue)
    items = [normalize_item(i) for i in load_queue(queue_path)]
    if args.limit and args.limit > 0:
        items = items[: args.limit]

    print(f"Loaded {len(items)} queue item(s) from {queue_path}")
    print(f"Mode: {'DRY-RUN' if args.dry_run else 'LIVE'}")
    print(f"Connecting to Chrome via CDP: {args.cdp}")

    pw = browser = None
    results: list[dict] = []
    try:
        pw, browser = connect_browser(args.cdp)
        contexts = browser.contexts
        if not contexts:
            print("No browser contexts found. Is Chrome running with --remote-debugging-port=9222?")
            return 1
        context = contexts[0]
        pages = context.pages
        page = pages[0] if pages else context.new_page()
        print(f"Attached. Active page: {page.url}")

        for item in items:
            skip = item.get("_skip_reason") or should_skip_item(item)
            if skip:
                entry = {
                    "id": item.get("id"),
                    "status": "skipped",
                    "skip_reason": skip,
                    "ok": True,
                    "log": [f"Skipped: {skip}"],
                }
                results.append(entry)
                print(f"SKIP {item.get('id')}: {skip}")
                continue

            print(f"Processing {item.get('id')} ...")
            entry = run_one_merge(page, item, dry_run=args.dry_run)
            results.append(entry)
            print(f"  → {entry['status']}")
            for line in entry["log"]:
                print(f"     {line}")

    except Exception as exc:  # noqa: BLE001
        print(f"Runner error: {exc}", file=sys.stderr)
        return 1
    finally:
        if pw is not None:
            # Do NOT browser.close() — that would close the user's Chrome.
            pw.stop()

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = out_dir / f"run_{stamp}.json"
    report = {
        "ts": utc_now(),
        "mode": "dry_run" if args.dry_run else "live",
        "queue": str(queue_path),
        "cdp": args.cdp,
        "result_count": len(results),
        "results": results,
    }
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nWrote audit log → {out_path}")

    failures = [r for r in results if not r.get("ok") and r.get("status") != "skipped"]
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
