"""Configurations pw-<engine>-warm: one long-lived ``node pw_warm.js <engine>``
process with the browser launched once; each check is a JSON request over
stdin that opens a new page, loads, waits, evaluates, closes the page.

Timer: from writing the request line until the response line was read
(includes IPC over pipes, new page, load, eval, page close; excludes browser
launch).
"""
from __future__ import annotations

import json
import os

from harness.base import CheckResult, Child, Ctx, Harness, now, t_wall


class PwWarm(Harness):
    kind = "warm"

    def __init__(self, ctx: Ctx, engine: str):
        super().__init__(ctx)
        self.engine = engine
        self.name = f"pw-{engine}-warm"
        self.script = os.path.join(ctx.harness_dir, "pw_warm.js")
        self.env = dict(ctx.base_env)
        self.child: Child | None = None
        self.win = None
        self.launch_ms = None
        self.browser_version = None
        self.restarts = 0
        self._next_id = 0

    def _launch(self):
        self.child = Child([self.ctx.node_bin, self.script, self.engine], self.env, stdin=True)
        self.win = self.ctx.sampler.open_window(self.ctx.label, self.child.pid)
        idx = self.child.stdout.wait_for(lambda l: l.startswith('{"ready"'), now() + self.ctx.timeout_s + 10, exited=self.child.exited)
        if idx is None:
            raise RuntimeError(f"pw_warm {self.engine} did not start: " + "\n".join(self.child.stderr.texts()[-5:]))
        info = json.loads(self.child.stdout.lines[idx][1])
        self.launch_ms = info.get("launchMs")
        self.browser_version = info.get("version")

    def setup(self):
        self._launch()

    def teardown(self):
        if self.child:
            self.child.write_line(json.dumps({"cmd": "quit"}))
            self.child.close_stdin()
            if not self.child.wait(5):
                self.child.kill()
            self.child.close_pipes()
            if self.win:
                self.ctx.sampler.close_window(self.win)

    def check(self, url, wait_for, eval_expr) -> CheckResult:
        res = CheckResult()
        if self.child is None or self.child.exited():
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
        self._next_id += 1
        req = {"id": self._next_id, "url": url, "waitFor": wait_for, "eval": eval_expr, "timeoutMs": int(self.ctx.timeout_s * 1000)}
        start = len(child.stdout.lines)
        t0 = now()
        child.write_line(json.dumps(req))
        idx = child.stdout.wait_for(lambda l: l.startswith("{") and f'"id":{self._next_id},' in l, t0 + self.ctx.timeout_s + 5, start_index=start, exited=child.exited)
        t_end = now()
        res.wall_ms = (t_end - t0) * 1000
        if idx is None:
            res.timed_out = True
            res.harness_error = f"no response within {self.ctx.timeout_s}s"
            res.raw_stderr = child.stderr.texts()[-5:]
            child.kill()
            return res
        payload = json.loads(child.stdout.lines[idx][1])
        res.stdout = payload.get("stdout", [])
        res.errors = payload.get("errors", [])
        res.eval_result = payload.get("evalResult")
        res.eval_error = payload.get("evalError")
        t = payload.get("t", {})
        acc = 0.0
        for key, label in (("newPageMs", "new_page"), ("gotoMs", "goto"), ("waitMs", "marker"), ("evalMs", "eval"), ("closeMs", "page_close")):
            if key in t:
                acc += t[key]
                res.breakdown_ms[label] = acc
        res.breakdown_ms["response"] = res.wall_ms
        st = self.ctx.sampler.window_slice_stats(self.win, t_wall(t0), t_wall(t_end))
        res.peak_rss_kb = st["peak_rss_kb"]
        res.cpu_sampled_s = st["cpu_sampled_s"]
        res.cpu_s = st["cpu_sampled_s"]
        res.cpu_src = "sampled"
        res.rss_samples = st["samples"]
        if payload.get("ok") is False:
            res.harness_error = "harness reported not ok"
        return res

    def version_info(self) -> dict:
        import subprocess

        pw = json.load(open(os.path.join(os.path.dirname(self.ctx.harness_dir), "..", "node_modules", "playwright", "package.json")))
        node = subprocess.run([self.ctx.node_bin, "--version"], capture_output=True, text=True).stdout.strip()
        return {
            "playwright_version": pw["version"],
            "node_version": node,
            "engine": self.engine,
            "browser_version": self.browser_version,
            "launch_ms": self.launch_ms,
            "argv": f"node pw_warm.js {self.engine} (long-lived, JSON lines over stdin)",
        }
