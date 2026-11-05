#!/usr/bin/env bash
# Installs the skill and the MCP tools once, for every project: client/install.sh [user@server]. Safe to re-run.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
skill="$HOME/.claude/skills/offload"
mkdir -p "$skill/scripts" "$HOME/.claude/offload"
install -m 644 "$here/SKILL.md" "$skill/SKILL.md"
install -m 755 "$here/scripts/offload.py" "$here/scripts/offload_mcp.py" "$skill/scripts/"
host=${1:-offload@$(sed -E 's/.*@//' "$here/../deploy/.host" 2>/dev/null || true)}
python3 -c 'import json,sys; json.dump({"host": sys.argv[1], "key": "~/.ssh/offload_ed25519"}, open(sys.argv[2], "w"))' "$host" "$HOME/.claude/offload/config.json"
echo "==> skill installed in $skill"
