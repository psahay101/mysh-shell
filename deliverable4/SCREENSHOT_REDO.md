# Deliverable 4 screenshots — please redo (about 10 minutes)

Hi Dushyanth — thanks for these, the shell clearly ran fine on your Mac.
The problem is only that the sessions you ran differ from the ones the
written report describes, so the report currently claims things the
images don't show. Below is what worked, what's missing, and the exact
commands to run.

## What was good

- `ls | grep py` and `echo hello world | cat` — both correct pipelines.
- Running a pipeline in the background (`ls | grep py &`) and then `jobs`
  showing it as one job. That was a nice extra and we'd like to keep it.
- The permissions session (`touch` → `setperm` → `mksys` → standard user
  denied `rm`) is exactly right. Only two lines are missing from it.

## What's missing

1. **A three-stage pipeline.** The assignment specifically requires
   "chaining multiple commands using pipes (e.g., `cat file | grep error
   | sort`)", and none of the screenshots show more than two stages. This
   is the important one.
2. **The setup step was skipped**, which is why there was no `words.txt`
   to build that chain from.
3. **Session 2** is missing `whoami`, the admin creating a user with
   `adduser`, and — the point of the figure — a standard user being
   *refused* the admin-only `adduser` command.
4. **Session 3** is missing the `perms` table and a denied `cat` on a
   private file (it currently shows only a denied `rm`).

## Setup — please don't skip this

Run these in a terminal, in the folder where you put `shell.py`
(you renamed it `mysh.py`, either name is fine — just use yours):

```bash
mkdir -p ~/Desktop/mysh_project && cd ~/Desktop/mysh_project
rm -f mysh_users.json mysh_perms.json          # reset accounts to defaults
printf 'alpha error one\nbeta ok two\ngamma error three\n' > words.txt
printf 'MSCS630 Deliverable 4 demo workspace.\n' > readme.txt
python3 mysh.py
```

The reset matters: the sessions below assume the default accounts
(`admin` / `admin123` and `prateek` / `user123`) and that `bob` does not
exist yet.

Type commands **one at a time** — pasting a whole block makes the output
interleave and become unreadable. Run the three sessions **in order**;
session 2 creates the user that session 3 uses.

## Session 1 → `fig1.png` (piping)

Log in as `admin` / `admin123`, then:

```
ls
ls | grep txt
cat words.txt | grep error
cat words.txt | grep error | sort
ls | wc -l
ls | grep txt &
jobs
```

The fourth line is the three-stage chain — that's the one that was
missing. It should print the two `error` lines in sorted order.
Screenshot the whole window including the `python3` line at the top.

## Session 2 → `fig2.png` (authentication)

Still as admin:

```
whoami
adduser bob bob123 standard
logout
```

At the login prompt, type username `bob` and a **wrong** password first
(so the "login incorrect" line appears), then log in properly as
`bob` / `bob123`, and run:

```
whoami
adduser eve eve123 standard
logout
```

That last `adduser` must be **denied** — bob is a standard user, and
showing that refusal is the whole point of this figure.

## Session 3 → `fig3.png` (file permissions)

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

Expected: `cat system.conf` **succeeds** (a system file is readable by
others), and the other three are all **denied**. Then log back in as
`admin` / `admin123` and finish with:

```
rm secret.txt
perms
```

showing that the admin can do what the standard user could not.

## Saving

Save the three captures as `fig1.png`, `fig2.png`, `fig3.png` and send
them back. If a session is too tall for one screenshot, split it into
`fig1_part1.png` / `fig1_part2.png` (etc.) with a little overlap — that's
fine, we'll lay them out as parts.

Thanks — this is the last piece we need for Deliverable 4.
