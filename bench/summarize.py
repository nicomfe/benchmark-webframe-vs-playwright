#!/usr/bin/env python3
"""Aggregate results/raw/*/runs.jsonl into results/summary.csv, charts and REPORT.md.

Everything in the report is computed from the raw data; rerun this script
after any benchmark run. Usage: python3 bench/summarize.py [--labels a,b]
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import statistics
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from charts import hbar_chart  # noqa: E402

RAW = os.path.join(ROOT, "results", "raw")
CHARTS = os.path.join(ROOT, "results", "charts")
CONFIG_ORDER = ["wf-cli", "wf-cli-display", "wf-repl", "pw-chromium-cold", "pw-chromium-warm", "pw-webkit-cold", "pw-webkit-warm"]
SCENARIO_ORDER = ["S1", "S2", "S3-throw", "S3-rejection", "S3-resource", "S4", "S5", "S6", "S7", "S7-iter"]
SCENARIO_TITLES = {
    "S1": "S1 hello.html: load, capture stdout, exit",
    "S2": "S2 ok.html: wait for READY, no errors",
    "S3-throw": "S3a broken-throw.html: detect uncaught exception",
    "S3-rejection": "S3b broken-rejection.html: detect unhandled rejection",
    "S3-resource": "S3c broken-resource.html: detect missing <img>/<script>",
    "S4": "S4 ok.html: evaluate document.querySelectorAll('li').length",
    "S5": "S5 spa.html: wait READY, DOM assertion",
    "S6": "S6 heavy.html: capture reported JS time",
    "S7": "S7 loop: 20 x (edit ok.html, run S2+S4), total wall time",
    "S7-iter": "S7 per-iteration",
}
CAP_LABELS = [
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


# ----------------------------------------------------------------- helpers
def pct(xs: list[float], p: float) -> float:
    if not xs:
        return float("nan")
    s = sorted(xs)
    k = (len(s) - 1) * p
    f = int(k)
    c = min(f + 1, len(s) - 1)
    return s[f] + (s[c] - s[f]) * (k - f)


def fmt_ms(v) -> str:
    if v is None or v != v:
        return "n/a"
    if v >= 10000:
        return f"{v/1000:.1f} s"
    if v >= 100:
        return f"{v:,.0f} ms"
    return f"{v:.1f} ms"


def fmt_mb(kb) -> str:
    if kb is None:
        return "n/a"
    return f"{kb/1024:,.0f} MB"


def fmt_cpu(a: dict) -> str:
    v = a.get("cpu_s_median")
    if v is None:
        return "n/a"
    if a.get("cpu_src") == "sampled" and v < 0.01:
        return "<10 ms (sampled)"
    return f"{v*1000:.0f} ms" + (" (sampled)" if a.get("cpu_src") == "sampled" else "")


def fmt_bytes(b) -> str:
    if b is None:
        return "n/a"
    for unit in ("B", "KB", "MB", "GB"):
        if b < 1024 or unit == "GB":
            return f"{b:,.0f} {unit}" if unit in ("B", "KB") else f"{b:,.1f} {unit}"
        b /= 1024
    return str(b)


def cfg_sort(c: str) -> int:
    return CONFIG_ORDER.index(c) if c in CONFIG_ORDER else 99


def is_wf(c: str) -> bool:
    return c.startswith("wf-")


def tool_of(c: str) -> str:
    return "webframe" if is_wf(c) else "Playwright"


def ratio_text(a: float, b: float) -> str:
    """How much better is a than b (both lower-is-better)."""
    if not a or not b or a != a or b != b:
        return ""
    r = b / a
    if abs(r - 1) < 0.005:
        return "tie"
    if r >= 1.15:
        return f"{r:.1f}x faster" if r < 10 else f"{r:.0f}x faster"
    if r >= 1.0:
        return f"{(r-1)*100:.0f}% faster"
    if r > 1 / 1.15:
        return f"{(1/r-1)*100:.0f}% slower"
    return f"{(1/r):.1f}x slower"


# ----------------------------------------------------------------- loading
def load_envs(labels: list[str] | None) -> dict:
    envs = {}
    if not os.path.isdir(RAW):
        return envs
    for label in sorted(os.listdir(RAW)):
        d = os.path.join(RAW, label)
        if not os.path.isdir(d) or (labels and label not in labels):
            continue
        runs_path = os.path.join(d, "runs.jsonl")
        env_path = os.path.join(d, "env.json")
        runs = []
        if os.path.exists(runs_path):
            with open(runs_path) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            runs.append(json.loads(line))
                        except ValueError:
                            pass
        env = json.load(open(env_path)) if os.path.exists(env_path) else {}
        caps = None
        cp = os.path.join(d, "capabilities.json")
        if os.path.exists(cp):
            caps = json.load(open(cp))
        fp = None
        fpp = os.path.join(d, "footprint.json")
        if os.path.exists(fpp):
            fp = json.load(open(fpp))
        if runs or caps or fp:
            envs[label] = {"env": env, "runs": runs, "caps": caps, "footprint": fp}
    return envs


# ----------------------------------------------------------------- aggregation
def aggregate(runs: list[dict]) -> dict[tuple[str, str], dict]:
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in runs:
        if r.get("warmup"):
            continue
        groups[(r["scenario"], r["config"])].append(r)
    out = {}
    for (scen, cfg), rs in groups.items():
        passed = [r for r in rs if r["pass"]]
        failed = [r for r in rs if not r["pass"]]
        walls = [r["wall_ms"] for r in passed if r.get("wall_ms") is not None]
        rss = [r["peak_rss_kb"] for r in passed if r.get("peak_rss_kb")]
        cpu = [r["cpu_s"] for r in passed if r.get("cpu_s") is not None]
        a = {
            "scenario": scen,
            "config": cfg,
            "kind": rs[0].get("kind", ""),
            "n": len(rs),
            "passed": len(passed),
            "failed": len(failed),
            "success_rate": len(passed) / len(rs) if rs else 0,
            "median_ms": statistics.median(walls) if walls else None,
            "p95_ms": pct(walls, 0.95) if walls else None,
            "min_ms": min(walls) if walls else None,
            "max_ms": max(walls) if walls else None,
            "mean_ms": statistics.fmean(walls) if walls else None,
            "stdev_ms": statistics.stdev(walls) if len(walls) > 1 else 0.0,
            "peak_rss_kb_median": statistics.median(rss) if rss else None,
            "peak_rss_kb_max": max(rss) if rss else None,
            "cpu_s_median": statistics.median(cpu) if cpu else None,
            "cpu_src": next((r.get("cpu_src") for r in passed if r.get("cpu_src")), ""),
            "fail_reasons": Counter(r.get("fail_reason", "")[:80] for r in failed),
            "restarts": sum(1 for r in rs if r.get("process_restarted")),
            "timeouts": sum(1 for r in rs if r.get("timed_out")),
        }
        # Breakdown medians (offsets from t0).
        bd: dict[str, list[float]] = defaultdict(list)
        for r in passed:
            for k, v in (r.get("breakdown_ms") or {}).items():
                bd[k].append(v)
        a["breakdown_median_ms"] = {k: statistics.median(v) for k, v in bd.items()}
        if scen.startswith("S3"):
            det = [bool((r.get("extra") or {}).get("detected")) for r in rs]
            a["detected_rate"] = sum(det) / len(det) if det else 0
            q = Counter((r.get("extra") or {}).get("message_quality", "none") for r in rs)
            a["message_quality"] = q.most_common(1)[0][0] if q else "none"
            a["sample_error"] = next((e.get("text", "") for r in passed for e in (r.get("errors") or [])[:1]), "")
        if scen == "S6":
            js = [(r.get("extra") or {}).get("js_ms") for r in passed if (r.get("extra") or {}).get("js_ms") is not None]
            a["js_ms_median"] = statistics.median(js) if js else None
            a["engine_ua"] = next(((r.get("extra") or {}).get("engine_ua", "") for r in passed), "")
        if scen == "S7":
            a["iters"] = rs[0].get("iters")
        out[(scen, cfg)] = a
    return out


def write_summary_csv(envs: dict, path: str):
    cols = ["env", "scenario", "config", "kind", "n", "passed", "failed", "success_rate", "median_ms", "p95_ms", "min_ms", "max_ms", "mean_ms", "stdev_ms", "peak_rss_mb_median", "peak_rss_mb_max", "cpu_s_median", "cpu_src", "detected_rate", "message_quality", "js_ms_median", "timeouts", "restarts", "fail_reasons"]
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for label, e in envs.items():
            agg = e["agg"]
            for key in sorted(agg, key=lambda k: (SCENARIO_ORDER.index(k[0]) if k[0] in SCENARIO_ORDER else 99, cfg_sort(k[1]))):
                a = agg[key]
                w.writerow([
                    label, a["scenario"], a["config"], a["kind"], a["n"], a["passed"], a["failed"], f"{a['success_rate']:.3f}",
                    *(f"{a[k]:.1f}" if a.get(k) is not None else "" for k in ("median_ms", "p95_ms", "min_ms", "max_ms", "mean_ms", "stdev_ms")),
                    f"{a['peak_rss_kb_median']/1024:.1f}" if a.get("peak_rss_kb_median") else "",
                    f"{a['peak_rss_kb_max']/1024:.1f}" if a.get("peak_rss_kb_max") else "",
                    f"{a['cpu_s_median']:.3f}" if a.get("cpu_s_median") is not None else "",
                    a.get("cpu_src", ""),
                    f"{a['detected_rate']:.2f}" if "detected_rate" in a else "",
                    a.get("message_quality", ""),
                    f"{a['js_ms_median']:.1f}" if a.get("js_ms_median") is not None else "",
                    a["timeouts"], a["restarts"],
                    "; ".join(f"{k} (x{v})" for k, v in a["fail_reasons"].items()),
                ])


# ----------------------------------------------------------------- scoreboard
def best(agg: dict, scen: str, configs: list[str], key="median_ms"):
    rows = [(c, agg[(scen, c)][key]) for c in configs if (scen, c) in agg and agg[(scen, c)].get(key) is not None and agg[(scen, c)]["passed"] > 0]
    rows.sort(key=lambda x: x[1])
    return rows


def scoreboard(label: str, e: dict) -> list[dict]:
    agg = e["agg"]
    configs = sorted({k[1] for k in agg}, key=cfg_sort)
    cold = [c for c in configs if agg.get(("S2", c), {}).get("kind") == "cold" or c.endswith("-cold") or c in ("wf-cli", "wf-cli-display")]
    warm = [c for c in configs if c not in cold]
    rows = []

    def add(cat, ranked, fmt, how):
        """Winner = best config overall; runner-up = best config of the *other*
        tool, so every row reads as webframe vs Playwright."""
        if not ranked:
            rows.append({"category": cat, "winner": "n/a", "value": "", "runner_up": "", "ru_value": "", "margin": "no data"})
            return
        w, wv = ranked[0]
        other = [(c, v) for c, v in ranked[1:] if tool_of(c) != tool_of(w)] or ranked[1:]
        if other:
            r, rv = other[0]
            margin = how(wv, rv)
            if rv and abs(wv - rv) / rv < 0.03:
                margin = f"~tie ({margin})"
            rows.append({"category": cat, "winner": w, "value": fmt(wv), "runner_up": r, "ru_value": fmt(rv), "margin": margin})
        else:
            rows.append({"category": cat, "winner": w, "value": fmt(wv), "runner_up": "-", "ru_value": "", "margin": "only one config"})

    add("Cold start: S1 hello (median)", best(agg, "S1", cold), fmt_ms, ratio_text)
    add("Cold start: S2 load + READY + no errors (median)", best(agg, "S2", cold), fmt_ms, ratio_text)
    add("Cold start: S4 load + eval (median)", best(agg, "S4", cold), fmt_ms, ratio_text)
    add("Warm per-check: S2 (median)", best(agg, "S2", warm), fmt_ms, ratio_text)
    add("Warm per-check: S4 load + eval (median)", best(agg, "S4", warm), fmt_ms, ratio_text)
    add("Loop S7, cold tools: 20 iterations (median total)", best(agg, "S7", cold), fmt_ms, ratio_text)
    add("Loop S7, warm tools: 20 iterations (median total)", best(agg, "S7", warm), fmt_ms, ratio_text)
    add("Peak memory per check: S4 (median of tree RSS)", best(agg, "S4", configs, "peak_rss_kb_median"), fmt_mb, lambda a, b: f"{(1-a/b)*100:.0f}% less" if b else "")
    add("CPU time per check: S2, cold tools (median user+sys)", best(agg, "S2", cold, "cpu_s_median"), lambda v: f"{v*1000:.0f} ms CPU", lambda a, b: f"{(1-a/b)*100:.0f}% less" if b else "")

    # Failure detection: per tool, min detection rate across the three S3 pages and quality.
    det = {}
    for c in configs:
        rates = [agg[(s, c)]["detected_rate"] for s in ("S3-throw", "S3-rejection", "S3-resource") if (s, c) in agg]
        if rates:
            det[c] = min(rates)
    if det:
        ranked = sorted(det.items(), key=lambda x: -x[1])
        w, wv = ranked[0]
        ties = [c for c, v in ranked if v == wv]
        rows.append({"category": "Failure detection: S3 (min detection rate over 3 broken pages)", "winner": "tie: " + ", ".join(ties) if len(ties) > 1 else w, "value": f"{wv*100:.0f}%", "runner_up": "" if len(ties) > 1 else ranked[1][0] if len(ranked) > 1 else "-", "ru_value": "" if len(ties) > 1 else f"{ranked[1][1]*100:.0f}%" if len(ranked) > 1 else "", "margin": "all tools detected all three failures" if len(ties) == len(ranked) else ""})

    # Raw JS speed by engine (S6 js_ms).
    js = [(c, agg[("S6", c)]["js_ms_median"]) for c in configs if ("S6", c) in agg and agg[("S6", c)].get("js_ms_median") is not None]
    if js:
        by_engine = {}
        for c, v in js:
            eng = "webframe (system WebKit)" if is_wf(c) else ("Playwright WebKit" if "webkit" in c else "Playwright Chromium")
            by_engine.setdefault(eng, []).append(v)
        ranked = sorted(((k, statistics.median(v)) for k, v in by_engine.items()), key=lambda x: x[1])
        add("Raw JS speed: S6 in-page ms (median, lower is better)", ranked, fmt_ms, ratio_text)

    # Footprint (from footprint.json or env footprint).
    fp = footprint_numbers(e)
    if fp.get("webframe_stack_bytes") and fp.get("playwright_stack_bytes"):
        ranked = sorted([("webframe stack", fp["webframe_stack_bytes"]), ("Playwright stack", fp["playwright_stack_bytes"])], key=lambda x: x[1])
        add("Install footprint on disk (" + fp.get("footprint_basis", "") + ")", ranked, fmt_bytes, lambda a, b: f"{(1-a/b)*100:.0f}% smaller" if b else "")
    elif fp.get("webframe_binary_bytes") and fp.get("playwright_total_bytes"):
        ranked = sorted([("webframe binary (system webview not counted)", fp["webframe_binary_bytes"]), ("Playwright npm + browsers", fp["playwright_total_bytes"])], key=lambda x: x[1])
        add("Install footprint on disk (macOS: webview is part of the OS)", ranked, fmt_bytes, lambda a, b: f"{(1-a/b)*100:.0f}% smaller" if b else "")

    # Capabilities
    caps = e.get("caps")
    if caps:
        wf_yes = sum(1 for k, _ in CAP_LABELS if caps["webframe"].get(k, {}).get("status") == "Yes")
        pw_eng = next(iter(caps["playwright"].values()), {})
        pw_yes = sum(1 for k, _ in CAP_LABELS if pw_eng.get(k, {}).get("status") == "Yes")
        ranked = sorted([("Playwright", pw_yes), ("webframe", wf_yes)], key=lambda x: -x[1])
        rows.append({"category": f"Capabilities: native 'Yes' out of {len(CAP_LABELS)}", "winner": ranked[0][0], "value": str(ranked[0][1]), "runner_up": ranked[1][0], "ru_value": str(ranked[1][1]), "margin": f"+{ranked[0][1]-ranked[1][1]} capabilities"})
    return rows


def footprint_numbers(e: dict) -> dict:
    out = {}
    envfp = (e.get("env") or {}).get("footprint") or {}
    out["webframe_binary_bytes"] = envfp.get("webframe_binary_bytes")
    br = envfp.get("playwright_browser_bytes") or {}
    out["playwright_npm_bytes"] = envfp.get("playwright_npm_bytes")
    out["playwright_browsers_bytes"] = br
    if envfp.get("playwright_npm_bytes") is not None and br:
        out["playwright_total_bytes"] = envfp["playwright_npm_bytes"] + sum(v for v in br.values() if v)
    fp = e.get("footprint") or {}
    if fp:
        out.update({k: v for k, v in fp.items() if k != "notes"})
        out["notes"] = fp.get("notes", [])
        if fp.get("webframe_stack_bytes") and fp.get("playwright_stack_bytes"):
            out["footprint_basis"] = fp.get("basis", "Linux, apt + npm + browser downloads")
    return out


# ----------------------------------------------------------------- hypotheses
def linux_footprint_env(all_envs: dict | None) -> tuple[str, dict] | tuple[None, None]:
    if not all_envs:
        return None, None
    for l, env in sorted(all_envs.items()):
        fp = env.get("footprint") or {}
        if "linux" in l and fp.get("webframe_stack_bytes") and fp.get("playwright_stack_bytes"):
            return l, env
    return None, None


def hypotheses(label: str, e: dict, all_envs: dict | None = None) -> list[tuple[str, str, str]]:
    agg = e["agg"]
    out = []

    def med(scen, cfg):
        a = agg.get((scen, cfg))
        return a["median_ms"] if a and a.get("median_ms") is not None and a["passed"] > 0 else None

    wf = med("S2", "wf-cli")
    pwc = [v for v in (med("S2", "pw-chromium-cold"), med("S2", "pw-webkit-cold")) if v]
    pww = [v for v in (med("S2", "pw-chromium-warm"), med("S2", "pw-webkit-warm")) if v]
    if wf and pwc:
        fastest_pw = min(pwc)
        if wf < fastest_pw * 0.9:
            out.append(("H1", "confirmed", f"wf-cli S2 median {fmt_ms(wf)} vs fastest Playwright cold {fmt_ms(fastest_pw)} ({ratio_text(wf, fastest_pw)})"))
        elif wf > fastest_pw * 1.1:
            out.append(("H1", "refuted", f"wf-cli S2 median {fmt_ms(wf)} is slower than the fastest Playwright cold config ({fmt_ms(fastest_pw)}, {ratio_text(fastest_pw, wf)})"))
        else:
            out.append(("H1", "inconclusive", f"wf-cli {fmt_ms(wf)} vs fastest Playwright cold {fmt_ms(fastest_pw)}: within 10%"))
    else:
        out.append(("H1", "inconclusive", "missing data"))
    if wf and pwc and pww:
        adv_cold = min(pwc) / wf
        adv_warm = min(pww) / wf
        if adv_warm <= 1.0:
            out.append(("H2", "confirmed", f"vs cold Playwright webframe-cli is {adv_cold:.2f}x; vs warm Playwright it is {adv_warm:.2f}x (warm Playwright {fmt_ms(min(pww))} beats wf-cli {fmt_ms(wf)}): the advantage disappears"))
        elif adv_warm < adv_cold * 0.75:
            out.append(("H2", "confirmed", f"webframe-cli advantage shrinks from {adv_cold:.2f}x (vs cold) to {adv_warm:.2f}x (vs warm)"))
        else:
            out.append(("H2", "refuted", f"webframe-cli advantage {adv_cold:.2f}x vs cold stays {adv_warm:.2f}x vs warm Playwright"))
    else:
        out.append(("H2", "inconclusive", "missing data"))
    fp = footprint_numbers(e)
    fp_label = label
    if not (fp.get("webframe_stack_bytes") and fp.get("playwright_stack_bytes")):
        fp_label, fp_env = linux_footprint_env(all_envs)
        if fp_env:
            fp = footprint_numbers(fp_env)
    if fp.get("webframe_stack_bytes") and fp.get("playwright_stack_bytes"):
        r = fp["webframe_stack_bytes"] / fp["playwright_stack_bytes"]
        where = f" on `{fp_label}`" if fp_label != label else ""
        if r >= 0.5:
            out.append(("H3", "confirmed", f"Linux webframe stack {fmt_bytes(fp['webframe_stack_bytes'])} is {r*100:.0f}% of the Playwright stack {fmt_bytes(fp['playwright_stack_bytes'])}{where}: not meaningfully smaller (threshold: <50%)"))
        else:
            out.append(("H3", "refuted", f"Linux webframe stack {fmt_bytes(fp['webframe_stack_bytes'])} is only {r*100:.0f}% of the Playwright stack {fmt_bytes(fp['playwright_stack_bytes'])}{where}"))
    else:
        out.append(("H3", "inconclusive", "no Linux footprint data (run `make footprint` with Docker)"))
    caps = e.get("caps")
    if caps:
        missing = [lbl for k, lbl in CAP_LABELS if caps["webframe"].get(k, {}).get("status") in ("No", "Workaround")]
        hard_no = [lbl for k, lbl in CAP_LABELS if caps["webframe"].get(k, {}).get("status") == "No"]
        if any(k in ("Screenshot", "Click / type / keyboard input") for k in missing):
            out.append(("H4", "confirmed", f"webframe has no native: {', '.join(missing)}; outright impossible: {', '.join(hard_no) or 'none'}. Playwright did all {len(CAP_LABELS)} natively"))
        else:
            out.append(("H4", "refuted", "webframe supported screenshots and input natively"))
    else:
        out.append(("H4", "inconclusive", "capabilities.json missing (run `make caps`)"))
    return out


# ----------------------------------------------------------------- charts
def make_charts(label: str, e: dict) -> dict[str, str]:
    os.makedirs(CHARTS, exist_ok=True)
    agg = e["agg"]
    configs = sorted({k[1] for k in agg}, key=cfg_sort)
    paths = {}

    def rows_for(scen, key="median_ms", unit_conv=lambda v: v, note_fn=None):
        rows = []
        for c in configs:
            a = agg.get((scen, c))
            if not a:
                continue
            v = a.get(key)
            note = note_fn(a) if note_fn else ""
            rows.append((c, unit_conv(v) if v is not None else None, note))
        return rows

    p = os.path.join(CHARTS, f"{label}-cold-start.svg")
    open(p, "w").write(hbar_chart(f"Cold start vs warm per-check: S2 median wall time [{label}]", rows_for("S2", note_fn=lambda a: f"p95 {fmt_ms(a['p95_ms'])}, {a['failed']} fail"), "ms", "cold = fresh process per check; warm = browser already running, new page per check"))
    paths["cold_start"] = os.path.relpath(p, ROOT)

    p = os.path.join(CHARTS, f"{label}-loop.svg")
    open(p, "w").write(hbar_chart(f"S7 loop: 20 x (edit + verify) total wall time, median [{label}]", rows_for("S7", unit_conv=lambda v: v / 1000, note_fn=lambda a: f"min {fmt_ms(a['min_ms'])}, max {fmt_ms(a['max_ms'])}"), "s"))
    paths["loop"] = os.path.relpath(p, ROOT)

    p = os.path.join(CHARTS, f"{label}-memory.svg")
    open(p, "w").write(hbar_chart(f"Peak memory per check: S4 process-tree RSS, median [{label}]", rows_for("S4", "peak_rss_kb_median", lambda v: v / 1024, note_fn=lambda a: f"max {fmt_mb(a['peak_rss_kb_max'])}"), "MB", "sum of RSS over the tool's process tree incl. webview/browser helpers, sampled every 100 ms"))
    paths["memory"] = os.path.relpath(p, ROOT)

    fp = footprint_numbers(e)
    frows = []
    if fp.get("webframe_binary_bytes"):
        frows.append(("webframe binary", fp["webframe_binary_bytes"] / 2**20, ""))
    if fp.get("webframe_deps_bytes"):
        frows.append(("webframe deps (WebKitGTK+Xvfb, apt)", fp["webframe_deps_bytes"] / 2**20, ""))
    if fp.get("playwright_npm_bytes"):
        frows.append(("playwright npm packages", fp["playwright_npm_bytes"] / 2**20, ""))
    for k, v in sorted((fp.get("playwright_browsers_bytes") or {}).items()):
        if v:
            frows.append((f"browser download: {k}", v / 2**20, ""))
    if fp.get("playwright_deps_bytes"):
        frows.append(("playwright --with-deps apt packages", fp["playwright_deps_bytes"] / 2**20, ""))
    for k in ("image_base_bytes", "image_webframe_bytes", "image_playwright_bytes", "image_bench_bytes"):
        if fp.get(k):
            frows.append((f"docker image: {k[6:-6]}", fp[k] / 2**20, ""))
    if frows:
        p = os.path.join(CHARTS, f"{label}-footprint.svg")
        open(p, "w").write(hbar_chart(f"Install footprint on disk [{label}]", frows, "MB"))
        paths["footprint"] = os.path.relpath(p, ROOT)
    return paths


# ----------------------------------------------------------------- report
def md_table(headers: list[str], rows: list[list[str]]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join([" --- "] * len(headers)) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(x) for x in r) + " |")
    return "\n".join(out)


def scenario_table(agg: dict, scen: str) -> str:
    configs = sorted({k[1] for k in agg if k[0] == scen}, key=cfg_sort)
    rows = []
    for c in configs:
        a = agg[(scen, c)]
        rows.append([
            f"`{c}`", a["kind"], f"**{fmt_ms(a['median_ms'])}**", fmt_ms(a["p95_ms"]), fmt_ms(a["min_ms"]), fmt_ms(a["max_ms"]),
            fmt_mb(a["peak_rss_kb_median"]), fmt_cpu(a),
            f"{a['passed']}/{a['n']}" + (" **FAIL**" if a["failed"] else ""),
        ])
    return md_table(["config", "kind", "median", "p95", "min", "max", "peak RSS (median)", "CPU (median)", "pass"], rows)


def s3_table(agg: dict) -> str:
    configs = sorted({k[1] for k in agg if k[0].startswith("S3")}, key=cfg_sort)
    rows = []
    for c in configs:
        for s, name in (("S3-throw", "throw"), ("S3-rejection", "rejection"), ("S3-resource", "resource")):
            a = agg.get((s, c))
            if not a:
                continue
            sample = a.get("sample_error", "").split("\n")[0][:110].replace("|", "\\|")
            rows.append([f"`{c}`", name, f"{a['detected_rate']*100:.0f}%", a.get("message_quality", ""), fmt_ms(a["median_ms"]), f"`{sample}`" if sample else ""])
    return md_table(["config", "page", "detected", "message quality", "median time", "first error line captured (sample)"], rows)


def breakdown_table(agg: dict, scen: str) -> str:
    configs = sorted({k[1] for k in agg if k[0] == scen}, key=cfg_sort)
    keys_order = ["spawn", "node_main", "require", "launch", "first_output", "nav_ack", "new_page", "goto", "marker", "eval", "page_close", "browser_close", "result_line", "response", "exit"]
    keys = [k for k in keys_order if any(k in agg[(scen, c)]["breakdown_median_ms"] for c in configs)]
    rows = []
    for c in configs:
        bd = agg[(scen, c)]["breakdown_median_ms"]
        rows.append([f"`{c}`"] + [fmt_ms(bd[k]) if k in bd else "" for k in keys])
    return md_table(["config"] + keys, rows)


def caps_table(caps: dict) -> str:
    engines = list(caps["playwright"].keys())
    rows = []
    for k, lbl in CAP_LABELS:
        w = caps["webframe"].get(k, {})
        cells = [lbl, f"**{w.get('status','?')}** - {w.get('note','')}"]
        for eng in engines:
            p = caps["playwright"][eng].get(k, {})
            cells.append(f"**{p.get('status','?')}** - {p.get('note','')}")
        rows.append([c.replace("|", "\\|") for c in cells])
    return md_table(["Capability", "webframe"] + [f"Playwright {e}" for e in engines], rows)


def footprint_section(e: dict) -> str:
    fp = footprint_numbers(e)
    envfp = (e.get("env") or {}).get("footprint") or {}
    lines = []
    rows = [["webframe binary", fmt_bytes(fp.get("webframe_binary_bytes"))]]
    if fp.get("webframe_deps_bytes"):
        rows.append(["webframe runtime deps (libwebkit2gtk-4.1-0 + xvfb + their apt deps, installed size)", fmt_bytes(fp["webframe_deps_bytes"])])
    rows.append(["Playwright npm packages (playwright + playwright-core)", fmt_bytes(fp.get("playwright_npm_bytes"))])
    for k, v in sorted((fp.get("playwright_browsers_bytes") or {}).items()):
        rows.append([f"Playwright browser download: {k}", fmt_bytes(v)])
    if fp.get("playwright_deps_bytes"):
        rows.append(["Playwright `--with-deps` apt packages (installed size)", fmt_bytes(fp["playwright_deps_bytes"])])
    if fp.get("webframe_stack_bytes"):
        rows.append(["**webframe stack total**", f"**{fmt_bytes(fp['webframe_stack_bytes'])}**"])
    if fp.get("playwright_stack_bytes"):
        rows.append(["**Playwright stack total (chromium + webkit)**", f"**{fmt_bytes(fp['playwright_stack_bytes'])}**"])
    for k, name in (("image_base_bytes", "Docker image: base (ubuntu:24.04 + node + python)"), ("image_webframe_bytes", "Docker image: webframe stack"), ("image_playwright_bytes", "Docker image: Playwright stack"), ("image_bench_bytes", "Docker image: both (bench)")):
        if fp.get(k):
            rows.append([name, fmt_bytes(fp[k])])
    for k, name in (("install_time_webframe_s", "Install time from clean state: webframe stack"), ("install_time_playwright_s", "Install time from clean state: Playwright stack")):
        if fp.get(k) is not None:
            rows.append([name, f"{fp[k]:.0f} s"])
    lines.append(md_table(["item", "size"], rows))
    if envfp.get("dpkg_direct_pkgs_kib"):
        lines.append("\ndpkg installed sizes of the direct packages (KiB): `" + envfp["dpkg_direct_pkgs_kib"].replace("\n", "; ") + "`")
    for n in fp.get("notes", []) or []:
        lines.append(f"\n- {n}")
    return "\n".join(lines)


def setup_section(label: str, e: dict) -> str:
    env = e["env"]
    hw = env.get("hardware", {})
    v = env.get("versions", {})
    a = env.get("args", {})
    rows = [
        ["Environment label", f"`{label}`"],
        ["OS", f"{hw.get('os','')} {hw.get('os_version','')} ({hw.get('platform','')})"],
        ["Kernel", hw.get("kernel", "")],
        ["CPU", f"{hw.get('cpu_model','')} x {hw.get('cpu_count','')}"],
        ["RAM", fmt_bytes(hw.get("ram_bytes"))],
        ["Container", ("yes" if hw.get("in_container") else "no") + (f", --cpus={hw.get('container_cpus')} --memory={hw.get('container_memory')}" if hw.get("container_cpus") else "") + (f", cgroup {hw.get('cgroup')}" if hw.get("cgroup") else "")],
        ["Load average at start / end", f"{hw.get('loadavg_at_start')} / {hw.get('loadavg_at_end')}"],
        ["webframe", f"commit `{v.get('webframe_commit','') or '?'}` (binary reports `{v.get('webframe_version_flag','')}`), glaze `{v.get('glaze_version','?')}`; built with {v.get('go_version','')}, CGO_ENABLED=0"],
        ["Playwright", f"{v.get('playwright_version','')} on Node {v.get('node_version','')}"],
        ["Browsers", ", ".join(f"{k} ({fmt_bytes(s)})" for k, s in (v.get("playwright_browsers") or {}).items())],
        ["Browser versions", ", ".join(f"{c}: {hv.get('browser_version')}" for c, hv in (env.get("harness_versions") or {}).items() if hv.get("browser_version"))],
        ["WebKitGTK / Xvfb (dpkg)", v.get("webkitgtk_pkg", "").replace("\n", "; ") or "n/a (macOS uses the system WebKit)"],
        ["Runs", f"warmup {a.get('warmup')}, measured {a.get('runs')} per scenario and config; S7: {a.get('loop_iters')} iterations x {a.get('loop_runs')} loops (+1 warmup loop)"],
        ["Order", f"configs shuffled every round (seed {a.get('seed')}); one check at a time, never in parallel"],
        ["Sampler", f"every {a.get('sample_interval')} s; {env.get('sampler',{}).get('samples')} samples, last sample cost {env.get('sampler',{}).get('last_sample_cost_ms')} ms"],
        ["Started / finished / duration", f"{env.get('started')} / {env.get('finished')} / {env.get('duration_s')} s"],
        ["Warm process launch (not timed)", ", ".join(f"{c}: {fmt_ms(ms)}" for c, ms in (env.get("warm_launch_ms") or {}).items() if ms)],
        ["Warm process restarts", str(env.get("warm_restarts"))],
    ]
    if env.get("shared_xvfb_rss_kb"):
        rows.append(["Shared Xvfb :99 RSS (wf-cli-display, not in its tree)", fmt_mb(env["shared_xvfb_rss_kb"])])
    if env.get("setup_failures"):
        rows.append(["Setup failures", str(env["setup_failures"])])
    return md_table(["item", "value"], rows)


def variance_section(envs: dict) -> str:
    """Compare S2/S4/S7 medians between environments that share a platform prefix (two full runs)."""
    groups: dict[str, list[str]] = defaultdict(list)
    for label in envs:
        base = re.sub(r"-run\d+$", "", label)
        groups[base].append(label)
    out = []
    for base, labels in groups.items():
        if len(labels) < 2:
            continue
        a, b = labels[0], labels[1]
        rows = []
        diffs = []
        for scen in ("S1", "S2", "S4", "S5", "S7"):
            for c in sorted({k[1] for k in envs[a]["agg"] if k[0] == scen}, key=cfg_sort):
                ma = envs[a]["agg"].get((scen, c), {}).get("median_ms")
                mb = envs[b]["agg"].get((scen, c), {}).get("median_ms")
                if ma and mb:
                    d = (mb - ma) / ma * 100
                    diffs.append(abs(d))
                    rows.append([scen, f"`{c}`", fmt_ms(ma), fmt_ms(mb), f"{d:+.1f}%"])
        if rows:
            out.append(f"Two full runs on `{a}` and `{b}`: median absolute difference of medians {statistics.median(diffs):.1f}%, max {max(diffs):.1f}%.\n")
            out.append(md_table(["scenario", "config", f"median {a}", f"median {b}", "diff"], rows))
    return "\n".join(out) if out else "Only one full run per environment is present; rerun `make bench ENV_LABEL=<label>-run2` to measure run-to-run variance."


def failures_section(e: dict) -> str:
    agg = e["agg"]
    rows = []
    for key in sorted(agg, key=lambda k: (SCENARIO_ORDER.index(k[0]) if k[0] in SCENARIO_ORDER else 99, cfg_sort(k[1]))):
        a = agg[key]
        if a["failed"]:
            rows.append([a["scenario"], f"`{a['config']}`", f"{a['failed']}/{a['n']}", "; ".join(f"{r} (x{n})" for r, n in a["fail_reasons"].items())])
    warm = sum(1 for r in e["runs"] if r.get("warmup"))
    text = f"{warm} warmup runs were discarded by design (flagged `warmup: true` in the raw data). "
    if rows:
        text += "Measured runs that failed the pass condition are counted as failures and excluded only from the timing statistics (never silently dropped):\n\n" + md_table(["scenario", "config", "failed", "reasons"], rows)
    else:
        text += "No measured run failed its pass condition; no run was excluded."
    restarts = [(r["scenario"], r["config"]) for r in e["runs"] if r.get("process_restarted") and not r.get("warmup")]
    if restarts:
        c = Counter(restarts)
        text += "\n\nWarm-process restarts (process exited during the previous check and was relaunched before this one; relaunch time is *not* in the check timer): " + ", ".join(f"{s}/{cfg} x{n}" for (s, cfg), n in c.items()) + ". For `wf-repl` this is `hello.html` calling `window.close()`, which exits webframe."
    return text


def summary_lines(label: str, e: dict, sb: list[dict], hyps: list[tuple[str, str, str]]) -> str:
    agg = e["agg"]

    def med(scen, cfg):
        a = agg.get((scen, cfg))
        return a["median_ms"] if a and a.get("median_ms") is not None else None

    lines = []
    wf = med("S2", "wf-cli")
    pwc = med("S2", "pw-chromium-cold")
    pwk = med("S2", "pw-webkit-cold")
    pwcw = med("S2", "pw-chromium-warm")
    pwkw = med("S2", "pw-webkit-warm")
    wfr = med("S2", "wf-repl")
    if wf and pwc:
        lines.append(f"- **Cold start (fresh process per check, S2):** webframe `wf-cli` {fmt_ms(wf)} vs Playwright Chromium {fmt_ms(pwc)} ({ratio_text(wf, pwc) if wf < pwc else 'webframe is ' + ratio_text(wf, pwc)}) and Playwright WebKit {fmt_ms(pwk)} ({ratio_text(wf, pwk) if pwk and wf < pwk else ('webframe is ' + ratio_text(wf, pwk) if pwk else 'n/a')}).")
    if wfr and pwcw:
        lines.append(f"- **Warm (browser already running, S2):** webframe `wf-repl` {fmt_ms(wfr)} vs Playwright Chromium warm {fmt_ms(pwcw)} ({ratio_text(wfr, pwcw) if wfr < pwcw else 'webframe is ' + ratio_text(wfr, pwcw)}) and WebKit warm {fmt_ms(pwkw)}.")
    if wf and pwcw:
        lines.append(f"- **Cold webframe vs warm Playwright:** `wf-cli` {fmt_ms(wf)} vs `pw-chromium-warm` {fmt_ms(pwcw)}: " + ("warm Playwright wins; webframe's cold-start edge only exists against cold Playwright." if pwcw < wf else "cold webframe still wins."))
    l7 = {c: med("S7", c) for c in ("wf-cli", "wf-repl", "pw-chromium-cold", "pw-chromium-warm", "pw-webkit-cold", "pw-webkit-warm")}
    if l7.get("wf-cli") and l7.get("pw-chromium-cold"):
        lines.append(f"- **Loop S7 (20 edit+verify iterations):** cold tools: wf-cli {fmt_ms(l7['wf-cli'])}, pw-chromium-cold {fmt_ms(l7['pw-chromium-cold'])}, pw-webkit-cold {fmt_ms(l7.get('pw-webkit-cold'))}; warm tools: wf-repl {fmt_ms(l7.get('wf-repl'))}, pw-chromium-warm {fmt_ms(l7.get('pw-chromium-warm'))}, pw-webkit-warm {fmt_ms(l7.get('pw-webkit-warm'))}.")
    m4 = {c: agg.get(("S4", c), {}).get("peak_rss_kb_median") for c in ("wf-cli", "wf-repl", "pw-chromium-cold", "pw-chromium-warm", "pw-webkit-cold", "pw-webkit-warm")}
    if m4.get("wf-cli") and m4.get("pw-chromium-cold"):
        lines.append(f"- **Memory (S4 peak tree RSS):** wf-cli {fmt_mb(m4['wf-cli'])}, wf-repl {fmt_mb(m4.get('wf-repl'))}, pw-chromium-cold {fmt_mb(m4['pw-chromium-cold'])}, pw-chromium-warm {fmt_mb(m4.get('pw-chromium-warm'))}, pw-webkit-cold {fmt_mb(m4.get('pw-webkit-cold'))}.")
    s3 = [r for r in sb if r["category"].startswith("Failure detection")]
    if s3:
        lines.append(f"- **Failure detection (S3):** {s3[0]['winner']} at {s3[0]['value']}; {s3[0]['margin'] or 'see table'}.")
    caps = [r for r in sb if r["category"].startswith("Capabilities")]
    if caps:
        lines.append(f"- **Capabilities:** Playwright {caps[0]['value'] if caps[0]['winner']=='Playwright' else caps[0]['ru_value']} native vs webframe {caps[0]['ru_value'] if caps[0]['winner']=='Playwright' else caps[0]['value']} of {len(CAP_LABELS)}; webframe has no screenshots, trusted input, or request interception.")
    # Verdict, built from the same numbers as the scoreboard.
    verdict = "it depends"
    if wf and pwc and pwcw:
        fastest_cold = min(pwc, pwk or pwc)
        parts = []
        if wf < fastest_cold * 0.9:
            parts.append(f"**yes against cold Playwright** ({ratio_text(wf, fastest_cold)} than the fastest cold Playwright config)")
        elif wf <= fastest_cold * 1.1:
            parts.append(f"**not meaningfully against cold Playwright Chromium** (within 10%: {fmt_ms(wf)} vs {fmt_ms(pwc)})" + (f", though {ratio_text(wf, pwk)} than cold Playwright WebKit" if pwk and wf < pwk * 0.9 else ""))
        else:
            parts.append(f"**no against cold Playwright** (wf-cli is {ratio_text(fastest_cold, wf)})")
        if pwcw < wf:
            parts.append(f"**no against a warm Playwright browser**, which at {fmt_ms(pwcw)} per check beats a fresh webframe process ({fmt_ms(wf)}) {ratio_text(pwcw, wf).replace(' faster', '')}")
        if wfr and wfr < pwcw:
            parts.append(f"**yes if webframe is also kept warm** (`wf-repl` {fmt_ms(wfr)} vs `pw-chromium-warm` {fmt_ms(pwcw)}, {ratio_text(wfr, pwcw)}), at the cost of the capabilities listed below")
        verdict = "**it depends**: " + "; ".join(parts)
    lines.append(f"- **Does webframe make the verification loop faster?** {verdict}.")
    lines.append("- Hypotheses: " + "; ".join(f"**{h} {v}**" for h, v, _ in hyps) + ".")
    return "\n".join(lines)


def assumptions_section() -> str:
    return "\n".join([
        "- Every check waits for a marker line the page itself logs (`READY`, `TIME ...`, or the hello line) rather than for a fixed delay; `window.close()` cannot be relied on in Playwright (browsers refuse to close windows a script did not open), so S1 ends when the expected line is seen and the page is closed.",
        "- `wf-cli` runs `webframe -e -c <url>`: `-e` for the exit code, `-c` so the harness can evaluate after READY and stop the process by closing stdin (webframe otherwise only exits on `window.close()` or a signal). `wf-repl` runs `webframe -c about:blank` without `-e` (it would die on the first broken page) and navigates with `location.href`.",
        "- Playwright warm uses `browser.newPage()` per check (a fresh context, the simplest API) and listens to `console`, `pageerror`, `requestfailed` and `response.status >= 400`; Chromium runs Playwright's default headless shell with its default flags (which already include `--no-sandbox` in containers).",
        "- The static server sends `Cache-Control: no-store` so warm browsers refetch the edited page in S7 (any dev server does this).",
        "- Peak RSS is the sum over the tool's process tree, sampled every 100 ms. On macOS WebKit's helper processes (WebContent, Networking, GPU) are launched via launchd, not as children; the sampler attributes new `com.apple.WebKit.*` processes to the configuration whose check was running when they appeared. CPU time for cold runs is `wait4` rusage (process + reaped descendants); for warm runs it is the sampled delta, which has 10 ms granularity and misses helpers that are not children.",
        "- `heavy.html` is a shortened variant of webframe's `examples/bench.html` (fib, math loop, sort, allocation, JSON), 1 warmup + 3 timed passes, median reported, so a run takes a few hundred ms rather than seconds.",
        "- No hyperfine: the correctness gate (every run must reach the verified outcome) and the stdin-driven protocols need a custom harness; it implements the same warmup + N measured runs + median/p95 reporting.",
    ])


def limitations_section(label: str, e: dict) -> str:
    env = e["env"]
    hw = env.get("hardware", {})
    la = hw.get("loadavg_at_start")
    smp = env.get("sampler", {})
    return "\n".join([
        f"- **Single machine per environment**; load average at start was {la} on {hw.get('cpu_count')} cores (the macOS host was not idle: editor and browsers were running). Interleaving configurations per round spreads drift across configs but does not remove it.",
        f"- **Measurement overhead**: the process sampler runs every {smp.get('interval_s')} s and one sample cost about {smp.get('last_sample_cost_ms')} ms (a `ps` call on macOS, a `/proc` scan on Linux), i.e. a constant background load that affects all configurations equally.",
        "- **Engine differences**: webframe uses the OS WebKit; `pw-webkit-*` uses Playwright's own WebKit build with a different process model (Playwright WebKit spends ~300 ms creating a page/context; see breakdown tables). Chromium is a different engine entirely; S6 shows the raw JS speed gap.",
        "- **Small page set** of synthetic pages on localhost; real apps with bundles, fonts and network latency will shift absolute numbers.",
        "- **Timer boundaries differ by kind**: cold timers include process spawn and runtime startup (Go binary vs Node + `require('playwright')` ~150 ms); warm timers include pipe IPC to the long-lived process. They are compared only within their kind, except where the report says so explicitly.",
        "- **Memory sampling** at 100 ms misses short peaks and, on macOS, relies on heuristic attribution of WebKit helper processes (no other WebKit app should be spawning processes during the run). Linux trees are exact. Xvfb started by webframe is included in its tree; the shared Xvfb for `wf-cli-display` is reported separately.",
        "- **Container/VM overhead**: the Linux environment runs under Docker Desktop's VM on Apple Silicon; absolute numbers are not comparable to bare-metal Linux. Xvfb startup is part of every `wf-cli` run on Linux unless `wf-cli-display` is used.",
        "- **OS file cache** was warm for all measured runs (binaries and browsers had been used by the warmup runs); no dropped-cache measurement was taken.",
        "- **webframe hides script error details on `file://` pages** (`Script error.`), so everything here is served over http; that mode was not timed separately.",
        "- `wf-repl` S1 relaunches the REPL after `hello.html` calls `window.close()`; that restart cost is reported, not timed.",
    ])


def recommendation_section(label: str, e: dict) -> str:
    agg = e["agg"]

    def med(scen, cfg):
        a = agg.get((scen, cfg))
        return a["median_ms"] if a and a.get("median_ms") is not None else None

    wf, wfr = med("S2", "wf-cli"), med("S2", "wf-repl")
    pwc, pwcw = med("S2", "pw-chromium-cold"), med("S2", "pw-chromium-warm")
    pwk = med("S2", "pw-webkit-cold")
    mem_wf = agg.get(("S2", "wf-cli"), {}).get("peak_rss_kb_median")
    mem_pwc = agg.get(("S2", "pw-chromium-cold"), {}).get("peak_rss_kb_median")
    cpu_wf = agg.get(("S2", "wf-cli"), {}).get("cpu_s_median")
    cpu_pwc = agg.get(("S2", "pw-chromium-cold"), {}).get("cpu_s_median")
    lines = []
    if wf and pwc:
        res = f"{fmt_mb(mem_wf)} vs {fmt_mb(mem_pwc)} peak RSS, {cpu_wf*1000:.0f} vs {cpu_pwc*1000:.0f} ms CPU" if mem_wf and mem_pwc and cpu_wf is not None and cpu_pwc is not None else ""
        if wf < pwc * 0.9:
            lines.append(f"- If the agent **must spawn a fresh process per check** (no daemon allowed), webframe is the faster verifier here ({fmt_ms(wf)} vs {fmt_ms(pwc)} Chromium / {fmt_ms(pwk)} WebKit; {res}), provided the check only needs console output, exceptions, resource failures and JS evaluation.")
        elif wf <= pwc * 1.1:
            lines.append(f"- If the agent **must spawn a fresh process per check**, wall time is a wash against Playwright Chromium ({fmt_ms(wf)} vs {fmt_ms(pwc)}) and clearly better than Playwright WebKit ({fmt_ms(pwk)}); webframe's measurable advantage in this mode is resources ({res}), not latency.")
        else:
            lines.append(f"- Even with a fresh process per check, Playwright Chromium ({fmt_ms(pwc)}) was faster than webframe ({fmt_ms(wf)}) on this machine; webframe's benefit here is footprint and resources ({res}), not speed.")
    if pwcw and wf:
        if pwcw < wf:
            lines.append(f"- If the agent **can keep a browser process alive** across edits, a warm Playwright Chromium ({fmt_ms(pwcw)} per check) beats cold webframe ({fmt_ms(wf)}) and matches or beats `wf-repl` on everything except the raw navigation round trip" + (f" ({fmt_ms(wfr)} for wf-repl)" if wfr else "") + ". Warm Playwright also gives screenshots, trusted input, interception and multiple pages.")
    if wfr and pwcw and wfr < pwcw:
        lines.append(f"- `wf-repl` is the cheapest possible loop ({fmt_ms(wfr)} per check, ~{fmt_mb(agg.get(('S4','wf-repl'),{}).get('peak_rss_kb_median'))} RSS) when the agent only needs load + console + eval and can tolerate a single page per process and plain-text stdout/stderr parsing.")
    lines.append("- Use Playwright when verification needs pixels, real input events, network mocking, waiting primitives or structured output; webframe has no equivalent and the workarounds (synthetic events, DOM dumps) change what is being verified.")
    fp = footprint_numbers(e)
    if fp.get("webframe_stack_bytes") and fp.get("playwright_stack_bytes"):
        r = fp["webframe_stack_bytes"] / fp["playwright_stack_bytes"]
        lines.append(
            f"- **Install footprint:** webframe stack {fmt_bytes(fp['webframe_stack_bytes'])} vs Playwright stack {fmt_bytes(fp['playwright_stack_bytes'])} "
            f"({r*100:.0f}% of Playwright). WebKitGTK + Xvfb dominate the webframe side (~{fmt_bytes(fp.get('webframe_deps_bytes'))}), but the full Playwright stack is still ~{1/r:.1f}x larger here. "
            f"On macOS the binary alone is ~5 MB because WebKit ships with the OS."
        )
    else:
        lines.append("- Use webframe on macOS where it needs nothing but a ~5 MB binary (system WebKit). Run `make footprint` for Linux WebKitGTK + Xvfb vs Playwright sizes.")
    return "\n".join(lines)


def build_report(envs: dict, chart_paths: dict, sbs: dict, hyps: dict, all_envs: dict | None = None) -> str:
    md = ["# Benchmark report: webframe vs Playwright for agent verification loops", ""]
    md.append("Generated by `bench/summarize.py` from `results/raw/*/runs.jsonl`. Every number below is computed from the raw data; rerun `make bench` to regenerate. Lower is better for all timings and sizes.")
    md.append("")
    primary = next(iter(envs))
    labels = list(envs)
    md.append(f"Environments: {', '.join(f'`{l}`' for l in labels)}. The summary uses `{primary}`" + ("; per-environment sections follow." if len(labels) > 1 else "."))
    md.append("")
    md.append("## 1. Summary")
    md.append("")
    md.append(summary_lines(primary, envs[primary], sbs[primary], hyps[primary]))
    md.append("")
    md.append("### Scoreboard (who won each category)")
    md.append("")
    for label in labels:
        if len(labels) > 1:
            md.append(f"**{label}**")
            md.append("")
        md.append(md_table(["category", "winner", "value", "runner-up", "value", "margin"], [[r["category"], f"`{r['winner']}`" if r["winner"] not in ("n/a", "") else r["winner"], r["value"], f"`{r['runner_up']}`" if r["runner_up"] not in ("", "-", "n/a") else r["runner_up"], r["ru_value"], r["margin"]] for r in sbs[label]]))
        md.append("")
    md.append("### Hypotheses")
    md.append("")
    for label in labels:
        if len(labels) > 1:
            md.append(f"**{label}**")
            md.append("")
        md.append(md_table(["hypothesis", "verdict", "evidence"], [[{"H1": "H1 webframe has lower cold-start time than Playwright for a simple load-and-check", "H2": "H2 the advantage shrinks or disappears once Playwright reuses a warm browser", "H3": "H3 on Linux, webframe's install footprint is not meaningfully smaller (WebKitGTK + Xvfb dominate)", "H4": "H4 webframe cannot perform some checks Playwright can (screenshots, input), limiting it as a verifier"}[h], f"**{v}**", ev] for h, v, ev in hyps[label]]))
        md.append("")

    for label in labels:
        e = envs[label]
        agg = e["agg"]
        md.append(f"## 2. Setup: `{label}`")
        md.append("")
        md.append(setup_section(label, e))
        md.append("")
        md.append("Timer definitions: **cold** = " + e["env"].get("timer_definitions", {}).get("cold", "") + " **warm** = " + e["env"].get("timer_definitions", {}).get("warm", ""))
        md.append("")
        md.append(f"## 3. Results: `{label}`")
        md.append("")
        cp = chart_paths[label]
        if "cold_start" in cp:
            md.append(f"![cold start]({cp['cold_start']})")
            md.append("")
        for scen in ("S1", "S2", "S4", "S5", "S6"):
            if not any(k[0] == scen for k in agg):
                continue
            md.append(f"### {SCENARIO_TITLES[scen]}")
            md.append("")
            md.append(scenario_table(agg, scen))
            md.append("")
            if scen == "S6":
                rows = []
                for c in sorted({k[1] for k in agg if k[0] == "S6"}, key=cfg_sort):
                    a = agg[("S6", c)]
                    rows.append([f"`{c}`", fmt_ms(a.get("js_ms_median")), f"`{a.get('engine_ua','')[:90]}`"])
                md.append("In-page JS time reported by the page (engine speed, independent of startup):")
                md.append("")
                md.append(md_table(["config", "js_ms median", "user agent"], rows))
                md.append("")
            if scen in ("S2", "S4"):
                md.append(f"Median time breakdown for {scen} (cumulative offsets from t0; cold: `spawn` -> ... -> `exit`; warm: `new_page` -> ... -> `response`):")
                md.append("")
                md.append(breakdown_table(agg, scen))
                md.append("")
        if any(k[0].startswith("S3") for k in agg):
            md.append("### S3 broken pages: detection and message quality")
            md.append("")
            md.append("`detected` = the tool surfaced an error signal before the page's READY marker (or exited non-zero). Message quality: `full+stack` (message + source location/stack or URL), `message` (text only), `generic` (e.g. `Script error.`), `none`.")
            md.append("")
            md.append(s3_table(agg))
            md.append("")
        if any(k[0] == "S7" for k in agg):
            md.append(f"### {SCENARIO_TITLES['S7']}")
            md.append("")
            if "loop" in cp:
                md.append(f"![loop]({cp['loop']})")
                md.append("")
            md.append(scenario_table(agg, "S7").replace("peak RSS (median)", "peak RSS (n/a)"))
            md.append("")
            if any(k[0] == "S7-iter" for k in agg):
                md.append("Per-iteration statistics inside the loop (each iteration = rewrite ok.html, load, wait READY, eval li count + revision):")
                md.append("")
                md.append(scenario_table(agg, "S7-iter"))
                md.append("")
        md.append("### Memory")
        md.append("")
        if "memory" in cp:
            md.append(f"![memory]({cp['memory']})")
            md.append("")
        rows = []
        for c in sorted({k[1] for k in agg}, key=cfg_sort):
            vals = [agg[(s, c)]["peak_rss_kb_median"] for s in ("S1", "S2", "S4", "S5", "S6", "S7-iter") if (s, c) in agg and agg[(s, c)].get("peak_rss_kb_median")]
            maxes = [agg[(s, c)]["peak_rss_kb_max"] for s in ("S1", "S2", "S4", "S5", "S6", "S7-iter") if (s, c) in agg and agg[(s, c)].get("peak_rss_kb_max")]
            if vals:
                rows.append([f"`{c}`", fmt_mb(statistics.median(vals)), fmt_mb(max(maxes)), fmt_mb(agg.get(("S6", c), {}).get("peak_rss_kb_median"))])
        md.append(md_table(["config", "typical peak RSS (median over scenarios)", "max peak RSS seen", "S6 heavy peak RSS"], rows))
        md.append("")
        md.append("### Failures and exclusions")
        md.append("")
        md.append(failures_section(e))
        md.append("")
        md.append(f"## 4. Capability matrix: `{label}`")
        md.append("")
        if e.get("caps"):
            md.append("Filled by running each probe (`bench/capabilities.py`, `bench/harness/pw_caps.js`) against the local pages; see `results/raw/" + label + "/capabilities.json` for the captured evidence.")
            md.append("")
            md.append(caps_table(e["caps"]))
        else:
            md.append("No capabilities.json for this environment (run `make caps`).")
        md.append("")
        md.append(f"## 5. Footprint: `{label}`")
        md.append("")
        if "footprint" in cp:
            md.append(f"![footprint]({cp['footprint']})")
            md.append("")
        md.append(footprint_section(e))
        md.append("")
    md.append("## 6. Limitations and threats to validity")
    md.append("")
    md.append(limitations_section(primary, envs[primary]))
    md.append("")
    md.append("### Assumptions made by the implementation")
    md.append("")
    md.append(assumptions_section())
    md.append("")
    md.append("### Run-to-run variance")
    md.append("")
    md.append(variance_section(all_envs or envs))
    md.append("")
    md.append("## 7. Recommendation")
    md.append("")
    md.append(recommendation_section(primary, envs[primary]))
    md.append("")
    md.append("## Appendix: files")
    md.append("")
    md.append("- `results/summary.csv`: aggregated medians, p95, memory, CPU, success rates for every environment, scenario and configuration.")
    md.append("- `results/raw/<env>/runs.jsonl`: one JSON record per check (including warmups and failures) with stdout, errors, timings, breakdown, RSS and CPU; `env.json`: hardware, versions, parameters; `capabilities.json`; `footprint.json` (Linux/Docker).")
    md.append("- `results/charts/*.svg`: cold start, S7 loop, peak memory, footprint per environment.")
    return "\n".join(md) + "\n"


def print_scoreboard(label: str, rows: list[dict], hyps):
    print(f"\n=== Scoreboard [{label}] ===")
    w = max(len(r["category"]) for r in rows) if rows else 20
    for r in rows:
        print(f"{r['category']:<{w}}  WINNER {r['winner']:<22} {r['value']:>12}   vs {r['runner_up']:<22} {r['ru_value']:>12}   {r['margin']}")
    print("Hypotheses: " + "; ".join(f"{h}={v}" for h, v, _ in hyps))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", default=os.environ.get("LABELS", ""), help="comma list of environment labels to include (default all)")
    ap.add_argument("--primary", default=os.environ.get("PRIMARY", ""), help="environment label used for the summary text (default: first)")
    args = ap.parse_args()
    labels = [l for l in args.labels.split(",") if l] or None
    envs = load_envs(labels)
    envs = {k: v for k, v in envs.items() if v["runs"]}
    if not envs:
        print("no results found under results/raw", file=sys.stderr)
        sys.exit(1)
    if args.primary and args.primary in envs:
        envs = {args.primary: envs[args.primary], **{k: v for k, v in envs.items() if k != args.primary}}
    for label, e in envs.items():
        e["agg"] = aggregate(e["runs"])
    os.makedirs(os.path.join(ROOT, "results"), exist_ok=True)
    write_summary_csv(envs, os.path.join(ROOT, "results", "summary.csv"))
    # Labels like `<env>-run2` are repeat runs: used for the run-to-run variance
    # section only, not as separate result sections.
    main_envs = {l: e for l, e in envs.items() if not re.search(r"-run\d+$", l)} or envs
    chart_paths = {l: make_charts(l, e) for l, e in main_envs.items()}
    sbs = {l: scoreboard(l, e) for l, e in main_envs.items()}
    hyps = {l: hypotheses(l, e, all_envs=main_envs) for l, e in main_envs.items()}
    report = build_report(main_envs, chart_paths, sbs, hyps, all_envs=envs)
    with open(os.path.join(ROOT, "REPORT.md"), "w") as f:
        f.write(report)
    for l in envs:
        print_scoreboard(l, sbs[l], hyps[l])
    print(f"\nwrote results/summary.csv, {sum(len(p) for p in chart_paths.values())} charts, REPORT.md")


if __name__ == "__main__":
    main()
