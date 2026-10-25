# webframe vs Playwright benchmark.
#
#   make setup                  install Playwright + browsers, build webframe (idempotent)
#   make bench                  capability probes + full benchmark + summary/report
#   make quick                  same with tiny counts, to check everything works (~2 min)
#   make summary                regenerate results/summary.csv, charts and REPORT.md from raw data
#   make docker-bench           build the Linux image and run the benchmark inside it
#   make footprint              Linux install footprint + install time via Docker
#
# Knobs (override on the command line, e.g. `make bench RUNS=50 CONFIGS=wf-cli,pw-chromium-warm`):
RUNS        ?= 30       # measured runs per scenario and configuration
WARMUP      ?= 3        # discarded warmup runs
LOOP_RUNS   ?= 5        # repetitions of the S7 loop per configuration (+1 warmup loop)
LOOP_ITERS  ?= 20       # iterations per S7 loop
CONFIGS     ?=          # comma list; default all: wf-cli,wf-cli-display(linux),wf-repl,pw-chromium-cold,pw-chromium-warm,pw-webkit-cold,pw-webkit-warm
SCENARIOS   ?=          # comma list; default all: S1,S2,S3-throw,S3-rejection,S3-resource,S4,S5,S6,S7
TIMEOUT     ?= 20       # seconds per check
SEED        ?= 1234     # shuffle seed for config order
SAMPLE_INTERVAL ?= 0.1  # process sampler interval (s)
ENV_LABEL   ?=          # results/raw/<label>; default <os>-<arch>[-docker]
WEBFRAME_REF ?= 03155e2ba67c
DOCKER_IMAGE ?= headless-benchmark
DOCKER_CPUS  ?= 2
DOCKER_MEMORY ?= 3g
DOCKER_PLATFORM ?=      # e.g. linux/amd64 to force emulation on Apple Silicon

ROOT := $(dir $(abspath $(lastword $(MAKEFILE_LIST))))
export PLAYWRIGHT_BROWSERS_PATH ?= $(ROOT).cache/ms-playwright
export WEBFRAME_BIN ?= $(ROOT).cache/bin/webframe
export WEBFRAME_REF

# strip trailing comments/spaces from knob values
strip = $(strip $(firstword $(subst \#, ,$(1))))
RUN_ARGS = --runs $(call strip,$(RUNS)) --warmup $(call strip,$(WARMUP)) --loop-runs $(call strip,$(LOOP_RUNS)) --loop-iters $(call strip,$(LOOP_ITERS)) \
           --timeout $(call strip,$(TIMEOUT)) --seed $(call strip,$(SEED)) --sample-interval $(call strip,$(SAMPLE_INTERVAL)) \
           $(if $(call strip,$(CONFIGS)),--configs $(call strip,$(CONFIGS))) $(if $(call strip,$(SCENARIOS)),--scenarios $(call strip,$(SCENARIOS))) \
           $(if $(call strip,$(ENV_LABEL)),--label $(call strip,$(ENV_LABEL)))
LABEL_ARG = $(if $(call strip,$(ENV_LABEL)),--label $(call strip,$(ENV_LABEL)))

.PHONY: setup bench quick run caps summary clean docker-build docker-bench footprint check

setup:
	bash bench/setup.sh

check: setup
	python3 bench/run.py --runs 1 --warmup 0 --loop-runs 1 --loop-iters 2 --label _check --no-summary && rm -rf results/raw/_check && echo "OK: every configuration ran one check"

caps: setup
	python3 bench/capabilities.py $(LABEL_ARG)

run: setup
	python3 bench/run.py $(RUN_ARGS) --no-summary

bench: setup caps run summary

quick:
	$(MAKE) bench RUNS=5 WARMUP=1 LOOP_RUNS=1 LOOP_ITERS=5

summary:
	python3 bench/summarize.py

docker-build:
	DOCKER_BUILDKIT=1 docker build $(if $(call strip,$(DOCKER_PLATFORM)),--platform $(call strip,$(DOCKER_PLATFORM))) --build-arg WEBFRAME_REF=$(WEBFRAME_REF) -t $(DOCKER_IMAGE):bench --target bench .

docker-bench: docker-build
	mkdir -p results/raw
	docker run --rm --init $(if $(call strip,$(DOCKER_PLATFORM)),--platform $(call strip,$(DOCKER_PLATFORM))) \
	  --cpus=$(call strip,$(DOCKER_CPUS)) --memory=$(call strip,$(DOCKER_MEMORY)) --shm-size=1g \
	  -e BENCH_CONTAINER_CPUS=$(call strip,$(DOCKER_CPUS)) -e BENCH_CONTAINER_MEMORY=$(call strip,$(DOCKER_MEMORY)) \
	  -e RUNS=$(call strip,$(RUNS)) -e WARMUP=$(call strip,$(WARMUP)) -e LOOP_RUNS=$(call strip,$(LOOP_RUNS)) -e LOOP_ITERS=$(call strip,$(LOOP_ITERS)) \
	  -e CONFIGS=$(call strip,$(CONFIGS)) -e SCENARIOS=$(call strip,$(SCENARIOS)) -e TIMEOUT=$(call strip,$(TIMEOUT)) -e ENV_LABEL=$(call strip,$(ENV_LABEL)) \
	  -v $(ROOT)results:/bench/results $(DOCKER_IMAGE):bench caps run
	$(MAKE) summary

footprint:
	DOCKER_IMAGE=$(DOCKER_IMAGE) WEBFRAME_REF=$(WEBFRAME_REF) DOCKER_PLATFORM=$(call strip,$(DOCKER_PLATFORM)) bash bench/footprint.sh
	$(MAKE) summary

clean:
	rm -rf results/raw/_check pages/_scratch
