#!/usr/bin/env bash
# claude-hue-notify setup — discover bridge, pair, pick a light, write config.
# Usage: ./setup.sh
set -euo pipefail

CONFIG_DIR="${HUE_NOTIFY_DIR:-$HOME/.config/hue-notify}"
CONFIG_PATH="$CONFIG_DIR/config.json"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "== claude-hue-notify setup =="

# 1) Find the bridge IP -----------------------------------------------------
BRIDGE_IP="${1:-}"
if [ -z "$BRIDGE_IP" ]; then
  echo "-> Discovering bridge via Philips cloud..."
  BRIDGE_IP="$(curl -s --max-time 5 https://discovery.meethue.com \
    | sed -n 's/.*"internalipaddress":"\([^"]*\)".*/\1/p' | head -1 || true)"
fi
if [ -z "$BRIDGE_IP" ]; then
  read -r -p "Could not auto-discover. Enter bridge IP manually: " BRIDGE_IP
fi
echo "   Bridge IP: $BRIDGE_IP"

# 2) Pair (create API token) ------------------------------------------------
echo
echo ">> PRESS the round link button on top of your Hue Bridge now, then press Enter."
read -r _
echo "-> Requesting API token..."
RESP="$(curl -s --max-time 8 -X POST "http://$BRIDGE_IP/api/" \
  -H 'Content-Type: application/json' \
  -d '{"devicetype":"claude_hue_notify#setup"}')"
TOKEN="$(printf '%s' "$RESP" | sed -n 's/.*"username":"\([^"]*\)".*/\1/p')"
if [ -z "$TOKEN" ]; then
  echo "!! Pairing failed. Response: $RESP"
  echo "   Make sure you pressed the button and retry within 30s."
  exit 1
fi
echo "   Token acquired."

# 3) List lights & pick one -------------------------------------------------
echo
echo "-> Lights on this bridge:"
curl -s --max-time 8 "http://$BRIDGE_IP/api/$TOKEN/lights" | python3 -c '
import sys, json
d = json.load(sys.stdin)
for k, v in d.items():
    st = v.get("state", {})
    color = "color" if ("xy" in st or "hue" in st) else "white"
    print(f"   [{k}] {v.get(\"name\")!r}  ({color})")
'
echo
read -r -p "Which light id should be the notification light? " LIGHT_ID

# 4) Write config -----------------------------------------------------------
mkdir -p "$CONFIG_DIR"
cat > "$CONFIG_PATH" <<EOF
{
  "bridge_ip": "$BRIDGE_IP",
  "token": "$TOKEN",
  "light_id": "$LIGHT_ID"
}
EOF
echo "-> Wrote $CONFIG_PATH"

# 5) Quick test -------------------------------------------------------------
echo "-> Testing (light should turn green briefly)..."
python3 "$SCRIPT_DIR/hue-notify.py" done || true

# 6) Install hooks ----------------------------------------------------------
echo
read -r -p "Add the hooks to ~/.claude/settings.json automatically? [Y/n] " ANS
ANS="${ANS:-Y}"
if [[ "$ANS" =~ ^[Yy] ]]; then
  python3 "$SCRIPT_DIR/install-hooks.py" --script "$SCRIPT_DIR/hue-notify.py"
  echo
  echo "== All set! Restart Claude Code (open a new session) to activate. =="
else
  echo
  echo "Skipped. Add hooks yourself anytime with:"
  echo "  python3 $SCRIPT_DIR/install-hooks.py"
  echo "(or copy the block from README.md into ~/.claude/settings.json)"
fi
