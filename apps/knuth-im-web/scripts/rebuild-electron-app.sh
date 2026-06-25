#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"

echo "==> 1/5 sidecar:build"
npm run sidecar:build

echo "==> 2/5 typecheck"
if ! npm run typecheck; then
  echo "typecheck 失败，清理过期 .next 后重试..."
  rm -rf .next
  npm run typecheck
fi

echo "==> 3/5 build"
npm run build

echo "==> 4/5 electron build"
ELECTRON_MIRROR="${ELECTRON_MIRROR:-https://npmmirror.com/mirrors/electron/}" \
  npx electron-builder --mac --dir --publish never

echo "==> 5/5 smoke:sidecar-binary"
npm run smoke:sidecar-binary

echo "完成：electron 应用已重新构建。"
