#!/usr/bin/env python3
"""
Local SAM RPA runner — attaches to YOUR already-logged-in Chrome session.

Usage:
  python sam_rpa_local.py --inspect
  python sam_rpa_local.py --queue outputs/sam_merge_queue.json --dry-run
  python sam_rpa_local.py --queue outputs/sam_merge_queue.json --live --confirm-live --limit 1

See docs/SAM_LOCAL_RPA_GUIDE.md for the full walkthrough.
"""

from __future__ import annotations

import argparse
import json
import re
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


def inspect_pages(browser, output_dir: Path) -> Path:
    """Dump visible controls from every open tab so we can tune selectors."""
    contexts = browser.contexts
    if not contexts:
        raise RuntimeError("No browser contexts found")

    report: dict[str, Any] = {"ts": utc_now(), "pages": []}
    interesting = re.compile(
        r"advanced|merge|duplicate|birth|mother|save|yes|option|alert|record",
        re.I,
    )

    for context in contexts:
        for idx, page in enumerate(context.pages):
            page_info: dict[str, Any] = {
                "index": idx,
                "url": page.url,
                "title": page.title(),
                "buttons": [],
                "links": [],
                "inputs": [],
                "interesting_matches": [],
            }
            try:
                buttons = page.get_by_role("button").all()
                for b in buttons[:80]:
                    try:
                        text = (b.inner_text(timeout=1000) or "").strip()
                        if text:
                            page_info["buttons"].append(text[:120])
                    except Exception:  # noqa: BLE001
                        continue
                links = page.get_by_role("link").all()
                for a in links[:120]:
                    try:
                        text = (a.inner_text(timeout=1000) or "").strip()
                        if text:
                            page_info["links"].append(text[:120])
                    except Exception:  # noqa: BLE001
                        continue
                inputs = page.locator("input, select, textarea").all()
                for el in inputs[:80]:
                    try:
                        meta = {
                            "tag": el.evaluate("e => e.tagName"),
                            "type": el.get_attribute("type"),
                            "name": el.get_attribute("name"),
                            "id": el.get_attribute("id"),
                            "placeholder": el.get_attribute("placeholder"),
                            "aria_label": el.get_attribute("aria-label"),
                        }
                        page_info["inputs"].append(meta)
                    except Exception:  # noqa: BLE001
                        continue

                blob = " | ".join(
                    page_info["buttons"] + page_info["links"]
                    + [str(x) for x in page_info["inputs"]]
                )
                page_info["interesting_matches"] = sorted(
                    {m.group(0) for m in interesting.finditer(blob)}
                )
            except Exception as exc:  # noqa: BLE001
                page_info["error"] = str(exc)
            report["pages"].append(page_info)

    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = output_dir / f"inspect_{stamp}.json"
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"Inspected {len(report['pages'])} page(s)/tab(s)")
    for p in report["pages"]:
        print(f"\n=== Tab {p['index']}: {p.get('title')} ===")
        print(f"URL: {p.get('url')}")
        print(f"Buttons ({len(p.get('buttons', []))}):")
        for t in p.get("buttons", [])[:30]:
            print(f"  - {t}")
        print(f"Links ({len(p.get('links', []))}):")
        for t in p.get("links", [])[:40]:
            print(f"  - {t}")
        if p.get("interesting_matches"):
            print(f"Interesting keywords found: {', '.join(p['interesting_matches'])}")
    print(f"\nWrote full inspect dump → {out_path}")
    return out_path


def _maybe_click(page, spec: dict, *, dry_run: bool, log: list[str]) -> bool:
    """Best-effort click using role/label/text selectors from the playbook."""
    label = spec.get("name") or spec.get("label") or spec.get("text") or str(spec)
    if dry_run:
        log.append(f"DRY-RUN would click: {label}")
        return True
    try:
        if "role" in spec and "name" in spec:
            page.get_by_role(spec["role"], name=re.compile(re.escape(spec["name"]), re.I)).first.click(
                timeout=8000
            )
        elif "label" in spec:
            page.get_by_label(re.compile(re.escape(spec["label"]), re.I)).first.click(timeout=8000)
        elif "text" in spec:
            page.get_by_text(re.compile(re.escape(spec["text"]), re.I)).first.click(timeout=8000)
        else:
            log.append(f"No click strategy for selector spec: {spec}")
            return False
        log.append(f"Clicked: {label}")
        return True
    except Exception as exc:  # noqa: BLE001
        # Fallback: plain visible text
        try:
            page.get_by_text(label, exact=False).first.click(timeout=5000)
            log.append(f"Clicked via text fallback: {label}")
            return True
        except Exception:  # noqa: BLE001
            log.append(f"Click failed for '{label}': {exc}")
            return False


def _maybe_fill(page, spec: dict, value: str, *, dry_run: bool, log: list[str]) -> bool:
    label = spec.get("label") or spec.get("name") or "input"
    if dry_run:
        log.append(f"DRY-RUN would paste Birth Mother ID '{value}' into: {label}")
        return True
    try:
        if "label" in spec:
            page.get_by_label(re.compile(re.escape(spec["label"]), re.I)).first.fill(
                value, timeout=8000
            )
        elif "role" in spec and "name" in spec:
            page.get_by_role(spec["role"], name=re.compile(re.escape(spec["name"]), re.I)).first.fill(
                value, timeout=8000
            )
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
    log.append(f"Current page URL: {page.url}")
    for step in MERGE_STEPS:
        log.append(f"PLAN: {step}")

    # IMPORTANT: Advanced Options lives on the birth-mother *profile* page,
    # not on the duplicate report grid. For now we require you to open the
    # duplicate profile tab first, then we attempt the merge controls.
    if "Rpt.aspx" in (page.url or ""):
        log.append(
            "NOTE: You are on the report grid page. Open the duplicate profile "
            f"(Birth Mother ID {dup_id}) first, then re-run. Use --inspect on that tab."
        )
        if not dry_run:
            return {
                "id": item.get("id"),
                "status": "need_profile_page",
                "master_birth_mother_id": master_id,
                "duplicate_birth_mother_id": dup_id,
                "log": log,
                "ok": False,
            }

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
    parser.add_argument("--queue", help="Path to approved merge queue JSON")
    parser.add_argument(
        "--cdp",
        default="http://127.0.0.1:9222",
        help="Chrome DevTools endpoint (default http://127.0.0.1:9222)",
    )
    parser.add_argument(
        "--inspect",
        action="store_true",
        help="List buttons/links/inputs on all open SAM tabs (for selector tuning)",
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

    pw = browser = None
    try:
        print(f"Connecting to Chrome via CDP: {args.cdp}")
        pw, browser = connect_browser(args.cdp)

        if args.inspect:
            inspect_pages(browser, Path(args.output_dir))
            return 0

        if not args.queue:
            print("Provide --queue ... or use --inspect", file=sys.stderr)
            return 2
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

        contexts = browser.contexts
        if not contexts:
            print("No browser contexts found. Is Chrome running with --remote-debugging-port=9222?")
            return 1
        context = contexts[0]
        pages = context.pages
        # Prefer a non-report page if one is open (profile/merge tab)
        page = pages[0] if pages else context.new_page()
        for candidate in pages:
            if "Rpt.aspx" not in (candidate.url or ""):
                page = candidate
                break
        print(f"Attached. Active page: {page.url}")
        if len(pages) > 1:
            print(f"Note: {len(pages)} tabs open. Using: {page.url}")

        results: list[dict] = []
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

    except Exception as exc:  # noqa: BLE001
        print(f"Runner error: {exc}", file=sys.stderr)
        return 1
    finally:
        if pw is not None:
            # Do NOT browser.close() — that would close the user's Chrome.
            pw.stop()


if __name__ == "__main__":
    raise SystemExit(main())
