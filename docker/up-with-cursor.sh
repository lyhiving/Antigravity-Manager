#!/usr/bin/env bash
# 同栈启动 Manager(8045) + cursor-relay(8317)
# 用法（项目根）: ./docker/up-with-cursor.sh [--build]
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
ARGS=(compose -f docker/docker-compose.yml -f docker/docker-compose.cursor-relay.yml)
if [[ "${1:-}" == "--build" ]]; then
  docker "${ARGS[@]}" up -d --build
else
  docker "${ARGS[@]}" up -d
fi
docker "${ARGS[@]}" ps
echo
echo "Gateway:  http://127.0.0.1:8045   (clients / cursor-auto)"
echo "Sidecar:  http://127.0.0.1:8317/health  (local only)"
