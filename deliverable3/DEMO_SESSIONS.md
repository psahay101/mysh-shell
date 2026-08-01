# How to take the 3 screenshots for Deliverable 3

The report (`Project Deliverable 3 Report.docx`) has three bold placeholders.
Take one screenshot per session below and paste it over the matching
placeholder (keep the *Figure N.* caption line under it).

## Setup — what a teammate needs

- Linux or macOS (or WSL on Windows) — the shell uses fork(), which
  does not exist on native Windows Python
- Python 3.8+ (only the standard library, nothing to install)
- This folder (shell.py + this guide). `SHELL_DIR` below is the folder
  you received — set it once:

```bash
SHELL_DIR="$(pwd)"        # run this INSIDE the folder you received
mkdir -p ~/mysh_demo && cd ~/mysh_demo
```

- Save your captures as fig1.png / fig2.png / fig3.png in this folder.
- Screenshot just the terminal window; any modern terminal is fine.

Then start the shell:

```bash
python3 "$SHELL_DIR/shell.py"
```

## Screenshot 1 — paging, faults, deallocation  (placeholder: SCREENSHOT 1)

```
meminit 3 fifo
memaccess P1 1
memaccess P1 2
memaccess P2 1
memaccess P1 1
memaccess P2 2
memaccess P1 1
memstats
memfree P2
memstats
```

You should see: 3 faults filling memory, a hit, then FIFO evictions on
overflow, per-process stats (P1: 3 faults / P2: 2 faults, 5 total,
2 evictions), and the frames going free after `memfree P2`.

## Screenshot 2 — FIFO vs LRU  (placeholder: SCREENSHOT 2)

```
meminit 3 fifo
memaccess P1 1
memaccess P1 2
memaccess P1 1
memaccess P1 3
memaccess P1 1
memaccess P1 4
memaccess P1 1
memaccess P1 5
memstats
meminit 3 lru
memaccess P1 1
memaccess P1 2
memaccess P1 1
memaccess P1 3
memaccess P1 1
memaccess P1 4
memaccess P1 1
memaccess P1 5
memstats
```

Same reference string both times: FIFO ends with **6 faults**, LRU with
**5 faults** (LRU keeps the hot page 1; FIFO evicts it at access 6).

## Screenshot 3 — synchronization  (placeholder: SCREENSHOT 3)

```
racedemo unsafe 300000
racedemo safe 300000
prodcons 2 2 4 3
```

Notes:
- `unsafe` will report a large number of lost updates — **your number
  will differ from anyone else's and from run to run; that variability
  is the point** (it's a real race between real threads).
- `safe` always lands exactly on 600000.
- `prodcons` line order varies run to run (real threads), but it always
  ends with "produced 8, consumed 8 ... True", "maximum threads inside
  the critical section at once: 1", and "no race conditions".

## Reference

`p3/screenshots/` contains rendered previews of expected output
(reference only — paste your own captures).
