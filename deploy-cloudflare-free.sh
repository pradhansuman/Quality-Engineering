#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE="$ROOT/.qa-deploy"
mkdir -p "$STATE"

for command in python3 npm npx cloudflared curl openssl; do
  command -v "$command" >/dev/null 2>&1 || { echo "Missing required command: $command" >&2; exit 1; }
done

if [[ ! -x "$ROOT/.venv/bin/python" ]]; then python3 -m venv "$ROOT/.venv"; fi
"$ROOT/.venv/bin/pip" install -q -r "$ROOT/requirements.txt"
"$ROOT/.venv/bin/python" -m playwright install chromium

API_TOKEN="$(openssl rand -hex 32)"
UI_ACCESS_KEY="$(openssl rand -hex 12)"
if [[ -f "$STATE/backend.pid" ]]; then kill "$(cat "$STATE/backend.pid")" 2>/dev/null || true; fi
if [[ -f "$STATE/tunnel.pid" ]]; then kill "$(cat "$STATE/tunnel.pid")" 2>/dev/null || true; fi

QA_API_TOKEN="$API_TOKEN" QA_REPORT_ROOT="$ROOT/reports" nohup \
  "$ROOT/.venv/bin/uvicorn" api.server:app --app-dir "$ROOT" --host 127.0.0.1 --port 8000 \
  >"$STATE/backend.log" 2>&1 &
echo $! >"$STATE/backend.pid"
for _ in {1..30}; do curl -fsS http://127.0.0.1:8000/api/health >/dev/null && break; sleep 1; done
curl -fsS http://127.0.0.1:8000/api/health >/dev/null || { cat "$STATE/backend.log"; exit 1; }

nohup cloudflared tunnel --no-autoupdate --url http://127.0.0.1:8000 >"$STATE/tunnel.log" 2>&1 &
echo $! >"$STATE/tunnel.pid"
TUNNEL_URL=""
for _ in {1..45}; do
  TUNNEL_URL="$(grep -Eo 'https://[-a-z0-9]+\.trycloudflare\.com' "$STATE/tunnel.log" | tail -1 || true)"
  [[ -n "$TUNNEL_URL" ]] && break
  sleep 1
done
[[ -n "$TUNNEL_URL" ]] || { cat "$STATE/tunnel.log"; exit 1; }

cd "$ROOT/web"
npm install
printf '%s' "$API_TOKEN" | npx wrangler secret put QA_API_TOKEN
printf '%s' "$UI_ACCESS_KEY" | npx wrangler secret put QA_UI_ACCESS_KEY
npx wrangler deploy --var "QA_API_BASE_URL:$TUNNEL_URL"

cat >"$STATE/deployment.txt" <<EOF
Backend PID: $(cat "$STATE/backend.pid")
Tunnel PID: $(cat "$STATE/tunnel.pid")
Tunnel URL: $TUNNEL_URL
Access key: $UI_ACCESS_KEY
EOF
echo
echo "Deployment complete."
echo "Access key: $UI_ACCESS_KEY"
echo "Keep this Mac powered on and connected to the internet."
echo "Deployment details: $STATE/deployment.txt"
