#!/usr/bin/env bash
# Idempotent setup: Playwright npm package + browsers, and a webframe binary
# built from a pinned commit. Safe to re-run; each step is skipped when its
# output already exists.
#
# Env knobs (all optional):
#   WEBFRAME_REF              git ref/commit of tractordev/webframe to build (default pinned below)
#   WEBFRAME_BIN              where the binary should live (default .cache/bin/webframe)
#   PLAYWRIGHT_BROWSERS_PATH  browser download dir (default .cache/ms-playwright)
#   PW_ENGINES                "chromium webkit" (default)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

WEBFRAME_REF="${WEBFRAME_REF:-03155e2ba67c}"
WEBFRAME_BIN="${WEBFRAME_BIN:-$ROOT/.cache/bin/webframe}"
export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-$ROOT/.cache/ms-playwright}"
PW_ENGINES="${PW_ENGINES:-chromium webkit}"
mkdir -p .cache/bin

say() { printf '\033[1;34m[setup]\033[0m %s\n' "$*" >&2; }

# --- Node deps -------------------------------------------------------------
if [ ! -f node_modules/playwright/package.json ]; then
  say "npm install (playwright)"
  if [ -f package-lock.json ]; then npm ci --no-audit --no-fund; else npm install --no-audit --no-fund; fi
else
  say "playwright $(node -p "require('./node_modules/playwright/package.json').version") already installed"
fi

# --- Browsers ----------------------------------------------------------------
PW_VERSION="$(node -p "require('./node_modules/playwright/package.json').version")"
MARKER="$PLAYWRIGHT_BROWSERS_PATH/.installed-$PW_VERSION-$(echo "$PW_ENGINES" | tr ' ' '-')"
if [ ! -f "$MARKER" ]; then
  say "downloading Playwright browsers ($PW_ENGINES) into $PLAYWRIGHT_BROWSERS_PATH"
  if [ "$(uname -s)" = "Linux" ] && [ "$(id -u)" = "0" ]; then
    # shellcheck disable=SC2086
    npx playwright install --with-deps $PW_ENGINES
  else
    # shellcheck disable=SC2086
    npx playwright install $PW_ENGINES
  fi
  mkdir -p "$PLAYWRIGHT_BROWSERS_PATH" && touch "$MARKER"
else
  say "Playwright browsers already present in $PLAYWRIGHT_BROWSERS_PATH"
fi

# --- webframe ----------------------------------------------------------------
if [ -x "$WEBFRAME_BIN" ] && [ "$("$WEBFRAME_BIN" -v 2>/dev/null || true)" = "$WEBFRAME_REF" ]; then
  say "webframe $WEBFRAME_REF already built at $WEBFRAME_BIN"
else
  if ! command -v go >/dev/null; then
    echo "go not found; install Go 1.27+ (https://go.dev/dl/) or set WEBFRAME_BIN to an existing binary" >&2
    exit 1
  fi
  say "building webframe @ $WEBFRAME_REF with $(go version)"
  SRC=".cache/webframe-src-$WEBFRAME_REF"
  if [ ! -d "$SRC" ]; then
    mkdir -p "$SRC"
    # The module has a `replace` directive, so `go install ...@latest` is
    # refused by Go; build from a source snapshot instead.
    curl -sSL "https://github.com/tractordev/webframe/archive/${WEBFRAME_REF}.tar.gz" | tar xz -C "$SRC" --strip-components=1
  fi
  (
    cd "$SRC"
    export GOFLAGS=-mod=mod GOPATH="${GOPATH:-$ROOT/.cache/go}" CGO_ENABLED=0
    go build -trimpath -ldflags="-s -w -X main.Version=${WEBFRAME_REF}" -o "$WEBFRAME_BIN" ./cmd/webframe
  )
  echo "$WEBFRAME_REF" > .cache/webframe.ref
  say "built $WEBFRAME_BIN ($(du -h "$WEBFRAME_BIN" | cut -f1))"
fi
echo "$WEBFRAME_REF" > .cache/webframe.ref

if [ "$(uname -s)" = "Linux" ]; then
  if ! ldconfig -p 2>/dev/null | grep -q libwebkit2gtk-4.1; then
    say "WARNING: libwebkit2gtk-4.1 not found; webframe needs it: apt install libwebkit2gtk-4.1-0 xvfb"
  fi
  command -v Xvfb >/dev/null || say "WARNING: Xvfb not found; webframe needs a display on Linux (apt install xvfb)"
fi
say "ready"
