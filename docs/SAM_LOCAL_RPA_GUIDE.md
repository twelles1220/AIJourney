# SAM Local RPA — Step-by-Step Guide

This guide walks you through using the agentic system when **SAM has no usable API and no sandbox**: you log into SAM on your machine, then a **local RPA runner** drives the merge clicks in your already-authenticated browser.

> **Mental model:** Watchdog + Fuzzy-Match decide *what* to merge.  
> Your laptop’s RPA runner does the *clicking* after you’re logged in.

Reference workflow (from your Loom):  
[Merging Duplicate Birth Mother Profiles](https://www.loom.com/share/55df212f59a247799830c617cf804470)

---

## 0. What you need before starting

| Item | Notes |
|------|--------|
| This repo on your laptop | Not in the cloud agent |
| Python 3.10+ | Same as the rest of the project |
| Google Chrome or Chromium | Recommended for CDP attach |
| Your normal SAM login | Including SSO / 2FA — **you** complete that |
| A small approved merge list | Start with 1–3 name-based duplicates |
| 20–30 quiet minutes | Watch the first live batch |

You do **not** need API keys or a SAM sandbox.

---

## 1. Install local tools (one-time)

In a terminal, from the repo root:

```bash
cd /path/to/AIJourney
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
pip install playwright
playwright install chromium
```

Optional but useful: keep a dedicated Chrome profile only for SAM automation (fewer random extensions, clearer session).

---

## 2. Understand the merge rules (from your Loom)

Encode these as policy before any live clicks:

### Master profile (survivor)
1. Prefer the record with an **SF Migration Contact ID**
2. If **both** have SF Migration Contact ID → prefer the **lower Birth Mother ID** (e.g. 803 over 1300)
3. Prefer the profile that has **birth mother children** linked
4. If conflicting / important **notes** exist on both → **do not merge** (skip + review)

### First-pass scope
- Automate **name-based** duplicates first
- Leave **cell-phone-only** matches manual for now

### Click path to automate
1. Start on the **Duplicate Records** report / alert page  
2. Open the duplicate via **Duplicate Records Alert → Duplicate Record**  
3. Go to the **master** (SF Migration Contact ID profile)  
4. **Copy Birth Mother ID**  
5. Return to the duplicate  
6. **Advanced Options → Merge Birth Mother**  
7. Paste master Birth Mother ID → **Save** → confirm **Yes, merge these records**  
8. Wait (SAM can be slow) → refresh → confirm duplicate is gone  

---

## 3. Build an approved merge queue

You can start from either:

### Option A — CSV / JSON you curate by hand (best for first run)

Copy the example:

```bash
cp samples/sam_merge_queue.example.json outputs/sam_merge_queue.json
```

Edit `outputs/sam_merge_queue.json` so each item has:

- `master` — the survivor (Birth Mother ID, name, SF Migration Contact ID if present)
- `duplicate` — the record to merge away
- `decision` — must be `"approved"` or `"auto_merge"` (the runner refuses anything else)
- `match_reason` — e.g. `"name"` (skip `"phone"` in v1)

### Option B — Let Fuzzy-Match propose, you approve

```bash
# After normalizing a donor export (if applicable):
python3 -c "
from agents.fuzzy_match import FuzzyMatchAgent
r = FuzzyMatchAgent().run({
    'csv_path': 'path/to/normalized.csv',
    'output_dir': 'outputs/fuzzy_match',
    'auto_threshold': 98,
    'review_threshold': 80,
})
print(r.summary)
print(r.artifacts)
"
```

Then:
1. Open `outputs/fuzzy_match/auto_merge_queue.json` and `review_needed.csv`
2. Apply the **SAM master rules** above
3. Copy only human-approved pairs into `outputs/sam_merge_queue.json` with `decision: "approved"`

**Do not** feed raw review-band pairs straight into live RPA.

---

## 4. Log into SAM in a debuggable Chrome window

The runner does **not** log in for you. You authenticate first; it attaches to that browser.

### macOS / Linux

Quit Chrome completely, then start it with remote debugging:

```bash
# macOS typical:
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
  --remote-debugging-port=9222 \
  --user-data-dir="$HOME/chrome-sam-rpa-profile"
```

```bash
# Linux typical:
google-chrome \
  --remote-debugging-port=9222 \
  --user-data-dir="$HOME/chrome-sam-rpa-profile"
```

### Windows (PowerShell)

```powershell
& "C:\Program Files\Google\Chrome\Application\chrome.exe" `
  --remote-debugging-port=9222 `
  --user-data-dir="$env:USERPROFILE\chrome-sam-rpa-profile"
```

Then, in that Chrome window:

1. Go to SAM  
2. Complete login / SSO / 2FA  
3. Navigate to the **Duplicate Records** landing page from your Loom  
4. Leave that window open  

Keep this Chrome window for automation only while the runner is active.

---

## 5. Dry-run (no irreversible merges)

With your venv active and Chrome logged into SAM:

```bash
python3 sam_rpa_local.py \
  --queue outputs/sam_merge_queue.json \
  --cdp http://127.0.0.1:9222 \
  --dry-run
```

What dry-run does:
- Connects to your open Chrome session  
- Loads the queue  
- Prints the planned click path per pair  
- Optionally opens/focuses pages if selectors are configured  
- **Does not** click Save / confirm merge  

Read the terminal output. If master/duplicate IDs look wrong, fix the queue and repeat.

---

## 6. First live batch (1–3 records, you watch)

Only after a clean dry-run:

```bash
python3 sam_rpa_local.py \
  --queue outputs/sam_merge_queue.json \
  --cdp http://127.0.0.1:9222 \
  --live \
  --confirm-live \
  --limit 1
```

Recommended first live settings:
- `--limit 1` — one merge only  
- Sit and watch the browser  
- Verify the duplicate disappears after refresh (as in the Loom)  
- If it works, raise `--limit` slowly (3 → 10)

Logs are written under `outputs/sam_rpa/` (actions, successes, skips, failures).

---

## 7. Recommended operating rhythm

```text
1. You log into SAM (Chrome on port 9222)
2. Prepare / approve today's merge queue
3. Dry-run
4. Live with --limit 1
5. Spot-check in SAM
6. Live small batch
7. Stop and review audit log
```

If SAM is slow (as in your Loom), don’t parallelize merges. One at a time is safer.

---

## 8. Safety checklist (print this near your desk)

- [ ] This is an approved queue only (`approved` / `auto_merge`)  
- [ ] Name-based matches only for v1  
- [ ] Master has SF Migration Contact ID when available  
- [ ] Dual-SF cases use lower Birth Mother ID  
- [ ] Notes conflicts were skipped  
- [ ] Dry-run already succeeded  
- [ ] `--confirm-live` used intentionally  
- [ ] I’m watching the first merges  

---

## 9. Troubleshooting

| Problem | What to try |
|---------|-------------|
| `Cannot connect to browser` | Chrome wasn’t started with `--remote-debugging-port=9222`, or you opened a different Chrome |
| Runner can’t find buttons | SAM selectors need tuning in `agents/sam_playbook.py` — pause and note the exact button labels from the UI |
| Merge dialog never appears | Pop-up/new window blocked; allow pop-ups for SAM |
| Logged out mid-run | Re-auth in the same Chrome profile, restart with `--limit` remaining items |
| Wrong record merged | Stop immediately; fix queue rules; keep `--limit 1` until stable |

---

## 10. How this fits the bigger agent cluster

| Stage | Where it runs | Who acts |
|-------|---------------|----------|
| Detect duplicates / quality issues | Watchdog | Automated |
| Score near-matches | Fuzzy-Match agent | Automated |
| Choose master via SAM rules | You + queue prep (later: master-chooser helper) | Mostly you at first |
| Execute merges in SAM UI | **Local `sam_rpa_local.py`** | Local RPA on your machine |
| Client summary | Executive Reporting agent | Automated |

Bloomerang / Salesforce / Raiser’s Edge can later use API adapters instead of local RPA. SAM stays on this “you log in, local robot clicks” path.

---

## 11. Next upgrades (when you’re ready)

1. Tighten Playwright selectors against your real SAM DOM  
2. Auto master-chooser implementing the Loom rules  
3. Skip rules for notes / phone matches  
4. Optional “pause before Save” prompt per record  
5. Feed RPA audit logs into the Executive Reporting agent  

---

## Quick command cheat sheet

```bash
# 1) Start Chrome (separate terminal)
chrome --remote-debugging-port=9222 --user-data-dir="$HOME/chrome-sam-rpa-profile"

# 2) Log into SAM → open Duplicate Records page

# 3) Dry-run
python3 sam_rpa_local.py --queue outputs/sam_merge_queue.json --cdp http://127.0.0.1:9222 --dry-run

# 4) One live merge while you watch
python3 sam_rpa_local.py --queue outputs/sam_merge_queue.json --cdp http://127.0.0.1:9222 --live --confirm-live --limit 1
```
