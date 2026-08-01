# mysh — a Unix-like shell that simulates the OS underneath it

`mysh` is a custom shell written from scratch in Python 3. Beyond running
commands, it internally simulates the operating-system mechanisms a real
shell delegates to the kernel: CPU scheduling, paged virtual memory,
process synchronization, and a permission model.

Built for MSCS630 Advanced Operating Systems (University of the
Cumberlands) as four cumulative deliverables. Each folder contains the
shell as it stood at the end of that stage, so the progression is
visible; `deliverable4/shell.py` is the complete program.

Standard library only — no dependencies.

```bash
python3 deliverable4/shell.py     # log in as admin / admin123
```

## What it does

**Process management** — external commands run through the classic
`fork()` → `execvp()` → `waitpid()` cycle. Every command gets its own
process group, so terminal signals reach the foreground job and not the
shell, and the terminal is handed over with `tcsetpgrp()` and reclaimed
afterwards. Background jobs are tracked in a job table, polled with
`WNOHANG` so nothing becomes a zombie, and controlled with `jobs`, `fg`,
and `bg`. Twelve built-ins (`cd`, `pwd`, `ls`, `cat`, `mkdir`, `rmdir`,
`rm`, `touch`, `echo`, `clear`, `kill`, `exit`) are implemented natively
rather than shelled out.

**CPU scheduling** — simulated processes are scheduled by two algorithms
against a logical clock, with `time.sleep()` for pacing so runs are
reproducible to the second regardless of speed:

| Command | Behavior |
|---|---|
| `runrr [quantum]` | Round-Robin, configurable time slice, FIFO ready queue |
| `runpriority` | Preemptive priority via a heap; equal priorities run FCFS |

Both print per-process waiting, turnaround, and response times. The
quantum trade-off is visible directly: on bursts of 5/3/4 seconds, a
2-second slice gives 2.00 s average response and 10.67 s turnaround,
while a 4-second slice improves turnaround to 10.00 s but degrades
response to 3.67 s.

**Virtual memory** — `meminit` builds a physical memory of fixed-size
frames managed by FIFO or LRU replacement; `memaccess` faults pages in,
evicting victims when memory is full; `memstats` reports the frame table,
per-process residency, and fault counts. On the reference string
`1 2 1 3 1 4 1 5` with three frames, FIFO takes 6 faults and LRU takes 5,
because LRU keeps the hot page that FIFO blindly evicts.

**Synchronization** — real threads, not simulations. `racedemo unsafe`
runs an unprotected read-modify-write and reliably loses a third of its
updates; `racedemo safe` wraps the same code in a mutex and lands exactly
on the expected value every time. `prodcons` solves the bounded-buffer
Producer-Consumer problem with a mutex plus `empty`/`full` counting
semaphores, and verifies its own correctness by asserting that no two
threads were ever inside the critical section together and that every
item was consumed exactly once.

**Piping** — `cat access.log | grep error | sort` builds a real pipeline:
one `os.pipe()` per stage boundary, one `fork()` per stage, `dup2()` to
rewire the children's standard streams, and the parent closing its copies
so end-of-file actually propagates. All stages share one process group,
so a pipeline is one job for `jobs`, `fg`, and `kill`.

**Security** — login is required; credentials are stored as salted
SHA-256 hashes. Users are `admin` or `standard`, and a simulated
permission layer gives files an owner plus `rwx` bits for owner and
others, enforced at the shell's own file operations. Permission records
are keyed by resolved path and compared by inode, so neither a symbolic
link nor a hard link can be used as an unprotected alias for a protected
file, and the credential store itself is not readable or writable by a
standard user. Concurrent sessions update the stores under an exclusive
file lock with atomic replacement, so two shells editing accounts at once
cannot lose each other's writes.

## Layout

| Folder | Adds |
|---|---|
| `deliverable1/` | Built-ins, `fork`/`exec`/`wait`, job control, error handling |
| `deliverable2/` | Round-Robin and preemptive priority scheduling with metrics |
| `deliverable3/` | Paging with FIFO/LRU replacement, mutexes, semaphores, Producer-Consumer |
| `deliverable4/` | Piping, authentication with roles, file permissions |

Each folder's `DEMO_SESSIONS.md` lists command-by-command sessions that
exercise that stage's features.

## Design notes

A few decisions worth calling out, all of them driven by bugs found while
testing:

- **The pipeline is split before quotes are removed**, so `echo "a|b"`
  prints a literal `a|b` while `echo a | b` builds a pipeline. Mid-word
  `#` is not treated as a comment either — the borrowed lexer that did
  turned `rm notes#backup` into `rm notes`, deleting the wrong file.
- **Only read-only built-ins may be pipeline stages.** A stage runs in a
  forked child, so a state change made there either vanishes with the
  child or reaches the disk while the parent's memory diverges from it.
- **Permission and credential stores are re-read at command execution**,
  not at startup, so a policy set in another session is enforced here and
  this session cannot overwrite it with a stale copy.
- **The permission layer is honest about its limit**: it governs the
  commands the shell implements, so an external `/bin/rm` reaches the real
  filesystem with the real kernel's permissions. Enforcing protection in
  an interpreter is not the same as enforcing it in a kernel.

## Authors

Chun Shuo Sheu, Dushyanth S Nandeesh, and Prateek Sahay
