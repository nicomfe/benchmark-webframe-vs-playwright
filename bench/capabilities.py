#!/usr/bin/env python3
"""Capability matrix (Spec section 9), filled by *running* each probe.

Writes results/raw/<label>/capabilities.json. Statuses: Yes / No / Workaround.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from server import StaticServer  # noqa: E402

CAPS = [
    ("console", "Capture console output"),
    ("uncaught_exception", "Detect uncaught exceptions"),
    ("unhandled_rejection", "Detect unhandled rejections"),
    ("failed_resource", "Detect failed resource loads"),
    ("eval", "Evaluate JS in page and get result"),
    ("screenshot", "Screenshot"),
    ("input", "Click / type / keyboard input"),
    ("wait", "Wait for selector or network idle"),
    ("intercept", "Network request interception"),
    ("multi_page", "Multiple pages or tabs"),
    ("structured_output", "Structured (JSON) output"),
    ("exit_code", "Exit code on failure"),
]


def wf(bin_: str, args: list[str], stdin_lines: list[str] | None, wait_s: float = 1.5, env=None, timeout=20) -> tuple[int, str, str]:
    """Run webframe; if stdin_lines given, wait `wait_s` for the page then send them."""
    p = subprocess.Popen([bin_, *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    try:
        if stdin_lines is not None:
            time.sleep(wait_s)
            for l in stdin_lines:
                p.stdin.write(l + "\n")
                p.stdin.flush()
            time.sleep(0.3)
            p.stdin.close()
            p.stdin = None  # communicate() must not touch the closed pipe
        else:
            # Without -c webframe runs until window.close()/signal: give it a moment, then stop it.
            time.sleep(wait_s)
            p.terminate()
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        out, err = p.communicate()
    return p.returncode, out, err


def webframe_probes(bin_: str, base: str, env: dict) -> dict:
    out = {}

    code, so, se = wf(bin_, ["-c", f"{base}/ok.html"], ["1"])
    out["console"] = {"status": "Yes" if "READY" in so else "No", "note": "console.log -> stdout, other levels prefixed [warn]/[error]", "evidence": so.strip().splitlines()[:3]}

    code, so, se = wf(bin_, ["-e", f"{base}/broken-throw.html"], None)
    out["uncaught_exception"] = {"status": "Yes" if "boom" in se else "No", "note": f"stderr message + stack; -e exits {code}", "evidence": se.strip().splitlines()[:3]}

    code, so, se = wf(bin_, ["-e", f"{base}/broken-rejection.html"], None)
    out["unhandled_rejection"] = {"status": "Yes" if "nope" in se else "No", "note": f"stderr 'Unhandled promise rejection: ...'; -e exits {code}", "evidence": se.strip().splitlines()[:3]}

    code, so, se = wf(bin_, [f"{base}/broken-resource.html"], None, wait_s=2.0)
    png = "missing.png" in se
    js = "missing.js" in se
    status = "Yes" if (png and js) else ("Workaround" if (png or js) else "No")
    note = "stderr 'Failed to load resource: <tag> url'"
    if status == "Workaround":
        note += f"; only the {'<script>' if js else '<img>'} 404 was reported in this run, not both"
    out["failed_resource"] = {"status": status, "note": note, "evidence": se.strip().splitlines()[:4]}

    code, so, se = wf(bin_, ["-c", f"{base}/ok.html"], ["document.querySelectorAll('li').length"])
    out["eval"] = {"status": "Yes" if "100" in so.splitlines() else "No", "note": "-c: each stdin line evaluated, result printed (promises awaited)", "evidence": so.strip().splitlines()[-1:]}

    # Screenshot: no flag exists (webframe -h lists none). Workaround: serialize the DOM.
    code, so, se = wf(bin_, ["-c", f"{base}/ok.html"], ["document.documentElement.outerHTML.length"])
    dom_ok = any(l.strip().isdigit() and int(l) > 1000 for l in so.splitlines())
    out["screenshot"] = {
        "status": "Workaround" if dom_ok else "No",
        "note": "no screenshot API; DOM snapshot via eval (outerHTML/getBoundingClientRect), no pixels, ~1 eval round trip",
        "evidence": so.strip().splitlines()[-1:],
    }

    lines = [
        "document.getElementById('btn').click()",
        "var i = document.getElementById('inp'); i.value = 'hello'; i.dispatchEvent(new Event('input', {bubbles:true}))",
        "document.getElementById('count').textContent + '|' + document.getElementById('typed').textContent + '|' + document.getElementById('trusted').textContent",
    ]
    code, so, se = wf(bin_, ["-c", f"{base}/caps.html"], lines)
    last = so.strip().splitlines()[-1] if so.strip() else ""
    ok = '"1|hello|synthetic"' in last
    out["input"] = {
        "status": "Workaround" if ok else "No",
        "note": "no input API; synthetic DOM events via eval (el.click(), dispatchEvent). isTrusted=false, no real keyboard/mouse, no focus/IME semantics",
        "evidence": [last],
    }

    lines = ["new Promise(r => { const t0 = Date.now(); const h = setInterval(() => { const el = document.querySelector('#late'); if (el) { clearInterval(h); r('late after ' + (Date.now() - t0) + 'ms (since eval)'); } }, 10); })"]
    code, so, se = wf(bin_, ["-c", f"{base}/caps.html"], lines, wait_s=0.8)
    waited = any("late after" in l for l in so.splitlines())
    out["wait"] = {
        "status": "Workaround" if waited else "No",
        "note": "no wait API; eval a polling Promise (webframe awaits promises). Network idle: not available (no network events exposed)",
        "evidence": [l for l in so.splitlines() if "late" in l][:1],
    }

    # Interception: there is no network API. Patching window.fetch via eval
    # only works after the page's own scripts ran, so it cannot affect the
    # SPA's initial fetch. We verify that limitation rather than claim a workaround.
    lines = ["typeof window.fetch", "document.querySelectorAll('#list li').length"]
    code, so, se = wf(bin_, ["-c", f"{base}/spa.html"], lines)
    out["intercept"] = {
        "status": "No",
        "note": "no request/route API; WF_* env vars inject string globals before scripts run but cannot alter requests; patching fetch via eval is too late for initial loads",
        "evidence": so.strip().splitlines()[-2:],
    }

    # Multiple pages: one webview per process; run two processes concurrently.
    a = subprocess.Popen([bin_, "-c", f"{base}/ok.html"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    b = subprocess.Popen([bin_, "-c", f"{base}/spa.html"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    time.sleep(1.5)
    for p in (a, b):
        p.stdin.write("document.title\n")
        p.stdin.flush()
        p.stdin.close()
        p.stdin = None
    oa, _ = a.communicate(timeout=20)
    ob, _ = b.communicate(timeout=20)
    ok = '"ok"' in oa and '"spa"' in ob
    out["multi_page"] = {"status": "Workaround" if ok else "No", "note": "one webview per process; run N processes (N x startup cost, no shared session)", "evidence": [oa.strip().splitlines()[-1:], ob.strip().splitlines()[-1:]]}

    code, so, se = wf(bin_, ["-c", f"{base}/ok.html"], ["({li: document.querySelectorAll('li').length, title: document.title})", "console.log({a:1})"])
    json_lines = []
    for l in so.splitlines():
        try:
            json_lines.append(json.loads(l))
        except ValueError:
            pass
    out["structured_output"] = {
        "status": "Workaround" if json_lines else "No",
        "note": "objects print as JSON (eval results and console.log args), but stdout/stderr are plain line streams: no envelope separating page output, eval results and errors; caller must parse/mark lines",
        "evidence": json_lines[:2],
    }

    code, so, se = wf(bin_, ["-e", f"{base}/broken-throw.html"], None)
    out["exit_code"] = {"status": "Yes" if code == 1 else "No", "note": f"-e: exit 1 on first exception (got {code}); exit 2 on bad usage", "evidence": code}
    return out


def main():
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default=os.environ.get("ENV_LABEL", ""))
    ap.add_argument("--webframe", default=os.environ.get("WEBFRAME_BIN", os.path.join(ROOT, ".cache", "bin", "webframe")))
    ap.add_argument("--node", default=os.environ.get("NODE_BIN", shutil.which("node") or "node"))
    ap.add_argument("--browsers-path", default=os.environ.get("PLAYWRIGHT_BROWSERS_PATH", os.path.join(ROOT, ".cache", "ms-playwright")))
    ap.add_argument("--engines", default="chromium,webkit")
    args = ap.parse_args()
    if not args.label:
        sys.path.insert(0, HERE)
        from run import default_label

        args.label = default_label()

    env = dict(os.environ)
    env["PLAYWRIGHT_BROWSERS_PATH"] = args.browsers_path
    if sys.platform.startswith("linux"):
        env.pop("DISPLAY", None)  # webframe starts its own Xvfb
    server = StaticServer(os.path.join(ROOT, "pages")).start()
    result = {"label": args.label, "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "capabilities": [c for c in CAPS], "webframe": {}, "playwright": {}}
    try:
        print("probing webframe...", file=sys.stderr, flush=True)
        result["webframe"] = webframe_probes(args.webframe, server.base_url, env)
        for eng in args.engines.split(","):
            print(f"probing playwright {eng}...", file=sys.stderr, flush=True)
            p = subprocess.run([args.node, os.path.join(HERE, "harness", "pw_caps.js"), eng, server.base_url], capture_output=True, text=True, env=env, timeout=180)
            try:
                result["playwright"][eng] = json.loads(p.stdout.strip().splitlines()[-1])
            except (ValueError, IndexError):
                result["playwright"][eng] = {"error": p.stderr[-500:]}
    finally:
        server.stop()
    out_dir = os.path.join(ROOT, "results", "raw", args.label)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "capabilities.json")
    with open(path, "w") as f:
        json.dump(result, f, indent=1)
    print(f"wrote {path}", file=sys.stderr)
    for key, label in CAPS:
        w = result["webframe"].get(key, {})
        pw = {e: v.get(key, {}).get("status") for e, v in result["playwright"].items()}
        print(f"{label:38s} webframe={w.get('status'):<11s} playwright={pw}")


if __name__ == "__main__":
    main()
