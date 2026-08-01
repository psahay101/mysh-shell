# How to take the 3 screenshots for Deliverable 2

The report (`Project Deliverable 2 Report.docx`) has three bold placeholders.
Take one screenshot per session below and paste it over the matching
placeholder (keep the *Figure N.* caption line under it).

Every schedule is deterministic — your times and metrics tables will match
the numbers quoted in the report exactly.

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

`timescale 0.4` makes the sleeps faster (0.4 real seconds per simulated
second) — the printed times are unaffected.

## Screenshot 1 — Round-Robin with two quantums  (placeholder: SCREENSHOT 1)

```
timescale 0.4
addproc procA 5
addproc procB 3
addproc procC 4
procs
runrr 2
addproc procA 5
addproc procB 3
addproc procC 4
runrr 4
```

Wait for each runrr to finish (about 5 s each) before typing the next line.
Screenshot the whole session (both runs).

## Screenshot 2 — priority scheduling with preemption  (placeholder: SCREENSHOT 2)

```
timescale 0.4
addproc batch_export 6 5
addproc sensor_poll 3 3 2
addproc estop_check 2 1 3
procs
runpriority
```

(`addproc NAME BURST PRIORITY ARRIVAL` — priority 1 is highest.)
You should see two PREEMPTION lines at t=2s and t=3s.

## Screenshot 3 — metrics tables  (placeholder: SCREENSHOT 3)

```
timescale 0
addproc procA 5
addproc procB 3
addproc procC 4
runrr 2
addproc batch_export 6 5
addproc sensor_poll 3 3 2
addproc estop_check 2 1 3
runpriority
```

`timescale 0` runs instantly; screenshot so both "performance metrics"
tables are visible.

## Reference

`p2/screenshots/` contains rendered previews of the expected output
(reference only — paste your own captures).
