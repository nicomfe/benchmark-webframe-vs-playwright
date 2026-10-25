"""Collect environment facts for the report: hardware, OS, versions, static footprint."""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys


def _run(argv, **kw) -> str:
    try:
        return subprocess.run(argv, capture_output=True, text=True, check=False, **kw).stdout.strip()
    except (OSError, FileNotFoundError):
        return ""


def _du_bytes(path: str) -> int | None:
    if not path or not os.path.exists(path):
        return None
    total = 0
    if os.path.isfile(path):
        return os.path.getsize(path)
    for root, dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def cgroup_limits() -> dict:
    out = {}
    try:
        if os.path.exists("/sys/fs/cgroup/cpu.max"):
            out["cpu.max"] = open("/sys/fs/cgroup/cpu.max").read().strip()
        if os.path.exists("/sys/fs/cgroup/memory.max"):
            out["memory.max"] = open("/sys/fs/cgroup/memory.max").read().strip()
    except OSError:
        pass
    return out


def hardware() -> dict:
    info = {
        "os": platform.system(),
        "os_release": platform.release(),
        "platform": platform.platform(),
        "arch": platform.machine(),
        "python": platform.python_version(),
        "in_container": os.path.exists("/.dockerenv"),
    }
    if sys.platform == "darwin":
        info["cpu_model"] = _run(["sysctl", "-n", "machdep.cpu.brand_string"])
        info["cpu_count"] = int(_run(["sysctl", "-n", "hw.ncpu"]) or os.cpu_count() or 0)
        mem = _run(["sysctl", "-n", "hw.memsize"])
        info["ram_bytes"] = int(mem) if mem.isdigit() else None
        info["os_version"] = _run(["sw_vers", "-productVersion"])
        info["kernel"] = _run(["uname", "-v"])
    else:
        try:
            cpuinfo = open("/proc/cpuinfo").read()
            m = re.search(r"model name\s*:\s*(.+)", cpuinfo)
            info["cpu_model"] = m.group(1).strip() if m else (re.search(r"Hardware\s*:\s*(.+)", cpuinfo) or [None, "unknown"])[1]
        except OSError:
            info["cpu_model"] = "unknown"
        info["cpu_count"] = os.cpu_count()
        try:
            meminfo = open("/proc/meminfo").read()
            m = re.search(r"MemTotal:\s*(\d+) kB", meminfo)
            info["ram_bytes"] = int(m.group(1)) * 1024 if m else None
        except OSError:
            info["ram_bytes"] = None
        info["kernel"] = _run(["uname", "-r"])
        try:
            osr = dict(l.split("=", 1) for l in open("/etc/os-release").read().splitlines() if "=" in l)
            info["os_version"] = osr.get("PRETTY_NAME", "").strip('"')
        except OSError:
            pass
        info["cgroup"] = cgroup_limits()
    info["container_cpus"] = os.environ.get("BENCH_CONTAINER_CPUS")
    info["container_memory"] = os.environ.get("BENCH_CONTAINER_MEMORY")
    try:
        info["loadavg_at_start"] = list(os.getloadavg())
    except OSError:
        pass
    return info


def versions(webframe_bin: str, node_bin: str, repo_root: str, browsers_path: str) -> dict:
    v = {}
    v["webframe_bin"] = webframe_bin
    v["webframe_version_flag"] = _run([webframe_bin, "-v"])
    v["webframe_commit"] = os.environ.get("WEBFRAME_REF", "")
    if not v["webframe_commit"]:
        try:
            v["webframe_commit"] = open(os.path.join(repo_root, ".cache", "webframe.ref")).read().strip()
        except OSError:
            v["webframe_commit"] = v["webframe_version_flag"]
    gomod = _run(["go", "version", "-m", webframe_bin])
    v["webframe_go_buildinfo"] = [l.strip() for l in gomod.splitlines() if any(k in l for k in ("mod\t", "dep\t", "vcs.revision", "go1", "GOARCH", "GOOS", "CGO_ENABLED"))][:12]
    v["go_version"] = _run(["go", "version"])
    # glaze (the webview binding) is pulled in through a `replace`; record the real version.
    for cand in (os.path.join(repo_root, ".cache", f"webframe-src-{v['webframe_commit']}", "go.mod"), "/src/go.mod"):
        if os.path.exists(cand):
            for line in open(cand):
                if line.startswith("replace") and "glaze" in line:
                    v["glaze_version"] = line.strip().split("=>")[-1].strip()
            break
    v["node_version"] = _run([node_bin, "--version"])
    try:
        v["playwright_version"] = json.load(open(os.path.join(repo_root, "node_modules", "playwright", "package.json")))["version"]
    except (OSError, KeyError, ValueError):
        v["playwright_version"] = None
    browsers = {}
    if browsers_path and os.path.isdir(browsers_path):
        for d in sorted(os.listdir(browsers_path)):
            p = os.path.join(browsers_path, d)
            if os.path.isdir(p) and not d.startswith("."):
                browsers[d] = _du_bytes(p)
    v["playwright_browsers"] = browsers
    v["browsers_path"] = browsers_path
    if sys.platform.startswith("linux"):
        v["webkitgtk_pkg"] = _run(["bash", "-lc", "dpkg-query -W -f='${Package} ${Version}\\n' 'libwebkit2gtk-4.1-0' 'xvfb' 2>/dev/null"])
    return v


def static_footprint(webframe_bin: str, repo_root: str, browsers_path: str) -> dict:
    fp = {"webframe_binary_bytes": _du_bytes(webframe_bin)}
    nm = os.path.join(repo_root, "node_modules")
    fp["playwright_npm_bytes"] = (_du_bytes(os.path.join(nm, "playwright")) or 0) + (_du_bytes(os.path.join(nm, "playwright-core")) or 0)
    fp["node_modules_total_bytes"] = _du_bytes(nm)
    per_engine = {}
    if browsers_path and os.path.isdir(browsers_path):
        for d in os.listdir(browsers_path):
            p = os.path.join(browsers_path, d)
            if os.path.isdir(p) and not d.startswith("."):
                per_engine[d] = _du_bytes(p)
    fp["playwright_browser_bytes"] = per_engine
    fp["node_binary_bytes"] = _du_bytes(shutil.which("node") or "")
    if sys.platform.startswith("linux") and shutil.which("dpkg-query"):
        # Installed size of WebKitGTK + Xvfb and everything they pulled in, as
        # reported by dpkg (KiB). Computed by bench/footprint.sh with apt
        # accounting; here we only record the direct packages.
        out = _run(["bash", "-lc", "dpkg-query -W -f='${Package} ${Installed-Size}\\n' libwebkit2gtk-4.1-0 xvfb libjavascriptcoregtk-4.1-0 2>/dev/null"])
        fp["dpkg_direct_pkgs_kib"] = out
    return fp
