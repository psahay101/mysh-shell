#!/usr/bin/env python3
"""
mysh - a small Unix-like shell with process management and job control.

MSCS630 Advanced Operating Systems - Project Deliverable 1
Author: Prateek Sahay

Features
--------
* Built-in commands: cd, pwd, exit, echo, clear, ls, cat, mkdir, rmdir,
  rm, touch, kill, jobs, fg, bg
* External commands executed with fork() + execvp() + waitpid()
* Foreground and background execution ("cmd &")
* Job table with job ids, process ids, and status (Running/Stopped/Done)
* Job control: jobs, fg [job_id], bg [job_id]; Ctrl-Z stops the
  foreground job when running on a real terminal
* Error handling with clear messages for invalid commands and arguments
"""

import errno
import os
import shlex
import signal
import stat
import sys

PROMPT_NAME = "mysh"


class Job:
    """One background or stopped job tracked by the shell."""

    def __init__(self, job_id, pgid, command):
        self.job_id = job_id
        self.pgid = pgid          # process group id (== child pid here)
        self.command = command
        self.status = "Running"   # Running | Stopped | Done


class Shell:
    def __init__(self):
        self.jobs = {}            # job_id -> Job
        self.next_job_id = 1
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
