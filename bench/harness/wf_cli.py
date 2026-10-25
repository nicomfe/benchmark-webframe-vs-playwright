"""Configuration wf-cli: one webframe process per check.

Command: ``webframe -e -c <url>`` with stdin controlled by the harness.

* ``-e``   exit 1 on the first page exception (how an agent gets a failure signal)
* ``-c``   console mode, so the harness can (a) evaluate an expression after the
           page printed its marker line and (b) end the process cleanly by
           closing stdin. Without ``-c`` webframe only exits when the page calls
           ``window.close()`` or on a signal; closing stdin is the documented
           way to stop it and is equivalent to an agent sending SIGTERM.

Timer: from just before ``Popen`` until the process has exited (wait4). This
includes process spawn, webview creation, page load, marker wait, eval and
teardown. Breakdown timestamps are offsets from the same t0.

``wf-cli-display`` (Linux only) is the same harness with ``DISPLAY`` pointing
at an Xvfb the runner started beforehand, instead of webframe starting its
own private Xvfb per process.
"""
from __future__ import annotations

import sys

from harness.base import CheckResult, Child, Ctx, Harness, now, parse_eval_marker, split_stderr, wf_wrap_eval


class WfCli(Harness):
    name = "wf-cli"
    kind = "cold"

    def __init__(self, ctx: Ctx, display: str | None = None):
        super().__init__(ctx)
        self.display = display
        self.env = dict(ctx.base_env)
        if sys.platform.startswith("linux"):
            self.env.pop("DISPLAY", None)
            self.env.pop("WAYLAND_DISPLAY", None)
            if display:
                self.env["DISPLAY"] = display

    def check(self, url, wait_for, eval_expr) -> CheckResult:
        res = CheckResult()
        ctx = self.ctx
        argv = [ctx.webframe_bin, "-e", "-c", url]
        t0 = now()
        deadline = t0 + ctx.timeout_s
        child = Child(argv, self.env, stdin=True)
        res.breakdown_ms["spawn"] = (child.t_spawned - t0) * 1000
        win = ctx.sampler.open_window(ctx.label, child.pid)
        try:
            idx = None
            if wait_for:
                idx = child.stdout.wait_for(lambda l: l == wait_for or l.startswith(wait_for), deadline, exited=child.exited)
                if idx is not None:
                    res.breakdown_ms["marker"] = (child.stdout.lines[idx][0] - t0) * 1000
            if child.stdout.lines:
                res.breakdown_ms["first_output"] = (child.stdout.lines[0][0] - t0) * 1000
            if wait_for and idx is None and not child.exited():
                res.timed_out = True
            if eval_expr and not child.exited() and not res.timed_out:
                child.write_line(wf_wrap_eval(eval_expr))
                start = len(child.stdout.lines)
                j = child.stdout.wait_for(lambda l: parse_eval_marker(l)[0], deadline, start_index=start, exited=child.exited)
                if j is None:
                    if not child.exited():
                        res.timed_out = True
                else:
                    res.eval_result = parse_eval_marker(child.stdout.lines[j][1])[1]
                    res.breakdown_ms["eval"] = (child.stdout.lines[j][0] - t0) * 1000
            # Closing stdin ends console mode -> webframe terminates with 0.
            child.close_stdin()
            if not child.wait(max(0.0, deadline - now())):
                res.timed_out = True
                child.kill()
            t_end = now()
            res.breakdown_ms["exit"] = (t_end - t0) * 1000
            res.wall_ms = (t_end - t0) * 1000
            res.exit_code = child.exit_code
            res.cpu_s = child.cpu_seconds()
            res.cpu_src = "rusage"
        finally:
            ctx.sampler.close_window(win)
            child.stdout.eof.wait(1)
            child.stderr.eof.wait(1)
            child.close_pipes()
        self._stats(win, res)
        all_out = child.stdout.texts()
        # Drop our own eval result line(s) from "page output".
        res.stdout = [l for l in all_out if not parse_eval_marker(l)[0]]
        errs, noise = split_stderr(child.stderr.texts())
        res.errors = errs
        res.raw_stderr = child.stderr.texts()
        if res.timed_out:
            res.harness_error = f"timeout after {ctx.timeout_s}s"
        return res

    def version_info(self) -> dict:
        import subprocess

        out = subprocess.run([self.ctx.webframe_bin, "-v"], capture_output=True, text=True).stdout.strip()
        return {"webframe_version": out, "argv": "webframe -e -c <url>", "display": self.display}
