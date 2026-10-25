"""Shared plumbing for the harnesses: process spawning with rusage, line
readers, and the result dictionary every harness returns."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from procmon import Sampler, Window


def now() -> float:
    return time.perf_counter()


_perf0 = time.perf_counter()
_wall0 = time.time()


def t_wall(perf_t: float) -> float:
    """Convert a perf_counter timestamp to wall-clock time (what the sampler uses)."""
    return _wall0 + (perf_t - _perf0)


@dataclass
class Ctx:
    """Everything a harness needs from the runner."""

    sampler: Sampler
    webframe_bin: str
    node_bin: str
    harness_dir: str
    base_env: dict[str, str]
    timeout_s: float = 20.0
    label: str = ""  # configuration label this harness instance runs as


@dataclass
class CheckResult:
    stdout: list[str] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    eval_result: Any = None
    eval_error: str | None = None
    exit_code: int | None = None
    wall_ms: float = 0.0
    breakdown_ms: dict[str, float] = field(default_factory=dict)
    peak_rss_kb: int | None = None
    cpu_s: float | None = None
    cpu_src: str = ""
    cpu_sampled_s: float | None = None
    rss_samples: int = 0
    process_restarted: bool = False
    restart_ms: float | None = None
    timed_out: bool = False
    harness_error: str | None = None
    raw_stderr: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        d = self.__dict__.copy()
        return d


class LineReader:
    """Reads lines from a pipe on a background thread with timestamps."""

    def __init__(self, pipe, on_line=None):
        self.pipe = pipe
        self.lines: list[tuple[float, str]] = []
        self.eof = threading.Event()
        self.cond = threading.Condition()
        self.on_line = on_line
        self.t = threading.Thread(target=self._run, daemon=True)
        self.t.start()

    def _run(self):
        try:
            for raw in iter(self.pipe.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                with self.cond:
                    self.lines.append((now(), line))
                    self.cond.notify_all()
                if self.on_line:
                    self.on_line(line)
        except (ValueError, OSError):
            pass
        finally:
            with self.cond:
                self.eof.set()
                self.cond.notify_all()

    def wait_for(self, pred, deadline: float, start_index: int = 0, exited=None) -> int | None:
        """Block until a line (at or after start_index) satisfies pred.

        Returns the index of the matching line, or None on EOF / deadline /
        when `exited()` turns true.
        """
        i = start_index
        with self.cond:
            while True:
                while i < len(self.lines):
                    if pred(self.lines[i][1]):
                        return i
                    i += 1
                if self.eof.is_set():
                    return None
                if exited is not None and exited():
                    # Give a few ms for any buffered output to flush.
                    self.cond.wait(0.02)
                    while i < len(self.lines):
                        if pred(self.lines[i][1]):
                            return i
                        i += 1
                    return None
                remaining = deadline - now()
                if remaining <= 0:
                    return None
                self.cond.wait(min(remaining, 0.005))

    def texts(self) -> list[str]:
        with self.cond:
            return [l for _, l in self.lines]


class Child:
    """A spawned process whose exit is collected with wait4 so we get rusage
    (CPU time of the process and every descendant it reaped)."""

    def __init__(self, argv: list[str], env: dict[str, str], stdin=True):
        self.argv = argv
        self.t_spawn0 = now()
        self.p = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE if stdin else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            start_new_session=True,
        )
        self.t_spawned = now()
        self.pid = self.p.pid
        self.stdout = LineReader(self.p.stdout)
        self.stderr = LineReader(self.p.stderr)
        self.exit_code: int | None = None
        self.rusage = None
        self._reaped = False

    def exited(self) -> bool:
        if self._reaped:
            return True
        try:
            pid, status, ru = os.wait4(self.pid, os.WNOHANG)
        except ChildProcessError:
            self._reaped = True
            return True
        if pid == 0:
            return False
        self._finish(status, ru)
        return True

    def _finish(self, status, ru):
        self._reaped = True
        self.exit_code = os.waitstatus_to_exitcode(status)
        self.rusage = ru
        self.p.returncode = self.exit_code  # keep Popen consistent

    def wait(self, timeout: float) -> bool:
        deadline = now() + timeout
        while not self.exited():
            if now() > deadline:
                return False
            time.sleep(0.001)
        return True

    def write_line(self, line: str):
        try:
            self.p.stdin.write((line + "\n").encode())
            self.p.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass

    def close_stdin(self):
        try:
            if self.p.stdin:
                self.p.stdin.close()
        except (BrokenPipeError, OSError):
            pass

    def kill(self):
        try:
            os.killpg(self.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            try:
                self.p.kill()
            except ProcessLookupError:
                pass
        self.wait(5)

    def terminate(self):
        try:
            os.killpg(self.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        if not self.wait(3):
            self.kill()

    def close_pipes(self):
        for pipe in (self.p.stdout, self.p.stderr):
            try:
                pipe.close()
            except OSError:
                pass

    def cpu_seconds(self) -> float | None:
        if self.rusage is None:
            return None
        return self.rusage.ru_utime + self.rusage.ru_stime


def parse_eval_marker(line: str):
    """webframe REPL prints an object result as JSON; we wrap evals as
    ({__r: EXPR}) so the result line is {"__r": ...}. Returns (found, value)."""
    if not line.startswith("{"):
        return False, None
    try:
        obj = json.loads(line)
    except ValueError:
        return False, None
    if isinstance(obj, dict) and "__r" in obj:
        return True, obj["__r"]
    if obj == {}:
        return True, None  # EXPR was undefined
    return False, None


def wf_wrap_eval(expr: str) -> str:
    return "({__r: (" + expr + ")})"


# macOS prints occasional framework noise on stderr; it is not a page error.
_NOISE_PREFIXES = ("objc[", "Error received in message reply handler", "Connection Invalid error")


def split_stderr(lines: list[str]) -> tuple[list[dict], list[str]]:
    """Group webframe stderr into error records (message + indented stack lines)."""
    errors: list[dict] = []
    noise: list[str] = []
    for line in lines:
        if not line.strip():
            continue
        if line.startswith(_NOISE_PREFIXES) or (len(line) > 24 and line[4] == "-" and "webframe[" in line[:60]):
            noise.append(line)
            continue
        if line.startswith((" ", "\t")) and errors:
            errors[-1]["text"] += "\n" + line
        else:
            errors.append({"type": "stderr", "text": line})
    return errors, noise


class Harness:
    name = "base"
    kind = "cold"  # or "warm"

    def __init__(self, ctx: Ctx):
        self.ctx = ctx

    def setup(self):
        pass

    def teardown(self):
        pass

    def check(self, url: str, wait_for: str | None, eval_expr: str | None) -> CheckResult:
        raise NotImplementedError

    def version_info(self) -> dict:
        return {}

    def _stats(self, w: Window, res: CheckResult):
        st = self.ctx.sampler.window_stats(w)
        res.peak_rss_kb = st["peak_rss_kb"]
        res.cpu_sampled_s = st["cpu_sampled_s"]
        res.rss_samples = st["samples"]
