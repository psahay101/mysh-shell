# How to take the 3 screenshots for Deliverable 1

The report (`Project Deliverable 1 Report.docx`) has three bold placeholders.
Take one screenshot per session below and paste it over the matching
placeholder in Word/Docs (keep the *Figure N.* caption line under it).

## Setup — what a teammate needs

- Linux or macOS (or WSL on Windows) — the shell uses fork(), which
  does not exist on native Windows Python
- Python 3.8+ (only the standard library, nothing to install)
- This folder (shell.py + this guide). `SHELL_DIR` below is the folder
  you received — set it once:

```bash
SHELL_DIR="$(pwd)"        # run this INSIDE the folder you received
mkdir -p ~/mysh_demo && cd ~/mysh_demo
printf 'MSCS630 Deliverable 1 demo workspace.\nThis file is read by the cat command.\n' > readme.txt
```

- Save your captures as fig1.png / fig2.png / fig3.png in this folder.
- Screenshot just the terminal window; any modern terminal is fine.

Then start the shell:

```bash
python3 "$SHELL_DIR/shell.py"
```

You are now inside `mysh`. After each session below, screenshot the whole
terminal window, then run the next session (restart the shell with the same
`python3` command if you exited).

## Screenshot 1 — built-in commands  (placeholder: SCREENSHOT 1)

Type these commands one by one:

```
pwd
ls
cat readme.txt
mkdir projects
cd projects
pwd
touch notes.txt robot_log.txt
ls
echo Hello from my custom shell
rm notes.txt robot_log.txt
ls
cd ..
rmdir projects
ls
```

Do NOT run `clear` — it would wipe the session you need to capture.

## Screenshot 2 — foreground/background jobs  (placeholder: SCREENSHOT 2)

```
date
sleep 3
sleep 200 &
sleep 400 &
jobs
kill -STOP %2
jobs
bg 2
jobs
kill %1
jobs
sleep 10 &
fg 3
jobs
kill %2
jobs
```

Notes: the long sleeps (200 s, 400 s) are deliberate — they outlast however
long you take to type, so the jobs stay Running instead of finishing on you.
Two intentional pauses: `sleep 3` blocks the prompt for 3 seconds (that is
the foreground wait being shown), and `fg 3` blocks for about 10 seconds
while that job finishes. If a `jobs` line still shows a killed job as
Running, run `jobs` once more so the `Done` line appears.

## Screenshot 3 — error handling  (placeholder: SCREENSHOT 3)

```
foobar_command
cat missing_file.txt
cd /no/such/directory
mkdir projects
mkdir projects
rm not_here.txt
rmdir projects
rmdir projects
fg 99
bg %7
kill abc
kill -BADSIG 1234
echo "unclosed quote
ls one two
```

## Reference

`p1/screenshots/` contains my rendered previews of what each session's
output should look like (for your reference only — do NOT submit those;
paste your own captures).
