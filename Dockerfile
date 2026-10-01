# AgentOS 统一镜像：个人 Compose 与企业 Kubernetes 共用同一构建物（NFR-DEP）
FROM node:22-bookworm-slim AS assets
WORKDIR /build/frontend/workflow
COPY frontend/workflow/package.json frontend/workflow/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/workflow/ ./
RUN npm run typecheck && npm run build
# The upstream CLI owns inference and compaction; no custom execution engine.
ARG NPM_REGISTRY=https://registry.npmjs.org
RUN npm install --prefix /opt/codex --omit=dev --no-audit --no-fund \
    --registry="$NPM_REGISTRY" --fetch-retries=1 --fetch-timeout=180000 @openai/codex@0.159.2

FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS app

# 构建元信息（NFR-DEP）：镜像内自证来源，Compose/k3s 两端比对即证明同构建物。
# docker save→ctr import 会重新打包改变 manifest digest，故不能只靠 imageID。
ARG GIT_COMMIT=unknown
ARG BUILD_TIME=unknown
LABEL org.opencontainers.image.revision="$GIT_COMMIT"
LABEL org.opencontainers.image.created="$BUILD_TIME"

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# 先锁依赖再拷贝源码，最大化层缓存
COPY pyproject.toml uv.lock .python-version README.md LICENSE NOTICE ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY frontend ./frontend
COPY --from=assets /build/frontend/workflow-dist ./frontend/workflow-dist
COPY --from=assets /opt/codex /opt/codex
COPY --from=assets /usr/local/bin/node /usr/local/bin/node
RUN uv sync --frozen --no-dev

# Node's official binary requires the matching Debian C++ runtime.
RUN apt-get update && apt-get install -y --no-install-recommends libstdc++6 \
    && rm -rf /var/lib/apt/lists/*

RUN printf 'git_commit=%s\nbuild_time=%s\n' "$GIT_COMMIT" "$BUILD_TIME" > /app/BUILD_INFO

ENV PATH="/app/.venv/bin:/opt/codex/node_modules/.bin:$PATH" AGENTOS_DATA_DIR=/data
RUN node --version && codex --version
EXPOSE 8000
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz')" || exit 1

CMD ["uvicorn", "agentos.apps.gateway:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
