#!/usr/bin/env bash
# 一键启动。Linux / macOS 通用。
set -euo pipefail

cd "$(dirname "$0")/.."

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

if [ -z "${YES_KEY:-}" ]; then
  echo "错误: 未设置 YES_KEY（YesCaptcha 客户端密钥）" >&2
  exit 1
fi

# 首次运行需安装 chromium（只需一次）
if [ "${INSTALL_BROWSER:-0}" = "1" ]; then
  python -m playwright install --with-deps chromium
fi

exec python -m uvicorn proxy_svc.app:app \
  --host "${HOST:-0.0.0.0}" \
  --port "${PORT:-8080}" \
  --workers 1 \
  --log-level "${LOG_LEVEL:-info}"
