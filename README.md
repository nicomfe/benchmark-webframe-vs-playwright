# Webframe vs Playwright for agent verification loops

Does [webframe](https://github.com/tractordev/webframe) make an AI agent's
verification loop (edit code, load it in a browser, check console/exceptions/DOM,
feed the result back) faster than [Playwright](https://playwright.dev)? 

The findings are in
[`REPORT.md`](REPORT.md), which is generated from the raw data by script.

**Quick view of the results:** the *Scoreboard* at the top of `REPORT.md` lists the
winner of every category (cold start, warm per-check, 20-iteration loop, memory, CPU,
failure detection, raw JS speed, footprint, capabilities) with the margin, followed by
the verdict on hypotheses H1 to H4. The same scoreboard is printed in the terminal at
the end of `make bench`.

## Get the benchmark results

Prerequisites:

| Tool | Why | macOS | Linux |
| --- | --- | --- | --- |
| Python 3.10+ | runner, summary, charts (stdlib only) | preinstalled / `brew install python` | `apt install python3` |
| Node 18+ and npm | Playwright | `brew install node` | NodeSource / `apt install nodejs npm` |
| Go 1.27+ | build webframe from a pinned commit | `brew install go` | https://go.dev/dl |
| WebKitGTK 4.1 + Xvfb | webframe runtime on Linux only | - | `apt install libwebkit2gtk-4.1-0 xvfb` |
| Docker (optional) | reproducible Linux run + install footprint | Docker Desktop | docker.io |

Then:

```sh
git clone <this repo> && cd headless-benchmark
make bench            # setup + capability probes + benchmark + REPORT.md (about 20 minutes with defaults)
```

`make bench` does, in order:

1. `make setup`: `npm install` (Playwright), `npx playwright install chromium webkit`
   into `.cache/ms-playwright`, and builds webframe from the pinned commit into
   `.cache/bin/webframe` (idempotent; re-run safely).
2. `make caps`: fills the capability matrix by actually running each probe
   (`results/raw/<env>/capabilities.json`).
3. `make run`: the timed benchmark. One process sampler, one local static server, all
   configurations interleaved in random order per round, correctness-gated.
   Raw per-run records go to `results/raw/<env>/runs.jsonl`, environment facts to
   `results/raw/<env>/env.json`.
4. `make summary`: aggregates everything into `results/summary.csv`, draws SVG charts
   into `results/charts/`, writes `REPORT.md` and prints the scoreboard.

Shorter variants:

```sh
make quick            # RUNS=5 WARMUP=1 LOOP_RUNS=1 LOOP_ITERS=5: sanity check in ~2 minutes
make check            # one check per configuration, no report: verifies the toolchain works
make summary          # regenerate CSV, charts and REPORT.md from existing raw data
```

### Providing your own values

Every knob is a Make variable (or an environment variable with the same name, or a
flag of `bench/run.py`; run `python3 bench/run.py --help`):

| Variable | Default | Meaning |
| --- | --- | --- |
| `RUNS` | `30` | measured runs per scenario and configuration (Spec: 30, 50 if time allows) |
| `WARMUP` | `3` | discarded warmup runs per scenario and configuration |
| `LOOP_RUNS` | `5` | how many times the S7 loop is repeated per configuration (+1 warmup loop) |
| `LOOP_ITERS` | `20` | edit+verify iterations per S7 loop |
| `CONFIGS` | all | comma list of `wf-cli`, `wf-cli-display` (Linux), `wf-repl`, `pw-chromium-cold`, `pw-chromium-warm`, `pw-webkit-cold`, `pw-webkit-warm` |
| `SCENARIOS` | all | comma list of `S1,S2,S3-throw,S3-rejection,S3-resource,S4,S5,S6,S7` |
| `TIMEOUT` | `20` | seconds before a check is killed and counted as a failure |
| `SEED` | `1234` | seed for the per-round shuffle of configurations |
| `SAMPLE_INTERVAL` | `0.1` | seconds between process-tree RSS/CPU samples |
| `ENV_LABEL` | `<os>-<arch>[-docker]` | results directory name, e.g. `macos-arm64`; use `macos-arm64-run2` for a repeat run (the report then computes run-to-run variance) |
| `WEBFRAME_REF` | `03155e2ba67c` | tractordev/webframe commit to build |
| `DOCKER_CPUS` / `DOCKER_MEMORY` | `2` / `3g` | limits for `make docker-bench` (recorded in the report) |
| `DOCKER_PLATFORM` | host | e.g. `linux/amd64` to run the amd64 image under emulation |

Examples:

```sh
make bench RUNS=50 WARMUP=5                                   # more samples
make bench CONFIGS=wf-cli,pw-chromium-warm SCENARIOS=S2,S4,S7 # a subset
make bench ENV_LABEL=macos-arm64-run2                         # second full run -> variance section in REPORT.md
make docker-bench DOCKER_CPUS=1 DOCKER_MEMORY=1g              # constrained agent-sandbox-like Linux run
PLAYWRIGHT_BROWSERS_PATH=~/.cache/ms-playwright make bench    # reuse browsers you already have
```

### Linux via Docker (the Spec's primary environment)

```sh
make docker-bench     # builds the image (Ubuntu 24.04, Node 22, WebKitGTK, Xvfb, Playwright browsers) and runs caps+bench inside it
make footprint        # image size per stack, dpkg/npm/browser sizes, install time from a clean base container
make summary          # merge all environments into REPORT.md
```

`docker-bench` mounts `./results` into the container, so the Linux raw data lands next
to the macOS data (`results/raw/linux-<arch>-docker/`) and `make summary` reports both.
On Linux the runner adds `wf-cli-display` (webframe under a pre-started shared
`Xvfb :99`) next to `wf-cli` (webframe starting its own private Xvfb per process).

### Reading the output

- `REPORT.md`: summary, scoreboard, hypotheses, setup, per-scenario tables with
  median/p95/min/max, time breakdowns, memory, S3 detection quality, S7 loop, capability
  matrix, footprint, limitations, recommendation.
- `results/summary.csv`: one row per environment x scenario x configuration.
- `results/raw/<env>/runs.jsonl`: every check (warmups and failures included) with
  stdout, errors, eval result, exit code, wall time, breakdown, peak RSS, CPU.
- `results/charts/*.svg`: cold start, S7 loop, peak memory, footprint.

## Layout

```
REPORT.md                generated findings
Makefile                 entry points and knobs
Dockerfile               Ubuntu 24.04 stages: base, webframe-stack, playwright-stack, bench
pages/                   test pages: hello, ok, broken-throw, broken-rejection, broken-resource, heavy, spa, caps, api/items.json
bench/run.py             runner (orders, times, gates correctness, writes raw data)
bench/scenarios.py       scenario definitions and pass conditions
bench/harness/wf_cli.py  webframe, new process per check (`webframe -e -c <url>`)
bench/harness/wf_repl.py webframe, one long-lived `-c` process, navigate per check
bench/harness/pw_cold.py + pw_cold.js   Playwright, fresh node process + browser per check
bench/harness/pw_warm.py + pw_warm.js   Playwright, long-lived node process, new page per check
bench/harness/pw_check.js               shared Playwright check logic (what signals are captured)
bench/procmon.py         process-tree RSS/CPU sampler (incl. macOS WebKit helper attribution)
bench/server.py          local static server (Cache-Control: no-store)
bench/capabilities.py + harness/pw_caps.js   capability matrix, filled by running probes
bench/summarize.py       raw -> summary.csv, charts, REPORT.md
bench/footprint.sh       Linux install footprint via Docker
bench/setup.sh           idempotent toolchain setup
results/                 raw data, summary.csv, charts
```

## Methodology in one paragraph

Each scenario is a verification check an agent might run. A check passes only if the
tool reaches the verified outcome (expected line, READY with no errors, failure detected,
eval result equal to the expected value); failing runs are reported, never dropped from
the record. Cold configurations spawn a fresh process per check and are timed from spawn
to exit; warm configurations keep the browser running and are timed from sending the
request to reading the result. Configurations are interleaved in shuffled order every
round, one at a time. Peak RSS is summed over the tool's process tree (including webview
and browser helper processes) and CPU time comes from `wait4` rusage for cold runs. All
pages are served from a local server with caching disabled. See `REPORT.md` section 6
for limitations.
