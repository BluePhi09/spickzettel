#!/usr/bin/env bash
# Copy the shared modules into every plugin that needs them.
# Each plugin must be self-contained (plugins are installed into separate cache dirs).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cp "$ROOT/shared/memcore.py"   "$ROOT/plugins/spickzettel-files/scripts/memcore.py"
cp "$ROOT/shared/memcore.py"   "$ROOT/plugins/spickzettel-tool/server/memcore.py"
cp "$ROOT/shared/mcp_stdio.py" "$ROOT/plugins/spickzettel-tool/server/mcp_stdio.py"
cp "$ROOT/shared/mcp_stdio.py" "$ROOT/plugins/spickzettel-search/server/mcp_stdio.py"
echo "shared modules synced"
