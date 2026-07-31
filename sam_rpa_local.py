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
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agents.sam_playbook import (
    MERGE_IFRAME_PATH_TEMPLATE,
    MERGE_STEPS,
    PROFILE_PATH_TEMPLATE,
    SELECTORS,
    choose_master,
    should_skip_item,
)


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
                        href = a.get_attribute("href") or ""
                        if text:
                            page_info["links"].append(text[:120])
                            if interesting.search(text) or interesting.search(href):
                                page_info.setdefault("interesting_links", []).append(
                                    {"text": text[:120], "href": href[:300]}
                                )
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
        if p.get("interesting_links"):
            print("Interesting links with hrefs:")
            for item in p["interesting_links"][:30]:
                print(f"  - {item['text']} → {item['href']}")
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

    locator = None
    try:
        if "role" in spec and "name" in spec:
            locator = page.get_by_role(
                spec["role"], name=re.compile(re.escape(spec["name"]), re.I)
            ).first
        elif "label" in spec:
            locator = page.get_by_label(re.compile(re.escape(spec["label"]), re.I)).first
        elif "text" in spec:
            locator = page.get_by_text(re.compile(re.escape(spec["text"]), re.I)).first
        else:
            log.append(f"No click strategy for selector spec: {spec}")
            return False

            locator.wait_for(state="attached", timeout=4000)
            try:
                locator.scroll_into_view_if_needed(timeout=2000)
            except Exception:  # noqa: BLE001
                pass

            # Normal click first
            try:
                locator.click(timeout=3000)
                log.append(f"Clicked: {label}")
                return True
            except Exception as first_exc:  # noqa: BLE001
                log.append(f"Normal click blocked for '{label}' ({first_exc.__class__.__name__}); trying force")

            try:
                locator.click(timeout=3000, force=True)
                log.append(f"Clicked (force): {label}")
                return True
            except Exception as force_exc:  # noqa: BLE001
                log.append(f"Force click failed for '{label}': {force_exc.__class__.__name__}")

            try:
                locator.dispatch_event("click")
                log.append(f"Clicked (dispatch): {label}")
                return True
            except Exception as disp_exc:  # noqa: BLE001
                log.append(f"Click failed for '{label}': {disp_exc}")
                return False
    except Exception as exc:  # noqa: BLE001
        # Last-chance plain text
        try:
            page.get_by_text(label, exact=False).first.click(timeout=5000, force=True)
            log.append(f"Clicked via text fallback: {label}")
            return True
        except Exception:  # noqa: BLE001
            log.append(f"Click failed for '{label}': {exc}")
            return False


def _maybe_fill_fallback(page, spec: dict, value: str, *, log: list[str]) -> bool:
    """Try alternate ways to find the merge ID field when label lookup fails."""
    root = _modal_root(page)
    try:
        if "placeholder" in spec:
            loc = root.get_by_placeholder(re.compile(spec["placeholder"], re.I)).first
            loc.fill(value, timeout=4000)
            log.append(f"Filled via placeholder '{spec['placeholder']}' with {value}")
            return True
        if "name_contains" in spec:
            loc = root.locator(
                f"input[name*='{spec['name_contains']}'], input[id*='{spec['name_contains']}']"
            ).first
            loc.fill(value, timeout=4000)
            log.append(f"Filled via name/id containing '{spec['name_contains']}' with {value}")
            return True
        if "label" in spec:
            try:
                root.get_by_label(re.compile(re.escape(spec["label"]), re.I)).first.fill(
                    value, timeout=4000
                )
                log.append(f"Filled via label '{spec['label']}' with {value}")
                return True
            except Exception:  # noqa: BLE001
                root.get_by_text(re.compile(re.escape(spec["label"]), re.I)).locator(
                    "xpath=following::input[1]"
                ).fill(value, timeout=4000)
                log.append(f"Filled via nearby label text '{spec['label']}' with {value}")
                return True
    except Exception as exc:  # noqa: BLE001
        log.append(f"Fallback fill missed ({spec}): {exc.__class__.__name__}")
    return False


def find_merge_frame(page, *, timeout_ms: int = 10000):
    """
    The Merge Entities UI is a modal and may live in an iframe.
    Poll the main page + all frames for 'Merge To' / 'Merge Entities'.
    Returns (frame_or_page, log_note).
    """
    import time

    deadline = time.time() + (timeout_ms / 1000)
    last_note = "no merge frame yet"
    while time.time() < deadline:
        candidates = [page, *page.frames]
        for frame in candidates:
            try:
                if frame.get_by_text(re.compile(r"Merge Entities", re.I)).count() > 0:
                    return frame, f"found Merge Entities in frame url={getattr(frame, 'url', page.url)}"
                if frame.get_by_text(re.compile(r"Merge To", re.I)).count() > 0:
                    return frame, f"found Merge To in frame url={getattr(frame, 'url', page.url)}"
            except Exception:  # noqa: BLE001
                continue
        try:
            for sel in ("iframe", "iframe[src*='Merge']", "iframe[src*='merge']", ".modal iframe"):
                for fr in page.locator(sel).all():
                    try:
                        content = fr.content_frame()
                        if not content:
                            continue
                        if content.get_by_text(re.compile(r"Merge (Entities|To)", re.I)).count() > 0:
                            return content, f"found merge UI via iframe selector {sel}"
                    except Exception:  # noqa: BLE001
                        continue
        except Exception:  # noqa: BLE001
            pass
        page.wait_for_timeout(500)
        last_note = f"still waiting; frames={len(page.frames)}"
    return None, last_note


def _modal_root(page):
    """Prefer the Merge Entities modal/dialog container if present on this page/frame."""
    candidates = [
        page.get_by_role("dialog"),
        page.locator(".modal.show, .modal.in, [role='dialog'], .ui-dialog, .modal"),
        page.get_by_text(re.compile(r"Merge Entities", re.I)).locator(
            "xpath=ancestor-or-self::*[contains(@class,'modal') or @role='dialog' or contains(@class,'ui-dialog')][1]"
        ),
    ]
    for loc in candidates:
        try:
            if loc.count() > 0:
                first = loc.first
                if first.is_visible():
                    return first
        except Exception:  # noqa: BLE001
            continue
    return page


def _maybe_fill(page, spec: dict, value: str, *, dry_run: bool, log: list[str]) -> bool:
    label = spec.get("label") or spec.get("name") or "input"
    if dry_run:
        log.append(f"DRY-RUN would paste master ID '{value}' into: {label}")
        return True

    root = _modal_root(page)
    try:
        # Fast path from successful live run
        if spec.get("input_id"):
            try:
                root.locator(f"#{spec['input_id']}, input[name='{spec['input_id']}']").first.fill(
                    value, timeout=3000
                )
                log.append(f"Filled #{spec['input_id']} with {value}")
                return True
            except Exception:  # noqa: BLE001
                pass
        if "label" in spec:
            try:
                root.get_by_label(re.compile(re.escape(spec["label"]), re.I)).first.fill(
                    value, timeout=5000
                )
                log.append(f"Filled via label '{label}' with {value}")
                return True
            except Exception:  # noqa: BLE001
                root.get_by_text(re.compile(re.escape(spec["label"]), re.I)).locator(
                    "xpath=following::input[1]"
                ).fill(value, timeout=5000)
                log.append(f"Filled via nearby label text '{label}' with {value}")
                return True
        if "role" in spec and "name" in spec:
            root.get_by_role(
                spec["role"], name=re.compile(re.escape(spec["name"]), re.I)
            ).first.fill(value, timeout=5000)
            log.append(f"Filled '{label}' with {value}")
            return True
        log.append(f"No fill strategy for selector spec: {spec}")
        return False
    except Exception as exc:  # noqa: BLE001
        log.append(f"Fill failed for '{label}': {exc}")
        return False


def dump_visible_controls(page, log: list[str], *, limit: int = 40) -> None:
    """Append a quick snapshot of buttons/links/inputs to the run log."""
    try:
        buttons = []
        for b in page.get_by_role("button").all()[:limit]:
            try:
                t = (b.inner_text(timeout=500) or "").strip()
                if t:
                    buttons.append(t[:80])
            except Exception:  # noqa: BLE001
                continue
        links = []
        for a in page.get_by_role("link").all()[:limit]:
            try:
                t = (a.inner_text(timeout=500) or "").strip()
                if t:
                    links.append(t[:80])
            except Exception:  # noqa: BLE001
                continue
        inputs = []
        for el in page.locator("input, select, textarea").all()[:limit]:
            try:
                inputs.append(
                    {
                        "type": el.get_attribute("type"),
                        "name": el.get_attribute("name"),
                        "id": el.get_attribute("id"),
                        "placeholder": el.get_attribute("placeholder"),
                        "aria_label": el.get_attribute("aria-label"),
                    }
                )
            except Exception:  # noqa: BLE001
                continue
        log.append(f"SNAPSHOT url={page.url}")
        log.append(f"SNAPSHOT buttons={buttons}")
        log.append(f"SNAPSHOT links={links[:25]}")
        log.append(f"SNAPSHOT inputs={inputs}")
    except Exception as exc:  # noqa: BLE001
        log.append(f"SNAPSHOT failed: {exc}")


def find_profile_page(pages, chmid: str):
    """Return an open tab whose URL contains chmid=..., else None."""
    needle = f"chmid={chmid}"
    for page in pages:
        if needle in (page.url or ""):
            return page
    return None


def open_profile(page, chmid: str, *, dry_run: bool, log: list[str]):
    """Navigate current page to the birth-mother cover page for chmid."""
    # Keep same origin; only swap path/query
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(page.url)
    path = PROFILE_PATH_TEMPLATE.format(chmid=chmid)
    target = urlunsplit((parts.scheme, parts.netloc, path, "", ""))
    if dry_run:
        log.append(f"DRY-RUN would open profile: {target}")
        return True
    log.append(f"Opening profile: {target}")
    page.goto(target, wait_until="domcontentloaded")
    page.wait_for_timeout(1500)
    log.append(f"Opened profile URL now: {page.url}")
    return True


# Patch confirm/alert on this window + reachable child frames.
# Used so Save never blocks the Playwright CDP call on a native confirm().
_CONFIRM_PATCH_JS = """
() => {
  const patch = (w) => {
    try {
      w.__samConfirmPatched = true;
      w.confirm = (msg) => { w.__samLastConfirm = String(msg || ''); return true; };
      w.alert = (msg) => { w.__samLastAlert = String(msg || ''); };
      w.prompt = () => null;
    } catch (e) {}
  };
  patch(window);
  try { patch(window.top); } catch (e) {}
  try {
    for (let i = 0; i < window.frames.length; i++) {
      try { patch(window.frames[i]); } catch (e) {}
    }
  } catch (e) {}
  return true;
}
"""

# Patch confirm in the same tick, then schedule Save via setTimeout so evaluate
# returns BEFORE any synchronous confirm() can freeze the CDP session.
_SAVE_SCHEDULE_JS = """
() => {
  const patch = (w) => {
    try {
      w.__samConfirmPatched = true;
      w.confirm = (msg) => { w.__samLastConfirm = String(msg || ''); return true; };
      w.alert = (msg) => { w.__samLastAlert = String(msg || ''); };
      w.prompt = () => null;
    } catch (e) {}
  };
  patch(window);
  try { patch(window.top); } catch (e) {}
  try {
    for (let i = 0; i < window.frames.length; i++) {
      try { patch(window.frames[i]); } catch (e) {}
    }
  } catch (e) {}

  const nodes = Array.from(
    document.querySelectorAll('a, button, input[type=button], input[type=submit]')
  );
  const btn = nodes.find((el) =>
    /\\bsave\\b/i.test((el.innerText || el.value || '').replace(/\\s+/g, ' ').trim())
  );
  if (!btn) return { ok: false, reason: 'no-save' };
  const text = (btn.innerText || btn.value || '').replace(/\\s+/g, ' ').trim().slice(0, 60);
  setTimeout(() => { try { btn.click(); } catch (e) {} }, 0);
  return { ok: true, reason: 'scheduled', text };
}
"""

_YES_SCHEDULE_JS = """
() => {
  const patterns = [/yes,?\\s*merge/i, /^yes$/i, /^ok$/i, /^confirm$/i];
  const nodes = Array.from(
    document.querySelectorAll(
      'a, button, input[type=button], input[type=submit], [role=button]'
    )
  );
  for (const el of nodes) {
    const text = (el.innerText || el.value || el.getAttribute('aria-label') || '')
      .replace(/\\s+/g, ' ')
      .trim();
    if (!text || !patterns.some((p) => p.test(text))) continue;
    try {
      const style = window.getComputedStyle(el);
      if (style && (style.visibility === 'hidden' || style.display === 'none')) continue;
    } catch (e) {}
    setTimeout(() => { try { el.click(); } catch (e) {} }, 0);
    return { ok: true, text: text.slice(0, 80) };
  }
  return { ok: false };
}
"""

_MERGE_UI_OPEN_JS = """
() => {
  const el = document.getElementById('ctrlMergeToEntID');
  if (el) {
    try {
      if (el.offsetParent !== null) return true;
    } catch (e) {
      return true;
    }
  }
  const t = ((document.body && document.body.innerText) || '').replace(/\\s+/g, ' ');
  return /Merge Entities/i.test(t) && /Merge To/i.test(t);
}
"""


def _iter_page_frames(host_page):
    yield host_page
    try:
        yield from host_page.frames
    except Exception:  # noqa: BLE001
        return


def _patch_confirm_everywhere(host_page, merge_ctx, log: list[str]) -> None:
    seen: set[int] = set()
    for ctx in (merge_ctx, *_iter_page_frames(host_page)):
        try:
            key = id(ctx)
            if key in seen:
                continue
            seen.add(key)
            ctx.evaluate(_CONFIRM_PATCH_JS)
            log.append(f"Patched confirm/alert on frame url={getattr(ctx, 'url', '')[:120]}")
        except Exception as exc:  # noqa: BLE001
            log.append(f"Confirm patch skipped ({exc.__class__.__name__})")


def _schedule_save_click(merge_ctx, log: list[str]) -> bool:
    """Click Save without blocking on native confirm(). Never use locator.click() here."""
    try:
        result = merge_ctx.evaluate(_SAVE_SCHEDULE_JS)
    except Exception as exc:  # noqa: BLE001
        log.append(f"Scheduled Save failed: {exc}")
        return False
    if isinstance(result, dict) and result.get("ok"):
        log.append(f"Scheduled Save click ({result.get('text')!r}) after confirm patch")
        return True
    log.append(f"Could not find Save control to schedule: {result!r}")
    return False


def _schedule_yes_clicks(host_page, log: list[str]) -> bool:
    """Click on-page Yes/OK via JS setTimeout (never Playwright locator.click)."""
    clicked = False
    for ctx in _iter_page_frames(host_page):
        try:
            result = ctx.evaluate(_YES_SCHEDULE_JS)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(result, dict) and result.get("ok"):
            log.append(f"Scheduled Yes/confirm click ({result.get('text')!r})")
            clicked = True
    return clicked


def _merge_ui_still_open(host_page) -> bool:
    for ctx in _iter_page_frames(host_page):
        try:
            if ctx.evaluate(_MERGE_UI_OPEN_JS):
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _finish_save_and_confirm(host_page, merge_ctx, log: list[str]) -> bool:
    """
    Save + confirm without hanging PowerShell.

    Native window.confirm() blocks Playwright CDP calls until dismissed. We:
      1) patch confirm() on every reachable frame
      2) schedule Save via setTimeout (evaluate returns immediately)
      3) accept Playwright dialog events on the browser context
      4) schedule on-page Yes clicks via JS (no locator.click)
      5) if UI still open, ask the operator to click Yes and wait for close
    """
    dialogs: list[str] = []

    def _accept_dialog(dialog) -> None:
        msg = (dialog.message or "").replace("\n", " ")[:180]
        dialogs.append(f"{dialog.type}:{msg}")
        try:
            dialog.accept()
        except Exception:  # noqa: BLE001
            pass

    context = getattr(host_page, "context", None)
    if context is not None:
        context.on("dialog", _accept_dialog)
    host_page.on("dialog", _accept_dialog)

    try:
        _patch_confirm_everywhere(host_page, merge_ctx, log)
        save_ok = _schedule_save_click(merge_ctx, log)
        if not save_ok:
            # Last resort: still avoid sync locator.click — try host page too
            save_ok = _schedule_save_click(host_page, log)

        # Let the scheduled Save run; confirm patch / dialog handler should absorb it.
        host_page.wait_for_timeout(400)

        confirmed = False
        try:
            last_confirm = merge_ctx.evaluate("() => window.__samLastConfirm || null")
            if last_confirm:
                log.append(f"window.confirm was auto-accepted: {str(last_confirm)[:180]}")
                confirmed = True
        except Exception:  # noqa: BLE001
            pass

        if dialogs:
            for d in dialogs:
                log.append(f"Accepted JS dialog → {d}")
            confirmed = True

        # On-page Yes/No (HTML), not native confirm
        for attempt in range(10):
            if _schedule_yes_clicks(host_page, log):
                confirmed = True
            if dialogs and not confirmed:
                for d in dialogs:
                    log.append(f"Accepted JS dialog → {d}")
                confirmed = True
            try:
                if not _merge_ui_still_open(host_page):
                    log.append("Merge UI closed after Save/confirm")
                    return save_ok
            except Exception:  # noqa: BLE001
                # If CDP is wedged by a native dialog, fall through to manual wait.
                pass
            # Enter often activates the default Yes on SAM prompts
            try:
                host_page.keyboard.press("Enter")
            except Exception:  # noqa: BLE001
                pass
            host_page.wait_for_timeout(400)

        if confirmed:
            log.append("Confirm signal seen; waiting for merge UI to settle")
        else:
            log.append("No auto-confirm signal yet — checking for manual Yes/No")

        print(
            "\n>>> If Yes/No is still visible in Chrome, click YES now.\n"
            ">>> Waiting up to 90s for the merge window to close...\n",
            flush=True,
        )
        log.append("Manual fallback: waiting for operator Yes or merge UI close (90s)")

        deadline = time.time() + 90
        while time.time() < deadline:
            if dialogs:
                for d in dialogs:
                    log.append(f"Accepted JS dialog → {d}")
                dialogs.clear()
                confirmed = True
            try:
                _schedule_yes_clicks(host_page, log)
            except Exception:  # noqa: BLE001
                pass
            try:
                if not _merge_ui_still_open(host_page):
                    log.append("Merge UI closed (merge likely committed)")
                    return save_ok
            except Exception:  # noqa: BLE001
                # Native dialog can block evaluate; keep waiting for operator click.
                pass
            try:
                host_page.keyboard.press("Enter")
            except Exception:  # noqa: BLE001
                pass
            try:
                host_page.wait_for_timeout(500)
            except Exception:  # noqa: BLE001
                time.sleep(0.5)

        dump_visible_controls(host_page, log)
        log.append(
            "Merge UI still open after timeout — click Yes in Chrome if visible, "
            "then re-run this queue item."
        )
        return False
    finally:
        for target in (host_page, context):
            if target is None:
                continue
            try:
                target.remove_listener("dialog", _accept_dialog)
            except Exception:  # noqa: BLE001
                pass


def run_one_merge(page, item: dict, *, dry_run: bool, all_pages: list | None = None) -> dict[str, Any]:
    log: list[str] = []
    master = item.get("master") or {}
    duplicate = item.get("duplicate") or {}
    master_id = str(master.get("birth_mother_id") or "")
    dup_id = str(duplicate.get("birth_mother_id") or "")

    log.append(f"Item {item.get('id')}: master={master_id} duplicate={dup_id}")
    log.append(f"Master reason: {item.get('master_reason') or 'provided_in_queue'}")
    for step in MERGE_STEPS:
        log.append(f"PLAN: {step}")

    # Prefer an already-open duplicate profile tab; otherwise navigate there.
    active = page
    if all_pages:
        existing = find_profile_page(all_pages, dup_id)
        if existing is not None:
            active = existing
            log.append(f"Using already-open duplicate tab: {active.url}")
        else:
            open_profile(active, dup_id, dry_run=dry_run, log=log)
    elif "chmid=" not in (active.url or ""):
        open_profile(active, dup_id, dry_run=dry_run, log=log)

    log.append(f"Working page URL: {active.url}")

    ok = True
    ok = _maybe_click(active, SELECTORS["advanced_options"], dry_run=dry_run, log=log) and ok
    if not dry_run:
        # ADVANCED OPTIONS is a Bootstrap collapse; wait for menu items
        active.wait_for_timeout(2000)
        try:
            active.get_by_text(re.compile(r"Merge\s+Birth\s+Mother", re.I)).first.wait_for(
                state="visible", timeout=10000
            )
            log.append("Merge Birth Mother became visible after ADVANCED OPTIONS")
        except Exception:  # noqa: BLE001
            # Try clicking ADVANCED OPTIONS again if collapsed
            _maybe_click(active, SELECTORS["advanced_options"], dry_run=False, log=log)
            active.wait_for_timeout(1500)
            try:
                active.get_by_text(re.compile(r"Merge\s+Birth\s+Mother", re.I)).first.wait_for(
                    state="visible", timeout=8000
                )
                log.append("Merge Birth Mother visible after second ADVANCED OPTIONS click")
            except Exception:  # noqa: BLE001
                log.append("Merge Birth Mother not visible yet; will still attempt click")

    # Prefer opening merge iframe the way SAM does:
    # javascript:GoAddEntIframe('/SAM/Cmn/Ent_Merge.aspx?enttpid=28&entid=7172');
    merge_opened = False
    if not dry_run:
        try:
            merge_path = MERGE_IFRAME_PATH_TEMPLATE.format(entid=dup_id)
            log.append(f"Opening merge iframe via GoAddEntIframe({merge_path})")
            active.evaluate(
                "(path) => { if (typeof GoAddEntIframe === 'function') { GoAddEntIframe(path); } }",
                merge_path,
            )
            merge_opened = True
            ok = True
        except Exception as exc:  # noqa: BLE001
            log.append(f"GoAddEntIframe failed ({exc}); falling back to menu click")
            ok = _maybe_click(active, SELECTORS["merge_birth_mother"], dry_run=False, log=log) and ok
            merge_opened = ok
    else:
        log.append(f"DRY-RUN would open merge iframe for entid={dup_id}")
        ok = True
        merge_opened = True

    if not dry_run and merge_opened:
        merge_ctx, note = find_merge_frame(active, timeout_ms=12000)
        log.append(f"Merge frame search: {note}")
        if merge_ctx is not None:
            active = merge_ctx
            log.append("Using merge frame/page for Merge To + Save")
        else:
            log.append(
                "Merge modal/iframe not found after click. "
                "If the modal is visible on screen, tell me — we may need a different trigger."
            )
        dump_visible_controls(active, log)
    filled = _maybe_fill(active, SELECTORS["master_id_input"], master_id, dry_run=dry_run, log=log)
    if not filled and not dry_run:
        for fallback in (
            {"label": "Merge To"},
            {"placeholder": "Merge"},
            {"name_contains": "merge"},
            {"name_contains": "chmid"},
            {"label": "Mother ID"},
        ):
            if _maybe_fill_fallback(active, fallback, master_id, log=log):
                filled = True
                break
        if not filled:
            # Last resort: first visible text input inside modal
            try:
                root = _modal_root(active)
                root.locator("input[type='text'], input:not([type])").first.fill(
                    master_id, timeout=5000
                )
                log.append(f"Filled first modal text input with {master_id}")
                filled = True
            except Exception as exc:  # noqa: BLE001
                log.append(f"First-modal-input fill failed: {exc}")
                dump_visible_controls(active, log)
    ok = filled and ok

    if dry_run:
        log.append("DRY-RUN stop point: would Save + confirm merge next")
        status = "dry_run_ok" if ok else "dry_run_selector_issues"
    else:
        # Dialogs / confirm must not block CDP. Use async Save + JS Yes only.
        host_page = active.page if hasattr(active, "page") else active
        confirmed_ok = _finish_save_and_confirm(host_page, active, log)
        ok = confirmed_ok and ok
        log.append("Waiting for SAM to finish merge...")
        try:
            host_page.wait_for_timeout(1500)
        except Exception:  # noqa: BLE001
            pass
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
        page = pages[0] if pages else context.new_page()
        # Prefer an open profile tab if present
        for candidate in pages:
            if "Ch_M_Vw.aspx" in (candidate.url or ""):
                page = candidate
                break
        print(f"Attached. Active page: {page.url}")
        if len(pages) > 1:
            print(f"Note: {len(pages)} tabs open.")

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
            entry = run_one_merge(page, item, dry_run=args.dry_run, all_pages=pages)
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
