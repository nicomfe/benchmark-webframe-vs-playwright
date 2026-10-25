#!/usr/bin/env python3
"""Benchmark runner: webframe vs Playwright for agent verification checks.

Writes results/raw/<label>/runs.jsonl (one line per measured check, including
warmups and failures) and results/raw/<label>/env.json. Run
``python3 bench/summarize.py`` afterwards (``make bench`` does both).

All knobs are CLI flags; the Makefile maps RUNS=, WARMUP=, CONFIGS=, ... onto
them. See ``--help``.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import time
from dataclasses import asdict

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import envinfo  # noqa: E402
import scenarios as sc  # noqa: E402
from harness.base import Ctx, Harness  # noqa: E402
from harness.pw_cold import PwCold  # noqa: E402
from harness.pw_warm import PwWarm  # noqa: E402
from harness.wf_cli import WfCli  # noqa: E402
from harness.wf_repl import WfRepl  # noqa: E402
from procmon import Sampler  # noqa: E402
from server import StaticServer  # noqa: E402

ALL_CONFIGS = ["wf-cli", "wf-cli-display", "wf-repl", "pw-chromium-cold", "pw-chromium-warm", "pw-webkit-cold", "pw-webkit-warm"]
SHARED_DISPLAY = ":99"


def default_configs() -> list[str]:
    cfgs = ["wf-cli", "wf-repl", "pw-chromium-cold", "pw-chromium-warm", "pw-webkit-cold", "pw-webkit-warm"]
    if sys.platform.startswith("linux") and shutil.which("Xvfb"):
        cfgs.insert(1, "wf-cli-display")
    return cfgs


def build_harness(label: str, ctx: Ctx) -> Harness:
    ctx.label = label
    if label == "wf-cli":
        return WfCli(ctx)
    if label == "wf-cli-display":
        return WfCli(ctx, display=SHARED_DISPLAY)
    if label == "wf-repl":
        return WfRepl(ctx)
    if label.startswith("pw-") and label.endswith("-cold"):
        return PwCold(ctx, label[3:-5])
    if label.startswith("pw-") and label.endswith("-warm"):
        return PwWarm(ctx, label[3:-5])
    raise SystemExit(f"unknown config {label}")


def log(msg: str):
    print(time.strftime("%H:%M:%S"), msg, file=sys.stderr, flush=True)


def default_label() -> str:
    osname = {"darwin": "macos"}.get(sys.platform, sys.platform.split("-")[0] if sys.platform != "linux" else "linux")
    arch = {"x86_64": "amd64", "AMD64": "amd64", "aarch64": "arm64"}.get(os.uname().machine, os.uname().machine)
    suffix = "-docker" if os.path.exists("/.dockerenv") else ""
    return f"{osname}-{arch}{suffix}"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", type=int, default=int(os.environ.get("RUNS", 30)), help="measured runs per scenario and configuration (default 30)")
    ap.add_argument("--warmup", type=int, default=int(os.environ.get("WARMUP", 3)), help="discarded warmup runs (default 3)")
    ap.add_argument("--configs", default=os.environ.get("CONFIGS", ""), help="comma list, default: all available (" + ",".join(ALL_CONFIGS) + ")")
    ap.add_argument("--scenarios", default=os.environ.get("SCENARIOS", ""), help="comma list of scenario ids (S1,S2,S3-throw,S3-rejection,S3-resource,S4,S5,S6,S7); default all")
    ap.add_argument("--loop-runs", type=int, default=int(os.environ.get("LOOP_RUNS", 5)), help="how many times the S7 loop (LOOP_ITERS iterations) is repeated per config (default 5)")
    ap.add_argument("--loop-iters", type=int, default=int(os.environ.get("LOOP_ITERS", 20)), help="iterations per S7 loop (default 20)")
    ap.add_argument("--timeout", type=float, default=float(os.environ.get("TIMEOUT", 20)), help="per-check timeout in seconds")
    ap.add_argument("--label", default=os.environ.get("ENV_LABEL", "") or default_label(), help="environment label for results/raw/<label>")
    ap.add_argument("--out", default=os.environ.get("RAW_DIR", ""), help="output dir (default results/raw/<label>)")
    ap.add_argument("--seed", type=int, default=int(os.environ.get("SEED", 1234)))
    ap.add_argument("--sample-interval", type=float, default=float(os.environ.get("SAMPLE_INTERVAL", 0.1)), help="process sampler interval in seconds")
    ap.add_argument("--webframe", default=os.environ.get("WEBFRAME_BIN", os.path.join(ROOT, ".cache", "bin", "webframe")))
    ap.add_argument("--node", default=os.environ.get("NODE_BIN", shutil.which("node") or "node"))
    ap.add_argument("--browsers-path", default=os.environ.get("PLAYWRIGHT_BROWSERS_PATH", os.path.join(ROOT, ".cache", "ms-playwright")))
    ap.add_argument("--no-summary", action="store_true", help="do not run summarize.py at the end")
    ap.add_argument("--append", action="store_true", help="append to an existing runs.jsonl instead of replacing it")
    args = ap.parse_args()

    configs = [c.strip() for c in args.configs.split(",") if c.strip()] or default_configs()
    for c in configs:
        if c not in ALL_CONFIGS:
            raise SystemExit(f"unknown config {c}; choose from {ALL_CONFIGS}")
    if "wf-cli-display" in configs and not sys.platform.startswith("linux"):
        log("wf-cli-display is Linux-only; skipping it here")
        configs.remove("wf-cli-display")
    scen_ids = [s.strip() for s in args.scenarios.split(",") if s.strip()] or [s.id for s in sc.SCENARIOS] + [sc.LOOP_SCENARIO_ID]
    for s in scen_ids:
        if s != sc.LOOP_SCENARIO_ID and s not in sc.BY_ID:
            raise SystemExit(f"unknown scenario {s}")

    if not os.path.exists(args.webframe):
        raise SystemExit(f"webframe binary not found at {args.webframe}; run `make setup`")
    if not os.path.exists(os.path.join(ROOT, "node_modules", "playwright")):
        raise SystemExit("playwright not installed; run `make setup`")

    out_dir = args.out or os.path.join(ROOT, "results", "raw", args.label)
    os.makedirs(out_dir, exist_ok=True)
    runs_path = os.path.join(out_dir, "runs.jsonl")
    if not args.append and os.path.exists(runs_path):
        os.remove(runs_path)

    base_env = dict(os.environ)
    base_env["PLAYWRIGHT_BROWSERS_PATH"] = args.browsers_path
    base_env.setdefault("PLAYWRIGHT_SKIP_BROWSER_GC", "1")

    # Linux: shared Xvfb for wf-cli-display, started before the sampler so it
    # is excluded from every tree (its RSS is recorded separately).
    xvfb = None
    shared_xvfb_rss_kb = None
    if "wf-cli-display" in configs:
        if not shutil.which("Xvfb"):
            log("Xvfb not found; dropping wf-cli-display")
            configs.remove("wf-cli-display")
        else:
            xvfb = subprocess.Popen(["Xvfb", SHARED_DISPLAY, "-screen", "0", "1280x800x24", "-nolisten", "tcp"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(1.0)
            log(f"started shared Xvfb on {SHARED_DISPLAY} (pid {xvfb.pid})")

    env = {
        "label": args.label,
        "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "args": {k: v for k, v in vars(args).items()},
        "configs": configs,
        "scenarios": scen_ids,
        "hardware": envinfo.hardware(),
        "versions": envinfo.versions(args.webframe, args.node, ROOT, args.browsers_path),
        "footprint": envinfo.static_footprint(args.webframe, ROOT, args.browsers_path),
        "timer_definitions": {
            "cold": "t0 just before spawning the process; end when the process has exited (wait4). Includes spawn, runtime/browser startup, page load, marker wait, eval, teardown.",
            "warm": "t0 just before writing the request/navigation command to the long-lived process; end when the result line was read. Excludes browser/webview startup.",
        },
    }

    server = StaticServer(os.path.join(ROOT, "pages")).start()
    sampler = Sampler(interval=args.sample_interval)
    sampler.start()
    log(f"serving pages at {server.base_url}; sampler every {args.sample_interval}s")

    ctx_kwargs = dict(sampler=sampler, webframe_bin=args.webframe, node_bin=args.node, harness_dir=os.path.join(HERE, "harness"), base_env=base_env, timeout_s=args.timeout)
    harnesses: dict[str, Harness] = {}
    for c in configs:
        h = build_harness(c, Ctx(**ctx_kwargs))
        sampler.set_active(c)
        try:
            h.setup()
        except Exception as e:  # noqa: BLE001
            log(f"setup failed for {c}: {e}")
            env.setdefault("setup_failures", {})[c] = str(e)
            continue
        finally:
            sampler.set_active(None)
        harnesses[c] = h
        # Let helper processes spawned by the launch show up before the next config.
        time.sleep(0.3)
    env["harness_versions"] = {c: h.version_info() for c, h in harnesses.items()}
    with open(os.path.join(out_dir, "env.json"), "w") as f:
        json.dump(env, f, indent=1)

    rng = random.Random(args.seed)
    f_out = open(runs_path, "a")
    total_written = 0

    def write(rec: dict):
        nonlocal total_written
        rec["ts"] = time.time()
        rec["env"] = args.label
        f_out.write(json.dumps(rec, default=str) + "\n")
        f_out.flush()
        total_written += 1

    def run_check(label: str, scen_id: str, page: str, wait_for, eval_expr, expect, **meta) -> bool:
        h = harnesses[label]
        url = server.url(page)
        sampler.set_active(label)
        try:
            res = h.check(url, wait_for, eval_expr)
        finally:
            sampler.set_active(None)
        d = asdict(res)
        try:
            passed, reason, extra = expect(d)
        except Exception as e:  # noqa: BLE001
            passed, reason, extra = False, f"expect() raised {e!r}", {}
        if res.harness_error and passed:
            passed, reason = False, res.harness_error
        rec = {
            "scenario": scen_id,
            "page": page,
            "config": label,
            "kind": h.kind,
            "pass": passed,
            "fail_reason": reason if not passed else "",
            "extra": extra,
            **meta,
            **d,
        }
        write(rec)
        return passed

    t_bench0 = time.time()
    try:
        order = [h for h in harnesses]
        for scen_id in scen_ids:
            if scen_id == sc.LOOP_SCENARIO_ID:
                continue
            s = sc.BY_ID[scen_id]
            la = list(os.getloadavg()) if hasattr(os, "getloadavg") else None
            log(f"== {scen_id} {s.label}  (warmup {args.warmup}, runs {args.runs}, load {la})")
            fails = {c: 0 for c in order}
            for rnd in range(args.warmup + args.runs):
                cfgs = order[:]
                rng.shuffle(cfgs)
                for c in cfgs:
                    ok = run_check(c, scen_id, s.page, s.wait_for, s.eval, s.expect, round=rnd, warmup=rnd < args.warmup, load_avg=la)
                    if not ok and rnd >= args.warmup:
                        fails[c] += 1
            log("   failures: " + ", ".join(f"{c}={n}" for c, n in fails.items() if n) if any(fails.values()) else "   all passed")

        if sc.LOOP_SCENARIO_ID in scen_ids:
            template = open(os.path.join(ROOT, "pages", "ok.html")).read()
            scratch = os.path.join(ROOT, "pages", "_scratch")
            os.makedirs(scratch, exist_ok=True)
            la = list(os.getloadavg()) if hasattr(os, "getloadavg") else None
            n_loops = 1 + args.loop_runs  # 1 warmup loop
            log(f"== S7 loop simulation: {args.loop_iters} iterations x {args.loop_runs} runs (+1 warmup) per config")
            rev = 0
            for lr in range(n_loops):
                cfgs = order[:]
                rng.shuffle(cfgs)
                for c in cfgs:
                    t0 = time.perf_counter()
                    all_ok = True
                    iter_ms = []
                    for it in range(args.loop_iters):
                        rev += 1
                        with open(os.path.join(scratch, "ok.html"), "w") as f:
                            f.write(sc.ok_html_with_rev(template, rev))
                        ti = time.perf_counter()
                        ok = run_check(c, "S7-iter", sc.LOOP_PAGE, "READY", sc.LOOP_EVAL, sc.make_expect_loop(rev), loop_run=lr, warmup=lr == 0, iter=it, rev=rev, load_avg=la)
                        iter_ms.append((time.perf_counter() - ti) * 1000)
                        all_ok = all_ok and ok
                    total_ms = (time.perf_counter() - t0) * 1000
                    write({
                        "scenario": "S7", "page": sc.LOOP_PAGE, "config": c, "kind": harnesses[c].kind,
                        "pass": all_ok, "fail_reason": "" if all_ok else "one or more iterations failed",
                        "round": lr, "loop_run": lr, "warmup": lr == 0, "iters": args.loop_iters,
                        "wall_ms": total_ms, "iter_ms": iter_ms, "extra": {}, "load_avg": la,
                    })
                    log(f"   loop {lr}{' (warmup)' if lr == 0 else ''} {c}: {total_ms/1000:.2f}s {'ok' if all_ok else 'FAILED'}")
            shutil.rmtree(scratch, ignore_errors=True)
    finally:
        for c, h in harnesses.items():
            try:
                h.teardown()
            except Exception as e:  # noqa: BLE001
                log(f"teardown {c}: {e}")
        if xvfb:
            try:
                import procmon

                snap = procmon.snapshot()
                shared_xvfb_rss_kb = snap[xvfb.pid].rss_kb if xvfb.pid in snap else None
            except Exception:  # noqa: BLE001
                pass
            xvfb.terminate()
        sampler.stop()
        server.stop()
        f_out.close()

    env["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    env["duration_s"] = round(time.time() - t_bench0, 1)
    env["records"] = total_written
    env["sampler"] = {"samples": sampler.samples_taken, "last_sample_cost_ms": round(sampler.last_sample_cost_ms, 1), "interval_s": args.sample_interval}
    env["shared_xvfb_rss_kb"] = shared_xvfb_rss_kb
    env["warm_restarts"] = {c: getattr(h, "restarts", 0) for c, h in harnesses.items() if h.kind == "warm"}
    env["warm_launch_ms"] = {c: getattr(h, "launch_ms", None) for c, h in harnesses.items() if h.kind == "warm"}
    try:
        env["hardware"]["loadavg_at_end"] = list(os.getloadavg())
    except OSError:
        pass
    with open(os.path.join(out_dir, "env.json"), "w") as f:
        json.dump(env, f, indent=1)
    log(f"done: {total_written} records in {runs_path} ({env['duration_s']}s)")

    if not args.no_summary:
        subprocess.run([sys.executable, os.path.join(HERE, "summarize.py")], check=False)


if __name__ == "__main__":
    main()
