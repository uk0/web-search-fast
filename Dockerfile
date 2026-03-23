# ---- Node.js build stage for admin UI ----
FROM node:20-alpine AS ui-builder
WORKDIR /ui
COPY admin-ui/package*.json ./
RUN npm ci 2>/dev/null || echo "No admin-ui package.json, skipping"
COPY admin-ui/ ./
RUN if [ -f package.json ]; then npx vite build --outDir dist; else mkdir -p dist; fi

# ---- Nuitka build stage ----
FROM python:3.11-slim AS builder

WORKDIR /app

# Build tools for Nuitka compilation
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ patchelf ccache \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY src/ ./src/

# Copy admin UI build output if available
COPY --from=ui-builder /ui/dist/ ./src/admin/static/

# Install deps + Nuitka, then compile
RUN pip install --no-cache-dir -e . nuitka ordered-set && \
    python -m nuitka \
        --standalone \
        --output-dir=/build \
        --output-filename=web-search-mcp \
        --follow-imports \
        --include-package=src \
        --include-package=mcp \
        --include-package=starlette \
        --include-package=uvicorn \
        --include-package=fastapi \
        --include-package=pydantic \
        --include-package=pydantic_core \
        --include-package=httpx \
        --include-package=httpcore \
        --include-package=anyio \
        --include-package=bs4 \
        --include-package=lxml \
        --include-package=markdownify \
        --include-package=camoufox \
        --include-package=playwright \
        --include-package=certifi \
        --include-package=h11 \
        --include-package=idna \
        --include-package=typing_extensions \
        --include-package=aiosqlite \
        --include-package=redis \
        --include-package=psutil \
        --include-package-data=src \
        --include-package-data=apify_fingerprint_datapoints \
        --include-package-data=browserforge \
        --include-package-data=camoufox \
        --include-package-data=certifi \
        --include-package-data=language_tags \
        --include-package-data=geoip2 \
        --include-package-data=maxminddb \
        --nofollow-import-to=pytest \
        --nofollow-import-to=setuptools \
        --nofollow-import-to=pip \
        --enable-plugin=anti-bloat \
        --prefer-source-code \
        --python-flag=-O \
        src/mcp_server.py

# ---- Runtime stage ----
FROM python:3.11-slim

WORKDIR /app

# Camoufox (Firefox-based) runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgtk-3-0 \
    libdbus-glib-1-2 \
    libxt6 \
    libx11-xcb1 \
    libasound2 \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    libpango-1.0-0 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libgbm1 \
    libnspr4 \
    libnss3 \
    libxshmfence1 \
    libxkbcommon0 \
    fonts-liberation \
    fonts-noto-cjk \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy Nuitka compiled binary
COPY --from=builder /build/mcp_server.dist/ /app/bin/

# Fetch Camoufox browser binary (needs pip camoufox for the fetch command)
RUN pip install --no-cache-dir "camoufox[geoip]>=0.4.11" && \
    python -m camoufox fetch

ENV MCP_HOST="0.0.0.0" \
    MCP_PORT="8897" \
    MCP_TRANSPORT="http" \
    BROWSER_POOL_SIZE="3" \
    BROWSER_PROXY="" \
    BROWSER_PROXY_LIST_FILE="" \
    PYTHONUNBUFFERED="1"

# SOCKS5 bridge uses local ports 19100+ for proxy auth relay
EXPOSE 8897

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -sf http://localhost:${MCP_PORT}/health -m 3 || exit 1

ENTRYPOINT ["/app/bin/web-search-mcp"]
CMD ["--transport", "http", "--host", "0.0.0.0", "--port", "8897"]
