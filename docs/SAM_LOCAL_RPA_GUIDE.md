# SAM Local RPA — Literal Step-by-Step Checklist

Follow this in order. Do not skip ahead.  
Check each box only after that tiny action is done.

Your Loom reference:  
https://www.loom.com/share/55df212f59a247799830c617cf804470

---

# STEP 1 — Install tools on your laptop (one time)

Goal: your computer can run the RPA script.

### 1.1 Open a terminal
- [ ] Mac: open **Terminal**
- [ ] Windows: open **PowerShell**

### 1.2 Go to this project folder
- [ ] Type (edit the path if yours is different):

```bash
cd /path/to/AIJourney
```

- [ ] Press Enter
- [ ] Confirm you are in the repo (you should see files like `sam_rpa_local.py`)

```bash
ls
```

### 1.3 Create a Python virtual environment
- [ ] Run:

```bash
python3 -m venv .venv
```

- [ ] Wait until it finishes with no error

### 1.4 Turn the virtual environment on
- [ ] Mac/Linux:

```bash
source .venv/bin/activate
```

- [ ] Windows:

```powershell
.venv\Scripts\activate
```

- [ ] Confirm it worked: your prompt should show `(.venv)`

### 1.5 Install Python packages
- [ ] Run:

```bash
pip install -r requirements.txt
pip install playwright
```

- [ ] Wait until both finish with no error

### 1.6 Install browser files Playwright needs
- [ ] Run:

```bash
playwright install chromium
```

- [ ] Wait until it finishes

### 1.7 Confirm Step 1 is done
- [ ] Run:

```bash
python3 sam_rpa_local.py --help
```

- [ ] You should see help text (flags like `--queue`, `--dry-run`, `--live`)
- [ ] **Step 1 complete**

---

# STEP 2 — Build an approved merge queue

Goal: create a small JSON file listing what to merge (1 pair for the first try).

### 2.1 Create the outputs folder
- [ ] In the same terminal (venv still on), run:

```bash
mkdir -p outputs
```

### 2.2 Copy the example queue file
- [ ] Run:

```bash
cp samples/sam_merge_queue.example.json outputs/sam_merge_queue.json
```

### 2.3 Open the queue file in an editor
- [ ] Open `outputs/sam_merge_queue.json` in Cursor / VS Code / Notepad

### 2.4 Replace the example with ONE real approved pair
For that one pair, fill in:

- [ ] `master.birth_mother_id` = the master profile’s Birth Mother ID
- [ ] `master.full_name` = master name
- [ ] `master.sf_migration_contact_id` = the SF Migration Contact ID if it has one (or `null`)
- [ ] `duplicate.birth_mother_id` = the duplicate’s Birth Mother ID
- [ ] `duplicate.full_name` = duplicate name
- [ ] `decision` must be exactly `"approved"`
- [ ] `match_reason` must be `"name"` for this first run (not `"phone"`)

### 2.5 Double-check master choice (from your Loom rules)
Before saving, confirm:

- [ ] Master is the one with **SF Migration Contact ID** when only one has it
- [ ] If both have SF Migration Contact ID, master has the **lower Birth Mother ID**
- [ ] You are **not** merging a pair where both have conflicting notes
- [ ] This is a **name** match, not phone-only

### 2.6 Save the file
- [ ] Save `outputs/sam_merge_queue.json`
- [ ] **Step 2 complete**

---

# STEP 3 — Start Chrome and log into SAM yourself

Goal: Chrome is open in “debug mode,” you are logged into SAM, Duplicate Records page is visible.

### 3.1 Fully quit Chrome
- [ ] Close all Chrome windows
- [ ] Mac: Chrome menu → Quit Google Chrome  
- [ ] Windows: make sure Chrome is not still running in the system tray

### 3.2 Start Chrome with remote debugging
Open a **new** terminal window (leave your Python venv terminal alone).

- [ ] Mac — paste and run:

```bash
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
  --remote-debugging-port=9222 \
  --user-data-dir="$HOME/chrome-sam-rpa-profile"
```

- [ ] Windows PowerShell — paste and run:

```powershell
& "C:\Program Files\Google\Chrome\Application\chrome.exe" `
  --remote-debugging-port=9222 `
  --user-data-dir="$env:USERPROFILE\chrome-sam-rpa-profile"
```

- [ ] Linux:

```bash
google-chrome --remote-debugging-port=9222 --user-data-dir="$HOME/chrome-sam-rpa-profile"
```

- [ ] A Chrome window opens

### 3.3 Log into SAM in that Chrome window
- [ ] Go to your normal SAM URL
- [ ] Complete username/password
- [ ] Complete SSO / 2FA if asked
- [ ] Confirm you can see the normal SAM home/dashboard

### 3.4 Open the Duplicate Records page
- [ ] Navigate to the same Duplicate Records report/alert page from your Loom
- [ ] Confirm you can see the duplicate list / alert screen
- [ ] Leave this Chrome window open (do not close it)
- [ ] **Step 3 complete**

---

# STEP 4 — Dry-run (practice, no real merge)

Goal: prove the script can attach to your Chrome and read the queue. No Save/confirm merge.

### 4.1 Go back to your Python terminal
- [ ] Make sure `(.venv)` is still showing
- [ ] If not, run `source .venv/bin/activate` (or Windows activate command) again
- [ ] Make sure you are in the repo folder:

```bash
cd /path/to/AIJourney
```

### 4.2 Run dry-run
- [ ] Run:

```bash
python3 sam_rpa_local.py \
  --queue outputs/sam_merge_queue.json \
  --cdp http://127.0.0.1:9222 \
  --dry-run
```

### 4.3 Check the terminal output
You want to see all of these:

- [ ] `Loaded 1 queue item(s)` (or however many you put in the queue)
- [ ] `Mode: DRY-RUN`
- [ ] `Connecting to Chrome via CDP...`
- [ ] `Attached. Active page: ...` (some SAM URL)
- [ ] Lines that say `DRY-RUN would click...` / `DRY-RUN would paste...`
- [ ] `Wrote audit log → outputs/sam_rpa/...`

### 4.4 If dry-run fails
- [ ] If it says it cannot connect to browser → repeat Step 3 (Chrome must be started with port `9222`)
- [ ] If it skips your item → check `decision` is `"approved"` and `match_reason` is not `"phone"`
- [ ] Fix the issue, then run the dry-run command again

### 4.5 Confirm Step 4 is done
- [ ] Dry-run finished without a connection error
- [ ] Audit file exists under `outputs/sam_rpa/`
- [ ] **Step 4 complete**

---

# STEP 5 — First live merge (exactly 1 record, you watch)

Goal: one real merge while you watch the screen.

### 5.1 Pre-flight checks
- [ ] Chrome is still open and still logged into SAM
- [ ] Duplicate Records page is still available
- [ ] Your queue still has only the pair(s) you intend
- [ ] You are ready to watch the Chrome window

### 5.2 Run live with limit 1
- [ ] In the Python terminal, run:

```bash
python3 sam_rpa_local.py \
  --queue outputs/sam_merge_queue.json \
  --cdp http://127.0.0.1:9222 \
  --live \
  --confirm-live \
  --limit 1
```

### 5.3 Watch Chrome during the run
Watch for the script trying to:

- [ ] Open **Advanced Options**
- [ ] Open **Merge Birth Mother**
- [ ] Paste the **master Birth Mother ID**
- [ ] Click **Save**
- [ ] Confirm **Yes, merge these records**

### 5.4 Verify in SAM (manual confirmation)
After the script finishes:

- [ ] Refresh the Duplicate Records / alert page
- [ ] Confirm the duplicate no longer appears (same check as in your Loom)
- [ ] Open the master profile and confirm it still looks right

### 5.5 If something looks wrong
- [ ] Stop immediately (do not raise `--limit`)
- [ ] Note which button/label failed
- [ ] Tell me the exact on-screen button text so we can update selectors in `agents/sam_playbook.py`

### 5.6 Confirm Step 5 is done
- [ ] One merge attempted
- [ ] You verified the result in SAM
- [ ] Audit log written under `outputs/sam_rpa/`
- [ ] **Step 5 complete**

---

# STEP 6 — Scale up only after Step 5 worked

Goal: do a few more merges safely.

### 6.1 Add more approved pairs to the queue
- [ ] Edit `outputs/sam_merge_queue.json`
- [ ] Add only pairs you have approved
- [ ] Keep `match_reason` as `"name"` for now
- [ ] Save the file

### 6.2 Dry-run the bigger queue
- [ ] Run the same dry-run command from Step 4.2
- [ ] Confirm every item is planned or intentionally skipped
- [ ] Fix any bad rows before live

### 6.3 Live small batch
- [ ] Start small:

```bash
python3 sam_rpa_local.py \
  --queue outputs/sam_merge_queue.json \
  --cdp http://127.0.0.1:9222 \
  --live \
  --confirm-live \
  --limit 3
```

- [ ] Watch the first one or two
- [ ] Spot-check results in SAM

### 6.4 Raise the limit gradually
- [ ] Only if the small batch looked correct
- [ ] Increase `--limit` slowly (3 → 10)
- [ ] Keep doing name-based pairs only
- [ ] Leave phone-only matches for manual work

### 6.5 End-of-session shutdown
- [ ] Stop the Python command if it is still running
- [ ] You may close the debug Chrome window when finished
- [ ] Keep the audit files in `outputs/sam_rpa/` for your records
- [ ] **Step 6 complete**

---

# Quick “where am I?” checklist

- [ ] Step 1 done = `python3 sam_rpa_local.py --help` works  
- [ ] Step 2 done = `outputs/sam_merge_queue.json` has a real approved pair  
- [ ] Step 3 done = debug Chrome open + logged into SAM + Duplicate Records visible  
- [ ] Step 4 done = dry-run attached and wrote an audit log  
- [ ] Step 5 done = one live merge verified in SAM  
- [ ] Step 6 done = small batch successful  

---

# Do-not-do list

- Do not run `--live` before a successful dry-run
- Do not run without `--confirm-live`
- Do not start with more than `--limit 1`
- Do not include phone-only matches in v1
- Do not merge when both profiles have conflicting notes
- Do not put SAM passwords into the cloud agent chat
