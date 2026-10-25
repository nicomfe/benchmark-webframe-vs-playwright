# Reproducible Linux environment for the benchmark (Spec section 4.1).
#
# Stages, all on the same ubuntu:24.04 base so image sizes are comparable:
#   base              Ubuntu 24.04 + Node 22 + Python 3 + procps (shared by both stacks)
#   webframe-stack    base + libwebkit2gtk-4.1-0 + xvfb + webframe binary
#   playwright-stack  base + playwright npm + chromium/webkit browsers (--with-deps)
#   bench             both stacks + this repository (default target)
#
#   make docker-build            # builds `bench`
#   make docker-bench            # runs the benchmark with --cpus/--memory limits
#   make footprint               # measures image sizes and install times per stack

ARG WEBFRAME_REF=03155e2ba67c

# ---------------------------------------------------------------- webframe build
FROM golang:1.27-bookworm AS wfbuild
ARG WEBFRAME_REF
RUN mkdir -p /src && curl -sSL "https://github.com/tractordev/webframe/archive/${WEBFRAME_REF}.tar.gz" | tar xz -C /src --strip-components=1
WORKDIR /src
# glibc build: no cgo, glaze loads WebKitGTK at runtime via purego.
RUN CGO_ENABLED=0 GOFLAGS=-mod=mod go build -trimpath -ldflags="-s -w -X main.Version=${WEBFRAME_REF}" -o /webframe ./cmd/webframe

# ---------------------------------------------------------------- base
FROM ubuntu:24.04 AS base
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl gnupg python3 procps make \
 && mkdir -p /etc/apt/keyrings \
 && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
 && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" > /etc/apt/sources.list.d/nodesource.list \
 && apt-get update && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /bench

# ---------------------------------------------------------------- webframe stack
FROM base AS webframe-stack
RUN apt-get update \
 && apt-get install -y --no-install-recommends libwebkit2gtk-4.1-0 xvfb \
 && rm -rf /var/lib/apt/lists/*
COPY --from=wfbuild /webframe /usr/local/bin/webframe
ENV WEBFRAME_BIN=/usr/local/bin/webframe

# ---------------------------------------------------------------- playwright stack
FROM base AS playwright-stack
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
COPY package.json package-lock.json* ./
RUN npm ci --no-audit --no-fund \
 && npx playwright install --with-deps chromium webkit \
 && rm -rf /var/lib/apt/lists/* /root/.npm

# ---------------------------------------------------------------- both + repo
FROM playwright-stack AS bench
ARG WEBFRAME_REF
RUN apt-get update \
 && apt-get install -y --no-install-recommends libwebkit2gtk-4.1-0 xvfb \
 && rm -rf /var/lib/apt/lists/*
COPY --from=wfbuild /webframe /usr/local/bin/webframe
ENV WEBFRAME_BIN=/usr/local/bin/webframe WEBFRAME_REF=${WEBFRAME_REF}
COPY . .
RUN mkdir -p .cache && echo "${WEBFRAME_REF}" > .cache/webframe.ref \
 && mkdir -p "$PLAYWRIGHT_BROWSERS_PATH" && touch "$PLAYWRIGHT_BROWSERS_PATH/.installed-$(node -p "require('./node_modules/playwright/package.json').version")-chromium-webkit"
ENTRYPOINT ["make"]
CMD ["caps", "run"]
