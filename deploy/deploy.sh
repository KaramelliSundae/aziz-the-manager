#!/usr/bin/env bash
# Copies Aziz to the server and installs it: deploy/deploy.sh [user@server], or put user@server in deploy/.host.
# It restarts Aziz, so it waits for a moment when the image app has no work: FORCE=yes skips that.
set -euo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)
host=${1:-$(cat "$here/deploy/.host" 2>/dev/null || true)}
[[ -n $host ]] || { echo "usage: deploy/deploy.sh user@server" >&2; exit 1; }

idle=$(ssh "$host" "curl -s --max-time 3 http://127.0.0.1:8000/v1/health | python3 -c 'import json,sys; d=json.load(sys.stdin); q=d.get(\"queue\") or {}; print(0 if d.get(\"loading\") or q.get(\"running\") or q.get(\"waiting\") else 1)' 2>/dev/null || echo 1")
[[ $idle == 1 || ${FORCE:-} == yes ]] || { echo "the image app is working now, try again when it is done" >&2; exit 1; }

ssh "$host" sudo install -d -m 755 /opt/aziz
rsync -rlt --delete --rsync-path="sudo rsync" \
  --exclude .git --exclude .venv --exclude __pycache__ --exclude .pytest_cache --exclude tests --exclude .DS_Store --exclude .host \
  "$here/" "$host:/opt/aziz/"
ssh -t "$host" sudo bash /opt/aziz/deploy/install.sh
