#!/usr/bin/env bash
# Static install footprint on Linux, measured with Docker (Spec section 7):
#   - image size per stack, all built from the same ubuntu:24.04 base
#   - dpkg installed-size delta vs base (WebKitGTK+Xvfb; Playwright --with-deps)
#   - webframe binary, Playwright npm packages, browser download per engine
#   - install time from a clean base container (network dependent)
# Writes results/raw/linux-<arch>-docker/footprint.json
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
IMAGE="${DOCKER_IMAGE:-headless-benchmark}"
WEBFRAME_REF="${WEBFRAME_REF:-03155e2ba67c}"
PLAT=()
[ -n "${DOCKER_PLATFORM:-}" ] && PLAT=(--platform "$DOCKER_PLATFORM")
docker_run() {
  if ((${#PLAT[@]})); then
    docker run --rm "${PLAT[@]}" "$@"
  else
    docker run --rm "$@"
  fi
}

now() { python3 -c 'import time; print(time.time())'; }
say() { printf '\033[1;34m[footprint]\033[0m %s\n' "$*" >&2; }

for t in base webframe-stack playwright-stack bench; do
  say "building stage $t"
  if ((${#PLAT[@]})); then
    DOCKER_BUILDKIT=1 docker build "${PLAT[@]}" --build-arg WEBFRAME_REF="$WEBFRAME_REF" --target "$t" -t "$IMAGE:$t" . >/dev/null
  else
    DOCKER_BUILDKIT=1 docker build --build-arg WEBFRAME_REF="$WEBFRAME_REF" --target "$t" -t "$IMAGE:$t" . >/dev/null
  fi
done

size() { docker image inspect -f '{{.Size}}' "$IMAGE:$1"; }
IMG_BASE=$(size base); IMG_WF=$(size webframe-stack); IMG_PW=$(size playwright-stack); IMG_BENCH=$(size bench)
ARCH=$(docker_run "$IMAGE:base" uname -m)
case "$ARCH" in x86_64) ARCH=amd64;; aarch64) ARCH=arm64;; esac
LABEL="linux-$ARCH-docker"
say "arch $ARCH; image sizes base=$IMG_BASE webframe=$IMG_WF playwright=$IMG_PW bench=$IMG_BENCH"

dpkg_total_kib() { docker_run "$IMAGE:$1" bash -c "dpkg-query -W -f='\${Installed-Size}\n' | awk '{s+=\$1} END {print s}'"; }
DPKG_BASE=$(dpkg_total_kib base); DPKG_WF=$(dpkg_total_kib webframe-stack); DPKG_PW=$(dpkg_total_kib playwright-stack)
WF_BIN=$(docker_run "$IMAGE:webframe-stack" stat -c %s /usr/local/bin/webframe)
WF_PKGS=$(docker_run "$IMAGE:webframe-stack" bash -c "dpkg-query -W -f='\${Package} \${Version} \${Installed-Size}\n' libwebkit2gtk-4.1-0 libjavascriptcoregtk-4.1-0 xvfb libgtk-3-0t64 2>/dev/null | tr '\n' ';'")
NPM_BYTES=$(docker_run "$IMAGE:playwright-stack" bash -c "du -sb node_modules/playwright node_modules/playwright-core | awk '{s+=\$1} END {print s}'")
NM_TOTAL=$(docker_run "$IMAGE:playwright-stack" bash -c "du -sb node_modules | cut -f1")
BROWSERS_JSON=$(docker_run "$IMAGE:playwright-stack" bash -c "cd /ms-playwright && for d in */; do printf '\"%s\": %s,' \"\${d%/}\" \"\$(du -sb \"\$d\" | cut -f1)\"; done | sed 's/,\$//'")
PW_VERSION=$(docker_run "$IMAGE:playwright-stack" node -p "require('/bench/node_modules/playwright/package.json').version")
NODE_VERSION=$(docker_run "$IMAGE:base" node --version)

say "timing install from clean base: webframe stack (apt)"
T0=$(now)
docker_run "$IMAGE:base" bash -c "apt-get update -qq >/dev/null && apt-get install -y -qq --no-install-recommends libwebkit2gtk-4.1-0 xvfb >/dev/null 2>&1 && curl -sSL https://github.com/tractordev/webframe/archive/${WEBFRAME_REF}.tar.gz -o /dev/null" >/dev/null
T_WF=$(python3 -c "import time; print(round(time.time() - $T0, 1))")
say "  ${T_WF}s (apt-get update + install libwebkit2gtk-4.1-0 xvfb + download source tarball; building the binary is excluded since release zips exist)"

say "timing install from clean base: Playwright stack (npm + browsers --with-deps)"
T0=$(now)
docker_run -e PLAYWRIGHT_BROWSERS_PATH=/ms-playwright "$IMAGE:base" bash -c "mkdir -p /tmp/p && cd /tmp/p && npm init -y >/dev/null && npm i --no-audit --no-fund playwright@${PW_VERSION} >/dev/null 2>&1 && npx playwright install --with-deps chromium webkit >/dev/null 2>&1" >/dev/null
T_PW=$(python3 -c "import time; print(round(time.time() - $T0, 1))")
say "  ${T_PW}s"

WF_DEPS=$(( (DPKG_WF - DPKG_BASE) * 1024 ))
PW_DEPS=$(( (DPKG_PW - DPKG_BASE) * 1024 ))
BROWSERS_SUM=$(echo "{$BROWSERS_JSON}" | python3 -c 'import json,sys; print(sum(json.load(sys.stdin).values()))')
WF_STACK=$(( WF_BIN + WF_DEPS ))
PW_STACK=$(( NPM_BYTES + BROWSERS_SUM + PW_DEPS ))

OUT="results/raw/$LABEL"
mkdir -p "$OUT"
cat > "$OUT/footprint.json" <<EOF
{
 "basis": "Linux ($ARCH) Docker, ubuntu:24.04 base with Node $NODE_VERSION; dpkg installed-size deltas vs base + binaries/downloads",
 "arch": "$ARCH",
 "image_base_bytes": $IMG_BASE,
 "image_webframe_bytes": $IMG_WF,
 "image_playwright_bytes": $IMG_PW,
 "image_bench_bytes": $IMG_BENCH,
 "webframe_binary_bytes": $WF_BIN,
 "webframe_deps_bytes": $WF_DEPS,
 "webframe_stack_bytes": $WF_STACK,
 "webframe_apt_packages": "$WF_PKGS",
 "playwright_version": "$PW_VERSION",
 "playwright_npm_bytes": $NPM_BYTES,
 "node_modules_total_bytes": $NM_TOTAL,
 "playwright_browsers_bytes": {$BROWSERS_JSON},
 "playwright_deps_bytes": $PW_DEPS,
 "playwright_stack_bytes": $PW_STACK,
 "install_time_webframe_s": $T_WF,
 "install_time_playwright_s": $T_PW,
 "notes": [
  "Image deltas vs base: webframe-stack +$(( (IMG_WF - IMG_BASE) / 1048576 )) MB, playwright-stack +$(( (IMG_PW - IMG_BASE) / 1048576 )) MB, both (bench) +$(( (IMG_BENCH - IMG_BASE) / 1048576 )) MB.",
  "Install times are wall-clock from a fresh base container including network downloads (apt mirrors / npm / Playwright CDN); they vary with bandwidth.",
  "The webframe install time covers apt + fetching the source tarball; compiling with Go is excluded because upstream ships release zips, but the Go toolchain (~250 MB) would be needed to build from source.",
  "Playwright stack counts chromium + chromium headless shell + webkit + ffmpeg downloads plus the apt packages pulled by --with-deps for those engines."
 ]
}
EOF
say "wrote $OUT/footprint.json"
python3 -m json.tool "$OUT/footprint.json" >/dev/null && cat "$OUT/footprint.json"
