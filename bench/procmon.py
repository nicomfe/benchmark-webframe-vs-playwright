"""Process-tree resource sampler.

Samples every process on the machine at a fixed interval (``/proc`` on Linux,
``ps`` on macOS) and lets the runner ask, for a time window and a root PID,
what the peak summed RSS and the CPU time of that process tree were.

Why a global sampler instead of one per child: on macOS, WebKit helper
processes (``com.apple.WebKit.WebContent``, ``.Networking``, ``.GPU``) are
spawned through launchd and are *not* children of the process that uses the
webview, so a plain tree walk misses most of webframe's memory. The sampler
therefore tracks "orphan" helper processes and attributes each one to the
configuration whose measurement window was active when the helper first
appeared. On Linux WebKitGTK helpers are ordinary children and no attribution
is needed (Xvfb started by webframe is a child too).

Limitations (also stated in the report): sampling misses peaks shorter than
the interval; CPU time from samples loses whatever a process used before its
first and after its last sample. Cold runs therefore use ``wait4`` rusage for
CPU instead (see harness code).
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field

HELPER_RE = re.compile(r"com\.apple\.WebKit|WebKitWebProcess|WebKitNetworkProcess|WebKitGPUProcess|Xvfb", re.I)


@dataclass
class Proc:
    pid: int
    ppid: int
    rss_kb: int
    cpu_s: float
    comm: str


@dataclass
class Sample:
    ts: float
    procs: dict[int, Proc]


@dataclass
class Window:
    label: str
    root_pid: int
    start: float
    end: float | None = None
    samples: list[tuple[float, int, float]] = field(default_factory=list)  # (ts, rss_kb, cpu_s)
    helper_pids: set[int] = field(default_factory=set)


def _parse_cputime(s: str) -> float:
    # ps cputime: [[dd-]hh:]mm:ss[.cc]
    days = 0
    if "-" in s:
        d, s = s.split("-", 1)
        days = int(d)
    parts = s.split(":")
    secs = 0.0
    for p in parts:
        secs = secs * 60 + float(p)
    return days * 86400 + secs


def snapshot() -> dict[int, Proc]:
    procs: dict[int, Proc] = {}
    if sys.platform.startswith("linux"):
        page_kb = os.sysconf("SC_PAGE_SIZE") // 1024
        hz = os.sysconf("SC_CLK_TCK")
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            pid = int(name)
            try:
                with open(f"/proc/{pid}/stat", "rb") as f:
                    data = f.read().decode("utf-8", "replace")
            except OSError:
                continue
            # comm may contain spaces/parens; split around the last ')'.
            lp = data.rfind(")")
            comm = data[data.find("(") + 1 : lp]
            fields = data[lp + 2 :].split()
            try:
                ppid = int(fields[1])
                utime = int(fields[11])
                stime = int(fields[12])
                rss_pages = int(fields[21])
            except (IndexError, ValueError):
                continue
            procs[pid] = Proc(pid, ppid, rss_pages * page_kb, (utime + stime) / hz, comm)
    else:
        out = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,rss=,cputime=,comm="],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        for line in out.splitlines():
            parts = line.split(None, 4)
            if len(parts) < 5:
                continue
            try:
                pid, ppid, rss = int(parts[0]), int(parts[1]), int(parts[2])
                cpu = _parse_cputime(parts[3])
            except ValueError:
                continue
            procs[pid] = Proc(pid, ppid, rss, cpu, parts[4])
    return procs


def tree_pids(procs: dict[int, Proc], root: int) -> set[int]:
    children: dict[int, list[int]] = {}
    for p in procs.values():
        children.setdefault(p.ppid, []).append(p.pid)
    out = set()
    stack = [root]
    while stack:
        pid = stack.pop()
        if pid in out:
            continue
        out.add(pid)
        stack.extend(children.get(pid, []))
    return out


class Sampler:
    def __init__(self, interval: float = 0.1):
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._windows: list[Window] = []
        self._helper_owner: dict[int, str] = {}  # helper pid -> label of owning window
        self._baseline_helpers: set[int] = set()
        self.active_label: str | None = None  # label of the config currently running a check
        self.samples_taken = 0
        self.last_sample_cost_ms = 0.0

    def start(self):
        base = snapshot()
        self._baseline_helpers = {p.pid for p in base.values() if HELPER_RE.search(p.comm)}
        self._thread = threading.Thread(target=self._loop, name="procmon", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def set_active(self, label: str | None):
        self.active_label = label

    def open_window(self, label: str, root_pid: int) -> Window:
        w = Window(label=label, root_pid=root_pid, start=time.time())
        with self._lock:
            self._windows.append(w)
        return w

    def close_window(self, w: Window):
        # Take one final sample so very short runs get at least one data point.
        self._sample_once()
        with self._lock:
            w.end = time.time()
            if w in self._windows:
                self._windows.remove(w)

    def _loop(self):
        while not self._stop.is_set():
            t0 = time.time()
            self._sample_once()
            self.last_sample_cost_ms = (time.time() - t0) * 1000
            self._stop.wait(max(0.0, self.interval - (time.time() - t0)))

    def _sample_once(self):
        try:
            procs = snapshot()
        except Exception:
            return
        ts = time.time()
        self.samples_taken += 1
        with self._lock:
            # Attribute newly seen helper processes to the active windows.
            active = [w for w in self._windows if w.end is None]
            covered: set[int] = set()
            trees = {w.label: tree_pids(procs, w.root_pid) for w in active}
            for pids in trees.values():
                covered |= pids
            for p in procs.values():
                if p.pid in covered or p.pid in self._baseline_helpers:
                    continue
                if not HELPER_RE.search(p.comm):
                    continue
                if p.pid not in self._helper_owner:
                    labels = [w.label for w in active]
                    if self.active_label in labels:
                        # The configuration currently running a check.
                        self._helper_owner[p.pid] = self.active_label
                    elif len(active) == 1:
                        self._helper_owner[p.pid] = active[0].label
                    elif active:
                        # Ambiguous: prefer a window that started most recently.
                        self._helper_owner[p.pid] = max(active, key=lambda w: w.start).label
                    else:
                        self._helper_owner[p.pid] = "?"
            # Forget helpers that no longer exist.
            for pid in [pid for pid in self._helper_owner if pid not in procs]:
                del self._helper_owner[pid]
            for w in active:
                pids = set(trees[w.label])
                pids |= {pid for pid, owner in self._helper_owner.items() if owner == w.label}
                w.helper_pids |= {pid for pid in pids if pid not in trees[w.label]}
                rss = sum(procs[pid].rss_kb for pid in pids if pid in procs)
                cpu = sum(procs[pid].cpu_s for pid in pids if pid in procs)
                w.samples.append((ts, rss, cpu))

    @staticmethod
    def window_stats(w: Window) -> dict:
        if not w.samples:
            return {"peak_rss_kb": None, "cpu_sampled_s": None, "samples": 0}
        peak = max(s[1] for s in w.samples)
        cpu = w.samples[-1][2] - w.samples[0][2] if len(w.samples) > 1 else 0.0
        return {"peak_rss_kb": peak, "cpu_sampled_s": round(max(cpu, 0.0), 3), "samples": len(w.samples)}

    @staticmethod
    def window_slice_stats(w: Window, start: float, end: float) -> dict:
        """Stats for a sub-interval of a long-lived window (warm configurations)."""
        sl = [s for s in w.samples if start <= s[0] <= end]
        if not sl:
            # Fall back to the nearest sample before `end` so short checks get a value.
            before = [s for s in w.samples if s[0] <= end]
            if before:
                sl = [before[-1]]
        if not sl:
            return {"peak_rss_kb": None, "cpu_sampled_s": None, "samples": 0}
        peak = max(s[1] for s in sl)
        cpu = sl[-1][2] - sl[0][2] if len(sl) > 1 else 0.0
        return {"peak_rss_kb": peak, "cpu_sampled_s": round(max(cpu, 0.0), 3), "samples": len(sl)}
