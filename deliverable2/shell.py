#!/usr/bin/env python3
"""
mysh - a small Unix-like shell with process management, job control,
and an internal process scheduler.

MSCS630 Advanced Operating Systems - Project Deliverable 2
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
"""

import errno
import heapq
import math
import os
import shlex
import signal
import stat
import sys
import time
from collections import deque

PROMPT_NAME = "mysh"


class Job:
    """One background or stopped job tracked by the shell."""

    def __init__(self, job_id, pgid, command):
        self.job_id = job_id
        self.pgid = pgid          # process group id (== child pid here)
        self.command = command
        self.status = "Running"   # Running | Stopped | Done


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


class Shell:
    def __init__(self):
        self.jobs = {}            # job_id -> Job
        self.next_job_id = 1
        self.sim_procs = []       # SimProcess objects awaiting a run
        self.next_sim_pid = 1
        self.timescale = 1.0      # real seconds per simulated second
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
    def run(self):
        while not self.exit_requested:
            self.reap_background_jobs()
            try:
                cwd = os.getcwd()
            except OSError:
                cwd = "(deleted)"
            prompt = f"{PROMPT_NAME}:{cwd}$ "
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
    def split_background(line):
        """Return (background, line, error) for a trailing unquoted '&'.

        An unquoted '&' anywhere but the end is a syntax error rather
        than ordinary argument text, matching a real shell.
        """
        quote = None
        i = 0
        last_amp = None
        amp_positions = []
        while i < len(line):
            ch = line[i]
            if quote is not None:
                if ch == "\\" and quote == '"' and i + 1 < len(line):
                    i += 2
                    continue
                if ch == quote:
                    quote = None
            elif ch in ("'", '"'):
                quote = ch
            elif ch == "\\" and i + 1 < len(line):
                i += 2
                continue
            elif ch == "&":
                last_amp = i
                amp_positions.append(i)
            elif not ch.isspace():
                last_amp = None
            i += 1
        if last_amp is None:
            if amp_positions:
                return False, line, True
            return False, line, False
        if len(amp_positions) > 1:
            return False, line, True
        return True, line[:last_amp] + line[last_amp + 1:], False

    def execute_line(self, line):
        if not line or line.startswith("#"):
            return
        # Split off a trailing UNQUOTED '&' before quote removal, so
        # that `cmd&` backgrounds like `cmd &` while a quoted "&" stays
        # an ordinary argument.
        background, line, amp_error = self.split_background(line)
        if amp_error:
            self.error("syntax error: '&' is only allowed at the "
                       "end of a command")
            return
        try:
            tokens = shlex.split(line)
        except ValueError as exc:
            self.error(f"parse error: {exc}")
            return
        if not tokens:
            if background:
                self.error("syntax error: no command before '&'")
            return

        name, args = tokens[0], tokens[1:]
        if name == "":
            self.error("empty command")
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
    # Process management: fork / exec / wait
    # ------------------------------------------------------------------ #
    def launch_process(self, tokens, background):
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

    def wait_foreground(self, pgid, command, job=None):
        """Give the terminal to the child, wait, then take it back.

        If the command stops, re-register it in the job table — reusing
        the existing Job (and its id) when one is passed in, so a job
        keeps a stable id across fg / stop cycles.
        """
        if self.interactive:
            os.tcsetpgrp(sys.stdin.fileno(), pgid)
        try:
            _, status = os.waitpid(pgid, os.WUNTRACED)
            if os.WIFSTOPPED(status):
                if job is None:
                    job = self.add_job(pgid, command)
                else:
                    self.jobs[job.job_id] = job
                job.status = "Stopped"
                print(f"\n[{job.job_id}]  Stopped    {command}")
        except ChildProcessError:
            pass
        finally:
            if self.interactive:
                os.tcsetpgrp(sys.stdin.fileno(), os.getpgrp())

    def add_job(self, pgid, command):
        job = Job(self.next_job_id, pgid, command)
        self.jobs[job.job_id] = job
        self.next_job_id += 1
        return job

    def reap_background_jobs(self):
        """Collect finished background children without blocking."""
        for job in list(self.jobs.values()):
            if job.status == "Done":
                continue
            try:
                pid, status = os.waitpid(
                    job.pgid,
                    os.WNOHANG | os.WUNTRACED | os.WCONTINUED)
            except ChildProcessError:
                pid, status = job.pgid, 0
            if pid == 0:
                continue
            if os.WIFSTOPPED(status):
                job.status = "Stopped"
            elif os.WIFCONTINUED(status):
                job.status = "Running"
            else:
                job.status = "Done"
                print(f"[{job.job_id}]  Done       {job.command}")
                del self.jobs[job.job_id]

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
            for job in running:
                try:
                    os.killpg(job.pgid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
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
        if not args:
            self.error("cat: missing file name")
            return
        for path in args:
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
            try:
                os.remove(path)
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
            pid = job.pgid
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
        self.wait_foreground(job.pgid, job.command, job=job)

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
