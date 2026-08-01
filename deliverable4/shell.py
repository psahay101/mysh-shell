#!/usr/bin/env python3
"""
mysh - a small Unix-like shell with process management, job control,
and an internal process scheduler.

MSCS630 Advanced Operating Systems - Project Deliverable 4
Author: Prateek Sahay

Features carried over from Deliverable 1
----------------------------------------
* Built-in commands: cd, pwd, exit, echo, clear, ls, cat, mkdir, rmdir,
  rm, touch, kill, jobs, fg, bg
* External commands executed with fork() + execvp() + waitpid()
* Foreground and background execution ("cmd &"), job table, fg/bg/jobs
* Error handling with clear messages for invalid commands and arguments

New in Deliverable 2: process scheduling
----------------------------------------
* addproc NAME BURST [PRIORITY] [ARRIVAL] - create a simulated process
  with a CPU burst in seconds, a priority (1 = highest), and an
  optional arrival time relative to the start of the schedule
* procs                 - list the simulated processes waiting to run
* runrr [QUANTUM]       - Round-Robin scheduling with a configurable
  time slice (default 2 s); process execution is simulated with
  time.sleep() and switches are logged against a logical clock
* runpriority           - preemptive priority scheduling backed by a
  heap; equal priorities run first-come-first-served, and a
  higher-priority arrival preempts the running process immediately
* timescale [FACTOR]    - scale simulated seconds to real seconds so
  long schedules can be demonstrated quickly
* Both schedulers end with a metrics table reporting waiting time,
  turnaround time, and response time for every process plus averages

New in Deliverable 3: memory management and process synchronization
-------------------------------------------------------------------
* meminit FRAMES [fifo|lru] - create a simulated physical memory of
  fixed-size page frames with the chosen page replacement algorithm
* memaccess PROC PAGE      - a process references a virtual page;
  hits, page faults, loads, and evictions are logged
* memstats                 - frame table, per-process resident pages,
  page-fault counts, and memory usage
* memfree PROC             - deallocate every frame owned by a process
* racedemo safe|unsafe [N] - two threads increment a shared counter,
  with or without a mutex, demonstrating lost updates vs. correctness
* prodcons [NP NC ITEMS BUF] - classical Producer-Consumer problem
  with a bounded buffer guarded by a mutex plus empty/full semaphores

New in Deliverable 4: integration and security
----------------------------------------------
* Command piping: cmd1 | cmd2 | cmd3 ... - the output of each stage
  feeds the next through real pipes (os.pipe + dup2 + fork); built-in
  commands may appear as pipeline stages, and pipelines can run in the
  background with &
* User authentication: the shell requires a login (users stored with
  salted SHA-256 password hashes in mysh_users.json); roles are admin
  and standard; whoami, logout, adduser (admin only)
* File permissions: a simulated permission layer (mysh_perms.json)
  gives files an owner plus rwx bits for owner and others, enforced by
  the shell's built-ins; setperm, perms, mksys (mark a system file that
  standard users cannot modify); admin bypasses all checks
"""

import errno
import fcntl
import io
import getpass
import hashlib
import json
import heapq
import math
import os
import shlex
import signal
import stat
import sys
import threading
import time
from collections import deque

PROMPT_NAME = "mysh"

# Built-ins safe to run as a pipeline stage: they only read state and
# write to standard output, so running them in a forked child is
# harmless. Everything else is refused inside a pipeline.
# Marks an unquoted '&' during parsing. It contains NUL bytes, which are
# rejected in ordinary command text, so user input can never forge it.
AMP_SENTINEL = "\0&\0"

PIPE_SAFE_BUILTINS = {"echo", "pwd", "ls", "cat", "whoami", "perms",
                      "procs", "memstats"}


class Job:
    """One background or stopped job (a command or a whole pipeline)."""

    def __init__(self, job_id, pgid, command, pids=None):
        self.job_id = job_id
        self.pgid = pgid          # process group id (== first child pid)
        self.command = command
        self.status = "Running"   # Running | Stopped | Done
        self.pids = list(pids) if pids else [pgid]


class SimProcess:
    """A simulated process scheduled inside the shell (not a real fork)."""

    def __init__(self, pid, name, burst, priority, arrival):
        self.pid = pid
        self.name = name
        self.burst = float(burst)       # total CPU time needed (seconds)
        self.remaining = float(burst)
        self.priority = priority        # 1 = highest priority
        self.arrival = float(arrival)   # seconds after schedule start
        self.first_run = None           # logical time of first CPU slice
        self.completion = None          # logical completion time


class PagingMemory:
    """Simulated physical memory: fixed-size frames + page replacement."""

    def __init__(self, frame_count, algorithm):
        self.frame_count = frame_count
        self.algorithm = algorithm          # "fifo" | "lru"
        self.frames = [None] * frame_count  # index -> (proc, page) | None
        self.load_seq = [0] * frame_count   # when the page was loaded
        self.used_seq = [0] * frame_count   # when the page was last used
        self.clock = 0                      # logical access counter
        self.faults = {}                    # proc -> page fault count
        self.accesses = {}                  # proc -> total accesses
        self.total_evictions = 0

    def find(self, proc, page):
        for i, entry in enumerate(self.frames):
            if entry == (proc, page):
                return i
        return None

    def victim_frame(self):
        """Pick the frame to evict according to the algorithm."""
        if self.algorithm == "fifo":
            seq = self.load_seq
        else:                               # lru
            seq = self.used_seq
        return min(range(self.frame_count), key=lambda i: seq[i])

    def access(self, proc, page):
        """Reference (proc, page); returns a log line describing it."""
        self.clock += 1
        self.accesses[proc] = self.accesses.get(proc, 0) + 1
        frame = self.find(proc, page)
        if frame is not None:
            self.used_seq[frame] = self.clock
            return (f"[hit]   {proc} page {page} already in frame {frame}")
        # Page fault: load into a free frame, or evict a victim.
        self.faults[proc] = self.faults.get(proc, 0) + 1
        if None in self.frames:
            frame = self.frames.index(None)
            evicted = ""
        else:
            frame = self.victim_frame()
            old_proc, old_page = self.frames[frame]
            self.total_evictions += 1
            evicted = (f" (evicted {old_proc} page {old_page} by "
                       f"{self.algorithm.upper()})")
        self.frames[frame] = (proc, page)
        self.load_seq[frame] = self.clock
        self.used_seq[frame] = self.clock
        return (f"[FAULT] {proc} page {page} loaded into frame "
                f"{frame}{evicted}")

    def free(self, proc):
        """Deallocate every frame owned by proc; returns pages freed."""
        freed = []
        for i, entry in enumerate(self.frames):
            if entry is not None and entry[0] == proc:
                freed.append(entry[1])
                self.frames[i] = None
        return freed


class Shell:
    def __init__(self):
        self.jobs = {}            # job_id -> Job
        self.next_job_id = 1
        self.sim_procs = []       # SimProcess objects awaiting a run
        self.next_sim_pid = 1
        self.timescale = 1.0      # real seconds per simulated second
        self.memory = None        # paging system, created by meminit
        base = os.path.dirname(os.path.abspath(__file__))
        self.users_path = os.path.join(base, "mysh_users.json")
        self.perms_path = os.path.join(base, "mysh_perms.json")
        self.users = self.load_json(self.users_path,
                                    ("salt", "hash", "role"))
        self.perms = self.load_json(self.perms_path,
                                    ("owner", "owner_perms",
                                     "other_perms"))
        # None means "unreadable": keep the shell usable but never treat
        # it as an empty store, or a damaged file would be replaced by
        # fresh default credentials.
        self.users_readable = self.users is not None
        self.perms_readable = self.perms is not None
        if not self.users_readable:
            self.users = {}
            self.error("the user database is unreadable; no account "
                       "changes will be saved until it is repaired")
        if not self.perms_readable:
            self.perms = {}
        self.current_user = None
        self.current_role = None
        if self.users_readable and not self.users:
            # First run: create the default accounts.
            self.create_user("admin", "admin123", "admin")
            self.create_user("prateek", "user123", "standard")
            print("created default users in mysh_users.json: "
                  "admin/admin123 (admin), prateek/user123 (standard)")
        try:
            self.interactive = bool(sys.stdin) and sys.stdin.isatty()
        except (ValueError, OSError):
            self.interactive = False
        if sys.stdin is None:
            sys.stderr.write(f"{PROMPT_NAME}: no standard input\n")
            raise SystemExit(1)
        self.exit_requested = False
        if self.interactive:
            # The shell itself must ignore job-control signals so that
            # Ctrl-C / Ctrl-Z reach only the foreground child process.
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            signal.signal(signal.SIGTSTP, signal.SIG_IGN)
            signal.signal(signal.SIGTTOU, signal.SIG_IGN)

    # ------------------------------------------------------------------ #
    # Main loop
    # ------------------------------------------------------------------ #
    def refresh_stores(self):  # noqa: D401
        """Re-read the credential and permission files.

        Another session (or the admin in another terminal) may have
        changed them; without this the shell would enforce a stale
        policy and later overwrite the file with its stale copy.
        """
        users = self.load_json(self.users_path, ("salt", "hash", "role"),
                               previous=self.users)
        perms = self.load_json(self.perms_path,
                               ("owner", "owner_perms", "other_perms"),
                               previous=self.perms)
        self.users_readable = users is not None
        self.perms_readable = perms is not None
        self.users = users if users is not None else self.users
        self.perms = perms if perms is not None else self.perms

    def run(self):
        while not self.exit_requested:
            if self.current_user is None:
                if not self.authenticate():
                    break
                continue
            self.reap_background_jobs()
            try:
                cwd = os.getcwd()
            except OSError:
                cwd = "(deleted)"
            prompt = f"{PROMPT_NAME}[{self.current_user}]:{cwd}$ "
            try:
                sys.stdout.write(prompt)
                sys.stdout.flush()
                raw = sys.stdin.buffer.readline()
            except KeyboardInterrupt:
                sys.stdout.write("\n")
                continue
            # Decode defensively: invalid bytes become replacement
            # characters instead of an exception or lost input.
            line = raw.decode(errors="replace")
            if line == "":                       # end of input (Ctrl-D)
                sys.stdout.write("\n")
                self.builtin_exit([])            # same job cleanup as exit
                break
            if not self.interactive:
                # Echo the command so piped demo sessions read like a
                # real terminal session.
                sys.stdout.write(line)
                sys.stdout.flush()
            self.execute_line(line.strip())

    @staticmethod
    def split_pipeline(line):
        """Split a line at UNQUOTED, UNESCAPED '|' characters.

        Splitting before quote removal is what lets a quoted "|" be an
        ordinary argument while a bare | separates pipeline stages.
        """
        segments, current = [], []
        quote = None
        i = 0
        while i < len(line):
            ch = line[i]
            if quote is not None:
                current.append(ch)
                if ch == "\\" and quote == '"' and i + 1 < len(line):
                    current.append(line[i + 1])
                    i += 2
                    continue
                if ch == quote:
                    quote = None
                i += 1
                continue
            if ch in ("'", '"'):
                quote = ch
                current.append(ch)
            elif ch == "\\" and i + 1 < len(line):
                current.append(ch)
                current.append(line[i + 1])
                i += 2
                continue
            elif ch == "|":
                segments.append("".join(current))
                current = []
            elif ch == "&":
                # Record an UNQUOTED '&' as its own word (surrounded by
                # spaces) so that `cmd&` is recognized exactly like
                # `cmd &`, while a quoted "&" stays an argument. The
                # sentinel contains a NUL, which the tokenizer rejects
                # in ordinary input, so it cannot be forged.
                current.append(" " + AMP_SENTINEL + " ")
            else:
                current.append(ch)
            i += 1
        segments.append("".join(current))
        return segments

    def tokenize(self, text):
        """Split one pipeline stage into words, honoring quotes."""
        lexer = shlex.shlex(text, posix=True)
        lexer.whitespace_split = True
        # '#' must not start a comment mid-word: without this, a command
        # such as `rm notes#backup` would silently act on `notes`.
        lexer.commenters = ""
        return list(lexer)

    def execute_line(self, line):
        if not line or line.startswith("#"):
            return
        if "\0" in line:
            # Checked on the raw line: the parser injects a NUL-bearing
            # marker for '&', so user text must be screened first.
            self.error("command contains a null byte")
            return
        # Pick up policy changes made by another session while this one
        # was sitting at the prompt.
        self.refresh_stores()
        raw_segments = self.split_pipeline(line)
        try:
            segments = [self.tokenize(seg) for seg in raw_segments]
        except ValueError as exc:
            self.error(f"parse error: {exc}")
            return

        background = False
        if segments[-1] and segments[-1][-1] == AMP_SENTINEL:
            background = True
            segments[-1] = segments[-1][:-1]
        for seg in segments:
            for word in seg:
                if AMP_SENTINEL in word:
                    self.error("syntax error: '&' is only allowed at the "
                               "end of a command")
                    return
        for seg in segments:
            for word in seg:
                if "\0" in word:
                    self.error("command contains a null byte")
                    return

        if len(segments) > 1:
            for seg in segments:
                if not seg:
                    self.error("syntax error: empty pipeline stage")
                    return
            self.launch_pipeline(segments, background)
            return

        tokens = segments[0]
        if not tokens:
            if background:
                self.error("syntax error: no command before '&'")
            return

        name, args = tokens[0], tokens[1:]
        if name == "":
            self.error("empty command")
            return
        if "\0" in name or any("\0" in a for a in args):
            self.error("command contains a null byte")
            return
        builtin = getattr(self, "builtin_" + name, None)
        if builtin is not None:
            if background:
                self.error(f"{name}: built-in commands run in the foreground")
                return
            try:
                builtin(args)
            except (OSError, ValueError) as exc:
                reason = getattr(exc, "strerror", None) or exc
                self.error(f"{name}: {reason}")
        else:
            self.launch_process(tokens, background)

    # ------------------------------------------------------------------ #
    # Deliverable 4: command piping
    # ------------------------------------------------------------------ #
    def launch_pipeline(self, segments, background):
        """Run cmd1 | cmd2 | ... with real pipes; stages may be built-ins."""
        for seg in segments:
            if seg[0] == "":
                self.error("empty command in pipeline")
                return
            if any("\0" in word for word in seg):
                self.error("command contains a null byte")
                return
            if (getattr(self, "builtin_" + seg[0], None) is not None
                    and seg[0] not in PIPE_SAFE_BUILTINS):
                # A pipeline stage runs in a forked child, so anything
                # that changes shell or on-disk state would either be
                # lost or silently diverge from the parent's view.
                self.error(f"{seg[0]}: this built-in cannot be used in "
                           "a pipeline (it changes shell state)")
                return
            if (getattr(self, "builtin_" + seg[0], None) is None
                    and self.find_entry(seg[0]) is not None
                    and not self.check_access(seg[0], seg[0], "x")):
                return

        pids = []
        pgid = None
        prev_read = None
        for index, seg in enumerate(segments):
            last = index == len(segments) - 1
            if not last:
                read_end, write_end = os.pipe()
            pid = os.fork()
            if pid == 0:
                os.setpgid(0, pgid or 0)
                signal.signal(signal.SIGINT, signal.SIG_DFL)
                signal.signal(signal.SIGTSTP, signal.SIG_DFL)
                signal.signal(signal.SIGTTOU, signal.SIG_DFL)
                if prev_read is not None:
                    os.dup2(prev_read, 0)
                    os.close(prev_read)
                if not last:
                    os.close(read_end)
                    os.dup2(write_end, 1)
                    os.close(write_end)
                builtin = getattr(self, "builtin_" + seg[0], None)
                if builtin is not None:
                    # The child inherited the parent's Python-level read
                    # buffer, which on a non-tty already holds the rest
                    # of the shell's own script. Rebind stdin to the raw
                    # descriptor so a stage such as `cat` reads the pipe
                    # and nothing else.
                    try:
                        sys.stdin = io.TextIOWrapper(
                            io.FileIO(0, "r"), errors="replace")
                    except OSError:
                        pass
                    # Read-only built-ins run inside the forked child so
                    # their output feeds the pipe. Stateful ones are
                    # rejected before the fork (see PIPE_SAFE_BUILTINS).
                    try:
                        builtin(seg[1:])
                        sys.stdout.flush()
                        os._exit(0)
                    except Exception:
                        os._exit(1)
                try:
                    os.execvp(seg[0], seg)
                except FileNotFoundError:
                    sys.stderr.write(f"{PROMPT_NAME}: {seg[0]}: "
                                     "command not found\n")
                    os._exit(127)
                except (OSError, ValueError) as exc:
                    reason = getattr(exc, "strerror", None) or str(exc)
                    sys.stderr.write(f"{PROMPT_NAME}: {seg[0]}: "
                                     f"{reason.lower()}\n")
                    os._exit(126)
            if pgid is None:
                pgid = pid
            try:
                os.setpgid(pid, pgid)
            except (PermissionError, ProcessLookupError):
                pass
            pids.append(pid)
            if prev_read is not None:
                os.close(prev_read)
            if not last:
                os.close(write_end)
                prev_read = read_end

        command = " | ".join(" ".join(seg) for seg in segments)
        if background:
            job = self.add_job(pgid, command, pids=pids)
            print(f"[{job.job_id}] {pgid}")
        else:
            self.wait_foreground(pgid, command, pids=pids)

    # ------------------------------------------------------------------ #
    # Process management: fork / exec / wait
    # ------------------------------------------------------------------ #
    def launch_process(self, tokens, background):
        if (self.find_entry(tokens[0]) is not None
                and not self.check_access(tokens[0], tokens[0], "x")):
            return
        pid = os.fork()
        if pid == 0:
            # Child: new process group, default signal handlers, exec.
            os.setpgid(0, 0)
            signal.signal(signal.SIGINT, signal.SIG_DFL)
            signal.signal(signal.SIGTSTP, signal.SIG_DFL)
            signal.signal(signal.SIGTTOU, signal.SIG_DFL)
            try:
                os.execvp(tokens[0], tokens)
            except FileNotFoundError:
                sys.stderr.write(f"{PROMPT_NAME}: {tokens[0]}: "
                                 "command not found\n")
                os._exit(127)
            except (OSError, ValueError) as exc:
                # Covers permission errors, exec-format errors, oversized
                # argument lists, and empty argv — the child must never
                # unwind back into the shell's main loop.
                reason = getattr(exc, "strerror", None) or str(exc)
                sys.stderr.write(f"{PROMPT_NAME}: {tokens[0]}: "
                                 f"{reason.lower()}\n")
                os._exit(126)
        # Parent: register the child in its own process group as well
        # (both sides call setpgid to close the classic race).
        try:
            os.setpgid(pid, pid)
        except PermissionError:
            pass
        command = " ".join(tokens)
        if background:
            job = self.add_job(pid, command)
            print(f"[{job.job_id}] {pid}")
        else:
            self.wait_foreground(pid, command)

    def wait_foreground(self, pgid, command, job=None, pids=None):
        """Give the terminal to the foreground group, wait, take it back.

        Waits for every process in the group (a single command or all
        stages of a pipeline). If the group stops, it is re-registered
        in the job table — reusing the existing Job (and its id) when
        one is passed in, so a job keeps a stable id across fg / stop
        cycles.
        """
        pids = list(pids) if pids else [pgid]
        if self.interactive:
            os.tcsetpgrp(sys.stdin.fileno(), pgid)
        try:
            stopped = False
            remaining = []
            for pid in pids:
                try:
                    _, status = os.waitpid(pid, os.WUNTRACED)
                    if os.WIFSTOPPED(status):
                        stopped = True
                        remaining.append(pid)
                except ChildProcessError:
                    pass
            if stopped:
                if job is None:
                    job = self.add_job(pgid, command, pids=remaining)
                else:
                    job.pids = remaining
                    self.jobs[job.job_id] = job
                job.status = "Stopped"
                print(f"\n[{job.job_id}]  Stopped    {command}")
        finally:
            if self.interactive:
                os.tcsetpgrp(sys.stdin.fileno(), os.getpgrp())

    def add_job(self, pgid, command, pids=None):
        job = Job(self.next_job_id, pgid, command, pids=pids)
        self.jobs[job.job_id] = job
        self.next_job_id += 1
        return job

    def reap_background_jobs(self):
        """Collect finished background children without blocking."""
        for job in list(self.jobs.values()):
            if job.status == "Done":
                continue
            stopped = False
            for pid in list(job.pids):
                try:
                    reaped, status = os.waitpid(
                        pid, os.WNOHANG | os.WUNTRACED | os.WCONTINUED)
                except ChildProcessError:
                    job.pids.remove(pid)
                    continue
                if reaped == 0:
                    continue
                if os.WIFSTOPPED(status):
                    stopped = True
                elif os.WIFCONTINUED(status):
                    if job.status == "Stopped":
                        job.status = "Running"
                else:
                    job.pids.remove(pid)
            if not job.pids:
                job.status = "Done"
                print(f"[{job.job_id}]  Done       {job.command}")
                del self.jobs[job.job_id]
            elif stopped:
                job.status = "Stopped"

    def find_job(self, args, command_name):
        """Resolve a job id argument like '2' or '%2' to a Job."""
        if not args:
            candidates = [j for j in self.jobs.values() if j.status != "Done"]
            if not candidates:
                self.error(f"{command_name}: no current job")
                return None
            return candidates[-1]
        text = args[0].lstrip("%")
        if not text.isdecimal() or int(text) not in self.jobs:
            self.error(f"{command_name}: no such job: {args[0]}")
            return None
        return self.jobs[int(text)]

    # ------------------------------------------------------------------ #
    # Built-in commands
    # ------------------------------------------------------------------ #
    def builtin_cd(self, args):
        if len(args) > 1:
            self.error("cd: too many arguments")
            return
        target = os.path.expanduser(args[0] if args else "~")
        try:
            os.chdir(target)
        except FileNotFoundError:
            self.error(f"cd: no such directory: {target}")
        except NotADirectoryError:
            self.error(f"cd: not a directory: {target}")
        except OSError as exc:
            self.error(f"cd: {target}: {exc.strerror or exc}")

    def builtin_pwd(self, args):
        try:
            print(os.getcwd())
        except OSError:
            self.error("pwd: current directory no longer exists")

    def builtin_exit(self, args):
        running = [j for j in self.jobs.values() if j.status != "Done"]
        if running:
            print(f"{PROMPT_NAME}: warning: terminating "
                  f"{len(running)} background job(s)")
            self.terminate_pids([pid for job in running
                                 for pid in job.pids])
        self.exit_requested = True

    def builtin_echo(self, args):
        print(" ".join(args))

    def builtin_clear(self, args):
        sys.stdout.write("\033[H\033[2J")
        sys.stdout.flush()

    def builtin_ls(self, args):
        if len(args) > 1:
            self.error("ls: too many arguments")
            return
        target = args[0] if args else "."
        try:
            entries = sorted(e for e in os.listdir(target)
                             if not e.startswith("."))
        except FileNotFoundError:
            self.error(f"ls: no such directory: {target}")
            return
        except NotADirectoryError:
            print(target)
            return
        except OSError as exc:
            self.error(f"ls: {target}: {exc.strerror or exc}")
            return
        for entry in entries:
            suffix = "/" if os.path.isdir(os.path.join(target, entry)) else ""
            print(entry + suffix)

    def builtin_cat(self, args):
        if not args or args == ["-"]:
            # No operand (or "-"): copy standard input, so that
            # `echo hi | cat` behaves like the real cat.
            try:
                sys.stdout.write(sys.stdin.read())
            except (OSError, ValueError, UnicodeDecodeError):
                self.error("cat: could not read standard input")
            return
        for path in args:
            if not self.check_access("cat", path, "r"):
                continue
            try:
                mode = os.stat(path).st_mode
            except FileNotFoundError:
                self.error(f"cat: no such file: {path}")
                continue
            except OSError as exc:
                self.error(f"cat: {path}: {exc.strerror or exc}")
                continue
            if stat.S_ISDIR(mode):
                self.error(f"cat: {path} is a directory")
                continue
            if not stat.S_ISREG(mode):
                # Opening a FIFO or device here could block the whole
                # shell (builtins run in the shell process itself).
                self.error(f"cat: {path}: not a regular file")
                continue
            try:
                with open(path, "r", errors="replace") as handle:
                    sys.stdout.write(handle.read())
            except OSError as exc:
                self.error(f"cat: {path}: {exc.strerror or exc}")

    def builtin_mkdir(self, args):
        if not args:
            self.error("mkdir: missing directory name")
            return
        for path in args:
            try:
                os.mkdir(path)
            except FileExistsError:
                self.error(f"mkdir: already exists: {path}")
            except OSError as exc:
                self.error(f"mkdir: {path}: {exc.strerror or exc}")

    def builtin_rmdir(self, args):
        if not args:
            self.error("rmdir: missing directory name")
            return
        for path in args:
            if not self.check_access("rmdir", path, "w"):
                continue
            try:
                os.rmdir(path)
            except FileNotFoundError:
                self.error(f"rmdir: no such directory: {path}")
            except OSError as exc:
                if exc.errno in (errno.ENOTEMPTY, errno.EEXIST):
                    self.error(f"rmdir: directory not empty: {path}")
                else:
                    self.error(f"rmdir: {path}: {exc.strerror or exc}")

    def builtin_rm(self, args):
        if not args:
            self.error("rm: missing file name")
            return
        for path in args:
            if not self.check_access("rm", path, "w"):
                continue
            try:
                key = self.perm_key(path)
                os.remove(path)
                if key in self.perms:
                    updated = self.update_store(
                        self.perms_path, lambda data: data.pop(key, None),
                        ("owner", "owner_perms", "other_perms"))
                    if updated is not None:
                        self.perms = updated
            except FileNotFoundError:
                self.error(f"rm: no such file: {path}")
            except IsADirectoryError:
                self.error(f"rm: {path} is a directory (use rmdir)")
            except OSError as exc:
                self.error(f"rm: {path}: {exc.strerror or exc}")

    def builtin_touch(self, args):
        if not args:
            self.error("touch: missing file name")
            return
        for path in args:
            if not self.check_access("touch", path, "w"):
                continue
            try:
                if (os.path.exists(path)
                        and not stat.S_ISREG(os.stat(path).st_mode)):
                    # Opening a FIFO for writing blocks until a reader
                    # appears, and built-ins run in the shell process.
                    self.error(f"touch: {path}: not a regular file")
                    continue
                with open(path, "a"):
                    os.utime(path, None)
            except OSError as exc:
                self.error(f"touch: {path}: {exc.strerror}")

    def builtin_kill(self, args):
        """kill [-STOP|-TERM|-9|...] pid|%job  (default signal: TERM)."""
        sig = signal.SIGTERM
        if args and args[0].startswith("-") and len(args[0]) > 1:
            name = args[0][1:].upper()
            if name.isdecimal():                 # numeric form, e.g. -9
                try:
                    sig = signal.Signals(int(name)) if int(name) else 0
                except ValueError:
                    self.error(f"kill: unknown signal: {args[0]}")
                    return
            else:                                # named form, e.g. -STOP
                candidate = getattr(signal, "SIG" + name, None)
                if not isinstance(candidate, signal.Signals):
                    self.error(f"kill: unknown signal: {args[0]}")
                    return
                sig = candidate
            args = args[1:]
        if len(args) != 1:
            self.error("usage: kill [-SIGNAL] pid|%job")
            return
        target = args[0]
        if target.startswith("%"):
            job = self.find_job([target], "kill")
            if job is None:
                return
            # A pipeline is several processes but one job: signal them all.
            delivered = []
            for member in list(job.pids):
                try:
                    os.kill(member, sig)
                    delivered.append(member)
                except ProcessLookupError:
                    continue
                except PermissionError:
                    self.error(f"kill: permission denied: {member}")
                    return
            if not delivered:
                self.error(f"kill: job {job.job_id} has no live processes")
                return
            signame = (sig.name if isinstance(sig, signal.Signals)
                       else "signal 0")
            print(f"sent {signame} to process"
                  f"{'es' if len(delivered) > 1 else ''} "
                  f"{', '.join(str(p) for p in delivered)}")
            if sig in (signal.SIGSTOP, signal.SIGTSTP):
                job.status = "Stopped"
            elif sig == signal.SIGCONT:
                job.status = "Running"
            return
        elif target.isdecimal() and 0 < int(target) < 2 ** 31:
            pid = int(target)
        else:
            self.error(f"kill: invalid pid or job: {target}")
            return
        signame = sig.name if isinstance(sig, signal.Signals) else "signal 0"
        job = self.jobs.get(int(target.lstrip("%"))) \
            if target.startswith("%") else None
        try:
            os.kill(pid, sig)
            print(f"sent {signame} to process {pid}")
            if job is not None:
                if sig in (signal.SIGSTOP, signal.SIGTSTP):
                    job.status = "Stopped"
                elif sig == signal.SIGCONT:
                    job.status = "Running"
        except ProcessLookupError:
            self.error(f"kill: no such process: {pid}")
        except PermissionError:
            self.error(f"kill: permission denied: {pid}")

    # ------------------------------------------------------------------ #
    # Job control built-ins
    # ------------------------------------------------------------------ #
    def builtin_jobs(self, args):
        self.reap_background_jobs()
        active = [j for j in self.jobs.values() if j.status != "Done"]
        if not active:
            print("no active jobs")
            return
        for job in active:
            print(f"[{job.job_id}]  {job.status:<9}  pid {job.pgid}  "
                  f"{job.command}")

    def builtin_fg(self, args):
        self.reap_background_jobs()
        job = self.find_job(args, "fg")
        if job is None:
            return
        print(job.command)
        try:
            os.killpg(job.pgid, signal.SIGCONT)
        except ProcessLookupError:
            pass
        job.status = "Running"
        del self.jobs[job.job_id]
        # Pass the job along so it keeps the same id if stopped again.
        self.wait_foreground(job.pgid, job.command, job=job,
                             pids=job.pids)

    def builtin_bg(self, args):
        self.reap_background_jobs()
        job = self.find_job(args, "bg")
        if job is None:
            return
        if job.status != "Stopped":
            self.error(f"bg: job {job.job_id} is already running")
            return
        try:
            os.killpg(job.pgid, signal.SIGCONT)
        except ProcessLookupError:
            pass
        job.status = "Running"
        print(f"[{job.job_id}]  {job.command} &")

    # ------------------------------------------------------------------ #
    # Deliverable 2: internal process scheduling (simulated processes)
    # ------------------------------------------------------------------ #
    def builtin_addproc(self, args):
        """addproc NAME BURST [PRIORITY] [ARRIVAL]"""
        if not 2 <= len(args) <= 4:
            self.error("usage: addproc NAME BURST [PRIORITY] [ARRIVAL]")
            return
        name = args[0]
        try:
            burst = float(args[1])
            priority = int(args[2]) if len(args) > 2 else 5
            arrival = float(args[3]) if len(args) > 3 else 0.0
        except ValueError:
            self.error("addproc: BURST/ARRIVAL must be numbers and "
                       "PRIORITY an integer")
            return
        if not (math.isfinite(burst) and math.isfinite(arrival)):
            self.error("addproc: BURST and ARRIVAL must be finite numbers")
            return
        if burst <= 0 or arrival < 0 or priority < 1:
            self.error("addproc: BURST must be > 0, ARRIVAL >= 0, "
                       "PRIORITY >= 1")
            return
        if burst > 100000 or arrival > 100000:
            self.error("addproc: BURST/ARRIVAL too large "
                       "(max 100000 seconds)")
            return
        proc = SimProcess(self.next_sim_pid, name, burst, priority, arrival)
        self.next_sim_pid += 1
        self.sim_procs.append(proc)
        print(f"added simulated process pid {proc.pid}: {name} "
              f"(burst {burst:g}s, priority {priority}, "
              f"arrival t={arrival:g}s)")

    def builtin_procs(self, args):
        if not self.sim_procs:
            print("no simulated processes queued (use addproc)")
            return
        print("PID  NAME          BURST  PRIORITY  ARRIVAL")
        for p in self.sim_procs:
            print(f"{p.pid:<4} {p.name:<13} {p.burst:<6g} "
                  f"{p.priority:<9} t={p.arrival:g}s")

    def builtin_timescale(self, args):
        if not args:
            print(f"timescale is {self.timescale:g} "
                  "(real seconds per simulated second)")
            return
        try:
            factor = float(args[0])
        except ValueError:
            self.error("timescale: FACTOR must be a number")
            return
        if not 0 <= factor <= 10:
            self.error("timescale: FACTOR must be between 0 and 10")
            return
        self.timescale = factor
        print(f"timescale set to {factor:g}")

    def sim_sleep(self, seconds):
        if seconds > 0 and self.timescale > 0:
            try:
                time.sleep(min(seconds * self.timescale, 1_000_000))
            except (OverflowError, OSError):
                pass

    def take_sim_procs(self, command_name):
        """Consume the queued processes, sorted by arrival then pid."""
        if not self.sim_procs:
            self.error(f"{command_name}: no processes queued "
                       "(use addproc first)")
            return None
        procs = sorted(self.sim_procs, key=lambda p: (p.arrival, p.pid))
        self.sim_procs = []
        return procs

    def builtin_runrr(self, args):
        """runrr [QUANTUM] - Round-Robin with configurable time slice."""
        quantum = 2.0
        if args:
            try:
                quantum = float(args[0])
            except ValueError:
                self.error("runrr: QUANTUM must be a number")
                return
        if not math.isfinite(quantum) or quantum <= 0:
            self.error("runrr: QUANTUM must be a positive finite number")
            return
        procs = self.take_sim_procs("runrr")
        if procs is None:
            return
        total = sum(p.burst for p in procs)
        if total / quantum > 100000:
            # Below this ratio the float clock stops advancing and the
            # run would never terminate.
            self.error(f"runrr: quantum {quantum:g}s is too small for a "
                       f"total burst of {total:g}s (it would need more "
                       "than 100000 slices)")
            self.sim_procs = procs
            return
        print(f"Round-Robin scheduling of {len(procs)} processes "
              f"with time slice (quantum) = {quantum:g}s")
        if self._run_interruptible(self._rr_loop, procs, quantum):
            self.print_metrics(procs, f"Round-Robin (quantum {quantum:g}s)")

    def _run_interruptible(self, loop_fn, *fn_args):
        """Run a scheduler loop with Ctrl-C able to abort it.

        The interactive shell normally ignores SIGINT, but the schedulers
        run inside the shell process itself, so a long simulation would
        otherwise be unbreakable. Restore the default KeyboardInterrupt
        behavior for the duration of the run.
        """
        previous = None
        if self.interactive:
            previous = signal.signal(signal.SIGINT,
                                     signal.default_int_handler)
        try:
            loop_fn(*fn_args)
            return True
        except KeyboardInterrupt:
            print("\nschedule interrupted; remaining work discarded")
            return False
        finally:
            if previous is not None:
                signal.signal(signal.SIGINT, previous)

    def _rr_loop(self, procs, quantum):
        clock = 0.0
        queue = deque()
        upcoming = deque(procs)

        def admit(now):
            while upcoming and upcoming[0].arrival <= now + 1e-9:
                p = upcoming.popleft()
                queue.append(p)
                print(f"[t={p.arrival:g}s] {p.name} (pid {p.pid}) "
                      f"arrived and joined the ready queue")

        admit(clock)
        while queue or upcoming:
            if not queue:
                clock = upcoming[0].arrival
                admit(clock)
                continue
            p = queue.popleft()
            if p.first_run is None:
                p.first_run = clock
            slice_len = min(quantum, p.remaining)
            if clock + slice_len == clock and p.remaining > 0:
                # The slice is too small to move a float clock of this
                # magnitude: stop rather than spin forever.
                p.completion = clock
                p.remaining = 0.0
                print(f"[t={clock:g}s] {p.name} (pid {p.pid}) stopped: "
                      "time slice is below the clock's resolution")
                continue
            print(f"[t={clock:g}s] running {p.name} (pid {p.pid}) "
                  f"for {slice_len:g}s of remaining {p.remaining:g}s")
            self.sim_sleep(slice_len)
            clock += slice_len
            p.remaining -= slice_len
            admit(clock)
            if p.remaining <= 1e-9:
                p.remaining = 0.0
                p.completion = clock
                print(f"[t={clock:g}s] {p.name} (pid {p.pid}) completed")
            else:
                print(f"[t={clock:g}s] time slice expired; "
                      f"{p.name} back to the ready queue")
                queue.append(p)

    def builtin_runpriority(self, args):
        """runpriority - preemptive priority scheduling (1 = highest)."""
        procs = self.take_sim_procs("runpriority")
        if procs is None:
            return
        print(f"Priority scheduling of {len(procs)} processes "
              "(lower number = higher priority; equal priorities FCFS; "
              "arrivals preempt lower-priority work)")
        if self._run_interruptible(self._priority_loop, procs):
            self.print_metrics(procs, "Preemptive priority")

    def _priority_loop(self, procs):
        clock = 0.0
        heap = []           # (priority, fcfs_seq, SimProcess)
        seq = 0
        upcoming = deque(procs)

        def admit(now):
            nonlocal seq
            while upcoming and upcoming[0].arrival <= now + 1e-9:
                p = upcoming.popleft()
                heapq.heappush(heap, (p.priority, seq, p))
                seq += 1
                print(f"[t={p.arrival:g}s] {p.name} (pid {p.pid}, "
                      f"priority {p.priority}) arrived")

        admit(clock)
        while heap or upcoming:
            if not heap:
                clock = upcoming[0].arrival
                admit(clock)
                continue
            _, _, p = heapq.heappop(heap)
            if p.first_run is None:
                p.first_run = clock
            preempted = False
            while p.remaining > 1e-9:
                next_arrival = upcoming[0].arrival if upcoming else None
                if next_arrival is None:
                    run_len = p.remaining
                else:
                    run_len = min(p.remaining,
                                  max(0.0, next_arrival - clock))
                if run_len > 0:
                    print(f"[t={clock:g}s] running {p.name} (pid {p.pid}, "
                          f"priority {p.priority}) — remaining "
                          f"{p.remaining:g}s")
                    self.sim_sleep(run_len)
                    clock += run_len
                    p.remaining -= run_len
                if p.remaining <= 1e-9:
                    break
                admit(clock)
                if heap and heap[0][0] < p.priority:
                    challenger = heap[0][2]
                    print(f"[t={clock:g}s] PREEMPTION: {challenger.name} "
                          f"(priority {challenger.priority}) preempts "
                          f"{p.name} (priority {p.priority})")
                    heapq.heappush(heap, (p.priority, seq, p))
                    seq += 1
                    preempted = True
                    break
            if not preempted:
                p.remaining = 0.0
                p.completion = clock
                print(f"[t={clock:g}s] {p.name} (pid {p.pid}) completed")

    def print_metrics(self, procs, label):
        print(f"\n{label} — performance metrics (seconds):")
        print("PID  NAME          BURST  PRI  ARRIVE  COMPLETE  "
              "TURNAROUND  WAITING  RESPONSE")
        totals = [0.0, 0.0, 0.0]
        for p in sorted(procs, key=lambda x: x.pid):
            turnaround = p.completion - p.arrival
            waiting = turnaround - p.burst
            response = p.first_run - p.arrival
            totals[0] += turnaround
            totals[1] += waiting
            totals[2] += response
            print(f"{p.pid:<4} {p.name:<13} {p.burst:<6g} "
                  f"{p.priority:<4} {p.arrival:<7g} {p.completion:<9g} "
                  f"{turnaround:<11g} {waiting:<8g} {response:g}")
        n = len(procs)
        print(f"averages: turnaround {totals[0] / n:.2f}  "
              f"waiting {totals[1] / n:.2f}  "
              f"response {totals[2] / n:.2f}\n")

    # ------------------------------------------------------------------ #
    # Deliverable 4: user authentication and roles
    # ------------------------------------------------------------------ #
    def load_json(self, path, required=(), previous=None):
        """Load a dict store, dropping entries with the wrong shape.

        A missing file means "empty store", but an unreadable or
        malformed one must NOT be treated as empty: doing so would let
        the next save overwrite a good file with nothing.
        """
        if not os.path.exists(path):
            return {}
        try:
            if not stat.S_ISREG(os.stat(path).st_mode):
                # Opening a FIFO or device here would block the shell
                # forever, and the shell ignores SIGINT while running.
                self.error(f"{os.path.basename(path)} is not a regular "
                           "file; it will not be read or overwritten")
                return None
        except OSError:
            return None
        try:
            with open(path) as handle:
                data = json.load(handle)
        except RecursionError:
            self.error(f"{os.path.basename(path)} is too deeply nested "
                       "to read; keeping the previous contents")
            return previous if previous is not None else None
        except (OSError, ValueError):
            if previous is not None:
                return previous
            self.error(f"could not read {os.path.basename(path)}; "
                       "it will not be overwritten")
            return None
        if not isinstance(data, dict):
            return {}
        clean, dropped = {}, 0
        for key, value in data.items():
            if (isinstance(value, dict)
                    and all(isinstance(value.get(f), str)
                            for f in required)):
                clean[key] = value
            else:
                dropped += 1
        if dropped:
            self.error(f"ignored {dropped} malformed entr"
                       f"{'y' if dropped == 1 else 'ies'} in "
                       f"{os.path.basename(path)}")
            if not clean:
                # Every record was unusable: the file holds something,
                # so treating it as empty would let the next write
                # replace real data with fresh default credentials.
                self.error(f"{os.path.basename(path)} contains no usable "
                           "entries; it will not be overwritten")
                return None
        return clean

    def update_store(self, path, mutate, required):
        """Serialized read-modify-write of a JSON store.

        Two shells editing the same store are a classic critical
        section: without mutual exclusion both read the old contents and
        the second write silently discards the first one's change. An
        advisory file lock makes the read, the change, and the write one
        atomic unit, exactly as the mutex in the synchronization
        demonstration protects the shared buffer.

        Returns the new contents on success, or None if the store could
        not be read or written, so callers never report a change that
        did not reach the disk.
        """
        def attempt():
            data = self.load_json(path, required)
            if data is None:
                self.error(f"{os.path.basename(path)} is unreadable; "
                           "the change was not saved")
                return None
            mutate(data)
            return data if self.save_json(path, data) else None

        lock_path = path + ".lock"
        try:
            if (os.path.exists(lock_path)
                    and not stat.S_ISREG(os.stat(lock_path).st_mode)):
                # A FIFO here would block forever; fall back to an
                # unlocked update rather than freezing the shell.
                self.error(f"{os.path.basename(lock_path)} is not a "
                           "regular file; proceeding without a lock")
                return attempt()
            lock = open(lock_path, "w")
        except OSError:
            return attempt()
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            return attempt()
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()

    def save_json(self, path, data):
        """Write atomically; returns True only if the file was replaced."""
        temp = f"{path}.{os.getpid()}.tmp"
        try:
            with open(temp, "w") as handle:
                json.dump(data, handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
            return True
        except OSError as exc:
            self.error(f"could not save {path}: {exc.strerror}")
            try:
                os.unlink(temp)
            except OSError:
                pass
            return False

    @staticmethod
    def hash_password(password, salt):
        return hashlib.sha256((salt + password).encode()).hexdigest()

    def create_user(self, name, password, role):
        salt = os.urandom(8).hex()
        record = {"salt": salt,
                  "hash": self.hash_password(password, salt),
                  "role": role}
        updated = self.update_store(
            self.users_path, lambda data: data.update({name: record}),
            ("salt", "hash", "role"))
        if updated is None:
            return False
        self.users = updated
        return True

    def read_line(self, prompt, mask=False):
        """Read one input line for the login dialog."""
        sys.stdout.write(prompt)
        sys.stdout.flush()
        if self.interactive and mask:
            try:
                return getpass.getpass("")
            except (EOFError, KeyboardInterrupt):
                return None
        raw = sys.stdin.buffer.readline()
        if raw == b"":
            return None
        line = raw.decode(errors="replace").rstrip("\n")
        if not self.interactive:
            sys.stdout.write(("********" if mask else line) + "\n")
            sys.stdout.flush()
        return line

    def authenticate(self):
        """Login dialog; three attempts. Returns True on success."""
        print(f"{PROMPT_NAME} login required")
        for _ in range(3):
            username = self.read_line("username: ")
            if username is None:
                return False
            password = self.read_line("password: ", mask=True)
            if password is None:
                return False
            self.refresh_stores()
            record = self.users.get(username.strip())
            if (record is not None and
                    self.hash_password(password, record["salt"])
                    == record["hash"]):
                self.current_user = username.strip()
                self.current_role = record["role"]
                print(f"welcome, {self.current_user} "
                      f"({self.current_role} user)")
                return True
            print("login incorrect")
        print("too many failed attempts")
        return False

    def builtin_whoami(self, args):
        print(f"{self.current_user} ({self.current_role} user)")

    def terminate_pids(self, pids):
        """Stop, terminate, and reap the given children for good.

        A stopped child ignores SIGTERM until continued, and a child may
        ignore it outright, so continue first, then escalate to SIGKILL
        and always reap: an unreaped child would stay a zombie for the
        rest of the shell's life.
        """
        ended = 0
        for pid in pids:
            for sig in (signal.SIGCONT, signal.SIGTERM):
                try:
                    os.kill(pid, sig)
                except (ProcessLookupError, PermissionError):
                    break
        deadline = time.monotonic() + 1.0
        pending = list(pids)
        while pending and time.monotonic() < deadline:
            for pid in list(pending):
                try:
                    reaped, _ = os.waitpid(pid, os.WNOHANG)
                except ChildProcessError:
                    pending.remove(pid)
                    continue
                if reaped:
                    pending.remove(pid)
                    ended += 1
            if pending:
                time.sleep(0.02)
        for pid in pending:
            try:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
                ended += 1
            except (ProcessLookupError, PermissionError, ChildProcessError):
                pass
        return ended

    def builtin_logout(self, args):
        # A session's processes and simulated state belong to the user
        # who created them; the next user must not inherit them.
        running = [j for j in self.jobs.values() if j.status != "Done"]
        pids = [pid for job in running for pid in job.pids]
        ended = self.terminate_pids(pids)
        if running:
            print(f"terminated {ended} process(es) of "
                  f"{len(running)} background job(s) owned by "
                  f"{self.current_user}")
        self.jobs.clear()
        self.next_job_id = 1
        self.sim_procs = []
        self.next_sim_pid = 1
        self.memory = None
        self.timescale = 1.0
        print(f"user {self.current_user} logged out")
        self.current_user = None
        self.current_role = None

    def builtin_adduser(self, args):
        """adduser NAME PASSWORD admin|standard  (admin only)."""
        if self.current_role != "admin":
            self.error("adduser: permission denied "
                       "(only the admin may add users)")
            return
        if len(args) != 3 or args[2] not in ("admin", "standard"):
            self.error("usage: adduser NAME PASSWORD admin|standard")
            return
        if not args[0] or any(c.isspace() for c in args[0]):
            self.error("adduser: NAME must be non-empty and contain "
                       "no spaces")
            return
        if not args[1]:
            self.error("adduser: PASSWORD must not be empty")
            return
        if args[0] in self.users:
            self.error(f"adduser: user already exists: {args[0]}")
            return
        if not self.create_user(args[0], args[1], args[2]):
            self.error(f"adduser: {args[0]} was NOT created")
            return
        print(f"added {args[2]} user {args[0]}")

    # ------------------------------------------------------------------ #
    # Deliverable 4: simulated file permissions
    # ------------------------------------------------------------------ #
    @staticmethod
    def perm_key(path):
        """Canonical permission key: resolves symlinks so a second name
        for the same file cannot escape its protection."""
        try:
            return os.path.realpath(path)
        except (ValueError, OSError):
            # e.g. an embedded null byte: no real file can match it.
            return "\0invalid\0" + str(path)

    def protected_store(self, path):
        """The permission/user stores are admin-only by construction.

        Compared by inode as well as by resolved path, so a hard link —
        a second directory entry for the same file — cannot be used to
        read or re-own the credential database.
        """
        stores = (self.users_path, self.perms_path)
        if self.perm_key(path) in (self.perm_key(s) for s in stores):
            return True
        try:
            info = os.stat(path)
        except OSError:
            return False
        for store in stores:
            try:
                other = os.stat(store)
            except OSError:
                continue
            if (other.st_dev, other.st_ino) == (info.st_dev, info.st_ino):
                return True
        return False

    def find_entry(self, path):
        """Permission record for path, if any.

        Falls back to an inode comparison so that a hard link — a second
        directory entry for the same file, which no path canonicalization
        can detect — inherits the protection of the file it points at.
        """
        entry = self.perms.get(self.perm_key(path))
        if entry is not None:
            return entry
        try:
            info = os.stat(path)
        except OSError:
            return None
        for tracked, record in self.perms.items():
            try:
                other = os.stat(tracked)
            except OSError:
                continue
            if (other.st_dev, other.st_ino) == (info.st_dev, info.st_ino):
                return record
        return None

    def allowed(self, path, need):
        """Does the current user have r/w/x access to path?"""
        if self.current_role == "admin":
            return True
        if self.protected_store(path):
            return False          # only the admin may touch the stores
        entry = self.find_entry(path)
        if entry is None:
            return True          # untracked files are unrestricted
        if entry["owner"] == self.current_user:
            return need in entry["owner_perms"]
        return need in entry["other_perms"]

    def check_access(self, command_name, path, need):
        if self.allowed(path, need):
            return True
        entry = self.find_entry(path) or {}
        kind = ("protected shell database" if self.protected_store(path)
                else "system file" if entry.get("system") else "file")
        self.error(f"{command_name}: permission denied: {path} "
                   f"({kind}; user {self.current_user} lacks "
                   f"'{need}' access)")
        return False

    def builtin_setperm(self, args):
        """setperm FILE OWNER_PERMS OTHER_PERMS  (e.g. rw- r--)."""
        if len(args) != 3:
            self.error("usage: setperm FILE OWNER_PERMS OTHER_PERMS "
                       "(e.g. setperm notes.txt rw- r--)")
            return
        path, owner_perms, other_perms = args
        for triplet in (owner_perms, other_perms):
            if len(triplet) != 3 or any(c not in "rwx-" for c in triplet):
                self.error("setperm: each perms field is three of "
                           "r, w, x, or - (e.g. rw-, r--, ---)")
                return
        if not os.path.exists(path):
            self.error(f"setperm: no such file: {path}")
            return
        if os.path.isdir(path):
            self.error("setperm: directories are not supported by the "
                       "simulated permission layer (files only)")
            return
        if self.protected_store(path):
            self.error("setperm: the shell's own credential and "
                       "permission databases cannot be re-owned")
            return
        absolute = self.perm_key(path)
        entry = self.perms.get(absolute)
        if (entry is not None and entry["owner"] != self.current_user
                and self.current_role != "admin"):
            self.error(f"setperm: permission denied "
                       f"(only {entry['owner']} or the admin may "
                       f"change {path})")
            return
        owner = entry["owner"] if entry else self.current_user
        record = {"owner": owner,
                  "owner_perms": owner_perms,
                  "other_perms": other_perms,
                  "system": bool(entry and entry.get("system"))}
        updated = self.update_store(
            self.perms_path, lambda data: data.update({absolute: record}),
            ("owner", "owner_perms", "other_perms"))
        if updated is None:
            self.error(f"setperm: permissions for {path} were NOT saved")
            return
        self.perms = updated
        print(f"{path}: owner={owner} owner_perms={owner_perms} "
              f"other_perms={other_perms}")

    def builtin_perms(self, args):
        if not self.perms:
            print("no files under simulated permission control")
            return
        print("OWNER-PERMS  OTHER-PERMS  OWNER      FILE")
        for absolute, entry in sorted(self.perms.items()):
            flag = "  [system]" if entry.get("system") else ""
            print(f"{entry['owner_perms']:<12} {entry['other_perms']:<12} "
                  f"{entry['owner']:<10} {absolute}{flag}")

    def builtin_mksys(self, args):
        """mksys FILE - mark a file as a protected system file (admin)."""
        if self.current_role != "admin":
            self.error("mksys: permission denied "
                       "(only the admin may mark system files)")
            return
        if len(args) != 1:
            self.error("usage: mksys FILE")
            return
        if not os.path.exists(args[0]):
            self.error(f"mksys: no such file: {args[0]}")
            return
        if os.path.isdir(args[0]):
            self.error("mksys: directories are not supported by the "
                       "simulated permission layer (files only)")
            return
        if self.protected_store(args[0]):
            self.error("mksys: the shell's own databases are already "
                       "protected")
            return
        absolute = self.perm_key(args[0])
        record = {"owner": "admin", "owner_perms": "rwx",
                  "other_perms": "r--", "system": True}
        updated = self.update_store(
            self.perms_path, lambda data: data.update({absolute: record}),
            ("owner", "owner_perms", "other_perms"))
        if updated is None:
            self.error(f"mksys: {args[0]} was NOT marked")
            return
        self.perms = updated
        print(f"{args[0]} is now a system file "
              "(admin: rwx, others: read-only)")

    # ------------------------------------------------------------------ #
    # Deliverable 3: memory management (paging + page replacement)
    # ------------------------------------------------------------------ #
    def builtin_meminit(self, args):
        """meminit FRAMES [fifo|lru] - initialize the paging system."""
        if not 1 <= len(args) <= 2:
            self.error("usage: meminit FRAMES [fifo|lru]")
            return
        if not args[0].isdecimal() or not 1 <= int(args[0]) <= 64:
            self.error("meminit: FRAMES must be an integer from 1 to 64")
            return
        algorithm = args[1].lower() if len(args) > 1 else "fifo"
        if algorithm not in ("fifo", "lru"):
            self.error("meminit: algorithm must be fifo or lru")
            return
        self.memory = PagingMemory(int(args[0]), algorithm)
        print(f"initialized memory: {args[0]} frames, "
              f"{algorithm.upper()} page replacement")

    def require_memory(self, command_name):
        if self.memory is None:
            self.error(f"{command_name}: no memory initialized "
                       "(use meminit first)")
            return False
        return True

    def builtin_memaccess(self, args):
        """memaccess PROC PAGE - process references a virtual page."""
        if len(args) != 2 or not args[1].isdecimal():
            self.error("usage: memaccess PROC PAGE  (PAGE a non-negative "
                       "integer)")
            return
        if not self.require_memory("memaccess"):
            return
        print(self.memory.access(args[0], int(args[1])))

    def builtin_memfree(self, args):
        """memfree PROC - deallocate all frames owned by a process."""
        if len(args) != 1:
            self.error("usage: memfree PROC")
            return
        if not self.require_memory("memfree"):
            return
        freed = self.memory.free(args[0])
        if freed:
            pages = ", ".join(str(p) for p in sorted(freed))
            print(f"freed {len(freed)} frame(s) of {args[0]} "
                  f"(pages {pages})")
        else:
            print(f"{args[0]} holds no frames")

    def builtin_memstats(self, args):
        if not self.require_memory("memstats"):
            return
        mem = self.memory
        used = sum(1 for f in mem.frames if f is not None)
        print(f"memory: {mem.frame_count} frames, {used} used, "
              f"{mem.frame_count - used} free, "
              f"{mem.algorithm.upper()} replacement")
        for i, entry in enumerate(mem.frames):
            if entry is None:
                print(f"  frame {i}: free")
            else:
                print(f"  frame {i}: {entry[0]} page {entry[1]} "
                      f"(loaded at access #{mem.load_seq[i]}, "
                      f"last used at access #{mem.used_seq[i]})")
        procs = sorted(set(mem.accesses) | set(mem.faults))
        for proc in procs:
            resident = sum(1 for f in mem.frames
                           if f is not None and f[0] == proc)
            print(f"  {proc}: {resident} page(s) resident, "
                  f"{mem.faults.get(proc, 0)} fault(s) in "
                  f"{mem.accesses.get(proc, 0)} accesses")
        print(f"  totals: {sum(mem.faults.values())} fault(s), "
              f"{mem.total_evictions} eviction(s), "
              f"{sum(mem.accesses.values())} access(es)")

    # ------------------------------------------------------------------ #
    # Deliverable 3: process synchronization (mutexes and semaphores)
    # ------------------------------------------------------------------ #
    def join_threads(self, threads):
        """Start and join worker threads, surviving Ctrl-C cleanly."""
        for t in threads:
            t.start()
        interrupted = False
        for t in threads:
            while t.is_alive():
                try:
                    t.join(0.2)
                except KeyboardInterrupt:
                    # Repeated Ctrl-C must not abandon the workers: the
                    # demonstration owns shared state, so it always runs
                    # to completion before the prompt returns.
                    if not interrupted:
                        interrupted = True
                        print("\ninterrupted; waiting for worker "
                              "threads to finish")

    def builtin_racedemo(self, args):
        """racedemo safe|unsafe [ITERATIONS] - shared-counter race demo."""
        if not args or args[0] not in ("safe", "unsafe"):
            self.error("usage: racedemo safe|unsafe [ITERATIONS]")
            return
        iterations = 100000
        if len(args) > 1:
            if not args[1].isdecimal() or not 1 <= int(args[1]) <= 2000000:
                self.error("racedemo: ITERATIONS must be an integer "
                           "from 1 to 2000000")
                return
            iterations = int(args[1])
        safe = args[0] == "safe"
        counter = {"value": 0}
        mutex = threading.Lock()
        # Both threads wait here so they genuinely run concurrently;
        # without this, the first thread can finish before the second
        # one has even started, hiding the race.
        barrier = threading.Barrier(2)

        def compute_next(value):
            # Stands in for the "modify" work between read and write.
            # Being a function call, it is also a point where the
            # interpreter may switch threads mid-sequence.
            return value + 1

        def worker():
            barrier.wait()
            for _ in range(iterations):
                if safe:
                    with mutex:
                        counter["value"] = compute_next(counter["value"])
                else:
                    # Deliberately unprotected read-modify-write: a
                    # thread switch between the read and the write makes
                    # this thread overwrite the other's update.
                    value = counter["value"]
                    value = compute_next(value)
                    counter["value"] = value

        old_interval = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)   # force frequent thread switches
        threads = [threading.Thread(target=worker) for _ in range(2)]
        try:
            self.join_threads(threads)
        finally:
            sys.setswitchinterval(old_interval)
        expected = 2 * iterations
        actual = counter["value"]
        mode = "WITH mutex" if safe else "WITHOUT mutex"
        print(f"two threads incremented a shared counter "
              f"{iterations} times each {mode}")
        print(f"expected final value: {expected}")
        print(f"actual final value:   {actual}")
        if actual == expected and safe:
            print("no updates lost: mutual exclusion preserved "
                  "every read-modify-write")
        elif actual == expected:
            print("no updates lost this run — the race happened not to "
                  "manifest; try more iterations")
        else:
            print(f"RACE CONDITION: {expected - actual} updates lost "
                  "to interleaved read-modify-write sequences")

    def builtin_prodcons(self, args):
        """prodcons [PRODUCERS CONSUMERS ITEMS BUFFER] - bounded buffer."""
        defaults = [2, 2, 5, 3]
        values = defaults[:]
        for i, arg in enumerate(args[:4]):
            if not arg.isdecimal() or not 1 <= int(arg) <= 50:
                self.error("prodcons: arguments must be integers "
                           "from 1 to 50")
                return
            values[i] = int(arg)
        n_prod, n_cons, items_each, buf_size = values
        total = n_prod * items_each
        if total % n_cons != 0:
            self.error(f"prodcons: total items ({total}) must divide "
                       f"evenly among {n_cons} consumer(s)")
            return
        print(f"Producer-Consumer: {n_prod} producer(s) x {items_each} "
              f"item(s), {n_cons} consumer(s), buffer size {buf_size}")
        buffer = deque()
        mutex = threading.Lock()          # protects the buffer
        empty = threading.Semaphore(buf_size)   # free slots
        full = threading.Semaphore(0)           # filled slots
        print_lock = threading.Lock()
        produced, consumed = [], []
        overlap = {"in_critical": 0, "max_in_critical": 0}

        def log(message):
            with print_lock:
                print(message)

        def enter_critical():
            overlap["in_critical"] += 1
            overlap["max_in_critical"] = max(overlap["max_in_critical"],
                                             overlap["in_critical"])

        def producer(pid):
            for seq in range(1, items_each + 1):
                item = f"P{pid}-item{seq}"
                empty.acquire()           # wait for a free slot
                with mutex:
                    enter_critical()
                    buffer.append(item)
                    produced.append(item)
                    occupancy = len(buffer)
                    overlap["in_critical"] -= 1
                full.release()            # signal a filled slot
                log(f"producer {pid} put {item:<10} "
                    f"(buffer {occupancy}/{buf_size})")
                time.sleep(0.01)

        def consumer(cid, count):
            for _ in range(count):
                full.acquire()            # wait for a filled slot
                with mutex:
                    enter_critical()
                    item = buffer.popleft()
                    consumed.append(item)
                    occupancy = len(buffer)
                    overlap["in_critical"] -= 1
                empty.release()           # signal a free slot
                log(f"consumer {cid} got {item:<10} "
                    f"(buffer {occupancy}/{buf_size})")
                time.sleep(0.015)

        threads = ([threading.Thread(target=producer, args=(i + 1,))
                    for i in range(n_prod)] +
                   [threading.Thread(target=consumer,
                                     args=(i + 1, total // n_cons))
                    for i in range(n_cons)])
        self.join_threads(threads)
        ok = (sorted(produced) == sorted(consumed)
              and len(consumed) == total
              and overlap["max_in_critical"] == 1)
        print(f"produced {len(produced)}, consumed {len(consumed)}, "
              f"every item consumed exactly once: "
              f"{sorted(produced) == sorted(consumed)}")
        print(f"maximum threads inside the critical section at once: "
              f"{overlap['max_in_critical']}")
        print("result: no race conditions, no lost or duplicated items"
              if ok else "result: INCONSISTENCY DETECTED")

    # ------------------------------------------------------------------ #
    def error(self, message):
        sys.stderr.write(f"{PROMPT_NAME}: {message}\n")
        sys.stderr.flush()


if __name__ == "__main__":
    try:
        Shell().run()
    except (BrokenPipeError, KeyboardInterrupt):
        sys.exit(1)
    except OSError as exc:
        sys.stderr.write(f"{PROMPT_NAME}: cannot start: {exc}\n")
        sys.exit(1)
