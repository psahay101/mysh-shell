# How to take the 3 screenshots for Deliverable 4

The report (`Project Deliverable 4 Report.docx`) has three bold placeholders.
Take one screenshot per session below and paste it over the matching
placeholder (keep the *Figure N.* caption line under it).

## Setup (IMPORTANT — do this once, before Session 1)

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

Then reset state and seed the demo files (the json files live NEXT TO
shell.py; deleting them makes the shell recreate the default accounts):

```bash
rm -f "$SHELL_DIR/mysh_users.json" "$SHELL_DIR/mysh_perms.json"
printf 'alpha error one\nbeta ok two\ngamma error three\n' > words.txt
printf 'MSCS630 Deliverable 4 demo workspace.\n' > readme.txt
python3 "$SHELL_DIR/shell.py"
```

Deleting the two json files resets users/permissions to a fresh state
(the shell recreates the default accounts: **admin/admin123** and
**prateek/user123**). Run the sessions IN ORDER — session 2 creates the
user `bob` that session 3 uses. In your terminal the password will simply
not echo (that's normal).

## Screenshot 1 — piping  (placeholder: SCREENSHOT 1)

Log in as `admin` / `admin123`, then:

```
ls
ls | grep txt
cat words.txt | grep error
cat words.txt | grep error | sort
ls | wc -l
```

## Screenshot 2 — authentication  (placeholder: SCREENSHOT 2)

Continue (or restart the shell), as admin:

```
whoami
adduser bob bob123 standard
logout
```

Then log in as `bob` but type a WRONG password first (shows "login
incorrect"), then `bob` / `bob123`, then:

```
whoami
adduser eve eve123 standard
logout
```

(`adduser` must be denied — bob is a standard user.)

## Screenshot 3 — file permissions  (placeholder: SCREENSHOT 3)

Log in as `admin` / `admin123`:

```
cp words.txt system.conf
mksys system.conf
touch secret.txt
setperm secret.txt rw- ---
perms
logout
```

Log in as `bob` / `bob123`:

```
cat system.conf
rm system.conf
cat secret.txt
touch secret.txt
logout
```

(read of system.conf succeeds; the other three are denied.)

Log in as `admin` / `admin123`:

```
rm secret.txt
perms
```

## Reference

`p4/screenshots/` contains rendered previews of expected output
(reference only — paste your own captures).
