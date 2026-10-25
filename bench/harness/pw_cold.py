"""Configurations pw-<engine>-cold: a fresh ``node bench/harness/pw_cold.js``
process per check (browser launched and closed inside it).

Timer: from just before ``Popen`` until the node process exited (wait4), so
Node startup + ``require('playwright')`` + browser launch + page + teardown are
all included -- the cost an agent pays when it shells out to a script.
"""
from __future__ import annotations

import json
import os

from harness.base import CheckResult, Child, Ctx, Harness, now


class PwCold(Harness):
    kind = "cold"

    def __init__(self, ctx: Ctx, engine: str):
        super().__init__(ctx)
        self.engine = engine
        self.name = f"pw-{engine}-cold"
        self.script = os.path.join(ctx.harness_dir, "pw_cold.js")
        self.env = dict(ctx.base_env)

    def check(self, url, wait_for, eval_expr) -> CheckResult:
        res = CheckResult()
        ctx = self.ctx
        req = {"id": 1, "url": url, "waitFor": wait_for, "eval": eval_expr, "timeoutMs": int(ctx.timeout_s * 1000)}
        argv = [ctx.node_bin, self.script, self.engine, json.dumps(req)]
        t0 = now()
        child = Child(argv, self.env, stdin=False)
        res.breakdown_ms["spawn"] = (child.t_spawned - t0) * 1000
        win = ctx.sampler.open_window(ctx.label, child.pid)
        try:
            if not child.wait(ctx.timeout_s + 5):
                res.timed_out = True
                child.kill()
            t_end = now()
            res.wall_ms = (t_end - t0) * 1000
            res.breakdown_ms["exit"] = res.wall_ms
            res.exit_code = child.exit_code
            res.cpu_s = child.cpu_seconds()
            res.cpu_src = "rusage"
        finally:
            ctx.sampler.close_window(win)
            child.stdout.eof.wait(1)
            child.stderr.eof.wait(1)
            child.close_pipes()
        self._stats(win, res)
        lines = child.stdout.texts()
        payload = None
        for ts, l in child.stdout.lines:
            if l.startswith("{"):
                try:
                    payload = json.loads(l)
                    res.breakdown_ms["result_line"] = (ts - t0) * 1000
                except ValueError:
                    pass
        if payload is None:
            res.harness_error = "no JSON result from node: " + " | ".join(lines[-3:] + child.stderr.texts()[-3:])
            res.raw_stderr = child.stderr.texts()
            return res
        res.stdout = payload.get("stdout", [])
        res.errors = payload.get("errors", [])
        res.eval_result = payload.get("evalResult")
        res.eval_error = payload.get("evalError")
        t = payload.get("t", {})
        # Convert the node-internal durations into offsets from t0 as far as
        # possible: spawn -> node ready (requireMs starts when main() runs).
        node_start = res.wall_ms - t.get("scriptMs", 0) if "scriptMs" in t else None
        if node_start is not None:
            acc = node_start
            res.breakdown_ms["node_main"] = acc
            for key, label in (("requireMs", "require"), ("launchMs", "launch"), ("newPageMs", "new_page"), ("gotoMs", "goto"), ("waitMs", "marker"), ("evalMs", "eval"), ("closeMs", "page_close"), ("browserCloseMs", "browser_close")):
                if key in t:
                    acc += t[key]
                    res.breakdown_ms[label] = acc
        res.raw_stderr = child.stderr.texts()
        if payload.get("ok") is False and not res.errors:
            res.harness_error = "harness reported not ok"
        if res.timed_out:
            res.harness_error = f"timeout after {ctx.timeout_s}s"
        return res

    def version_info(self) -> dict:
        import subprocess

        pw = json.load(open(os.path.join(os.path.dirname(self.ctx.harness_dir), "..", "node_modules", "playwright", "package.json")))
        node = subprocess.run([self.ctx.node_bin, "--version"], capture_output=True, text=True).stdout.strip()
        return {"playwright_version": pw["version"], "node_version": node, "engine": self.engine, "argv": f"node pw_cold.js {self.engine} <request-json>"}
