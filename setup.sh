#!/usr/bin/env bash
# Lampsignal setup for the standalone script (not needed for the plugin).
# Finds the bridge, pairs over pinned HTTPS, picks a lamp, offers to install the hooks.
# Usage: ./setup.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "== Lampsignal setup =="
python3 "$SCRIPT_DIR/lampsignal.py" pair

echo
read -r -p "Add the hooks to ~/.claude/settings.json automatically? [Y/n] " ANS
ANS="${ANS:-Y}"
if [[ "$ANS" =~ ^[Yy] ]]; then
  python3 "$SCRIPT_DIR/install-hooks.py" --script "$SCRIPT_DIR/lampsignal.py"
  echo
  echo "== All set! Restart Claude Code (open a new session) to activate. =="
else
  echo
  echo "Skipped. Add hooks yourself anytime with:"
  echo "  python3 $SCRIPT_DIR/install-hooks.py"
fi
