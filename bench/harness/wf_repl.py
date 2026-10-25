"""Configuration wf-repl: one long-lived ``webframe -c about:blank`` process.

Each check navigates the single webview with ``location.href = <url>``,
waits for the page's marker line on stdout, optionally evaluates an
expression, and is done. There is no "page close" (there is only one page).

``-e`` is deliberately *not* used: it would exit the process on the first
exception and the broken-page scenarios would kill the REPL. Exceptions are
read from stderr instead.

Timer: from writing the navigation command until the eval result (or the
marker line when there is no eval) was read. Browser/webview startup is
excluded, like for pw-*-warm.

Known behaviour: a page that calls ``window.close()`` (hello.html) makes
webframe exit. The harness records that (``process_restarted``) and relaunches
the REPL before the next check; relaunch time is reported separately and not
included in the check timer.
"""
from __future__ import annotations

import json
import sys

from harness.base import CheckResult, Child, Ctx, Harness, now, parse_eval_marker, split_stderr, t_wall, wf_wrap_eval


class WfRepl(Harness):
    name = "wf-repl"
    kind = "warm"

    def __init__(self, ctx: Ctx):
        super().__init__(ctx)
        self.env = dict(ctx.base_env)
        if sys.platform.startswith("linux"):
            self.env.pop("DISPLAY", None)
            self.env.pop("WAYLAND_DISPLAY", None)
        self.child: Child | None = None
        self.win = None
        self.launch_ms: float | None = None
        self.restarts = 0
        self._stderr_cursor = 0

    def _launch(self):
        t0 = now()
        self.child = Child([self.ctx.webframe_bin, "-c", "about:blank"], self.env, stdin=True)
        # Open the sampler window immediately so helper processes spawned
        # during startup (macOS WebContent etc.) are attributed to this config.
        self.win = self.ctx.sampler.open_window(self.ctx.label, self.child.pid)
        # Round-trip a trivial eval so we know the webview is ready.
        self.child.write_line("1")
        idx = self.child.stdout.wait_for(lambda l: l == "1", now() + self.ctx.timeout_s, exited=self.child.exited)
        if idx is None:
            raise RuntimeError("webframe REPL did not become ready: " + "\n".join(self.child.stderr.texts()))
        self.launch_ms = (now() - t0) * 1000
        self._stderr_cursor = len(self.child.stderr.lines)

    def setup(self):
        self._launch()

    def teardown(self):
        if self.child:
            self.child.close_stdin()
            if not self.child.wait(3):
                self.child.kill()
            self.child.close_pipes()
            if self.win:
                self.ctx.sampler.close_window(self.win)

    def check(self, url, wait_for, eval_expr) -> CheckResult:
        res = CheckResult()
        if self.child is None or self.child.exited():
            # Previous page closed the window (or the process died). Relaunch.
            if self.child is not None:
                self.child.close_pipes()
                if self.win:
                    self.ctx.sampler.close_window(self.win)
            t0 = now()
            self._launch()
            self.restarts += 1
            res.process_restarted = True
            res.restart_ms = (now() - t0) * 1000
        child = self.child
        t0 = now()
        deadline = t0 + self.ctx.timeout_s
        out_start = len(child.stdout.lines)
        err_start = len(child.stderr.lines)
        child.write_line("location.href = " + json.dumps(url))
        # The REPL echoes the assignment result (the quoted url); skip it.
        i = child.stdout.wait_for(lambda l: l == json.dumps(url), deadline, start_index=out_start, exited=child.exited)
        if i is not None:
            res.breakdown_ms["nav_ack"] = (child.stdout.lines[i][0] - t0) * 1000
            out_start = i + 1
        t_end = None
        if wait_for:
            idx = child.stdout.wait_for(lambda l: l == wait_for or l.startswith(wait_for), deadline, start_index=out_start, exited=child.exited)
            if idx is None:
                if not child.exited():
                    res.timed_out = True
            else:
                t_end = child.stdout.lines[idx][0]
                res.breakdown_ms["marker"] = (t_end - t0) * 1000
        if eval_expr and not res.timed_out and not child.exited():
            child.write_line(wf_wrap_eval(eval_expr))
            start = len(child.stdout.lines)
            j = child.stdout.wait_for(lambda l: parse_eval_marker(l)[0], deadline, start_index=start, exited=child.exited)
            if j is None:
                if not child.exited():
                    res.timed_out = True
            else:
                res.eval_result = parse_eval_marker(child.stdout.lines[j][1])[1]
                t_end = child.stdout.lines[j][0]
                res.breakdown_ms["eval"] = (t_end - t0) * 1000
        if t_end is None:
            t_end = now()
        res.wall_ms = (t_end - t0) * 1000
        if child.exited():
            res.exit_code = child.exit_code  # informational: the page closed the window
            res.process_restarted = res.process_restarted or False
        # Output/errors produced during this check.
        res.stdout = [l for _, l in child.stdout.lines[out_start:] if not parse_eval_marker(l)[0]]
        errs, _noise = split_stderr([l for _, l in child.stderr.lines[err_start:]])
        res.errors = errs
        res.raw_stderr = [l for _, l in child.stderr.lines[err_start:]]
        st = self.ctx.sampler.window_slice_stats(self.win, t_wall(t0), t_wall(t_end))
        res.peak_rss_kb = st["peak_rss_kb"]
        res.cpu_sampled_s = st["cpu_sampled_s"]
        res.cpu_s = st["cpu_sampled_s"]
        res.cpu_src = "sampled"
        res.rss_samples = st["samples"]
        if res.timed_out:
            res.harness_error = f"timeout after {self.ctx.timeout_s}s"
            # A hung REPL is useless; restart it for the next check.
            child.kill()
        return res

    def version_info(self) -> dict:
        import subprocess

        out = subprocess.run([self.ctx.webframe_bin, "-v"], capture_output=True, text=True).stdout.strip()
        return {"webframe_version": out, "argv": "webframe -c about:blank (long-lived)", "launch_ms": self.launch_ms}
