# claude-hue-notify

Turn a **Philips Hue** light into an ambient status indicator for **Claude Code**.

- 🔵 **Blue, blinking** — Claude needs you (asking a question, requesting permission, or idle waiting)
- 🟢 **Green** — Claude finished a turn; auto-off after 5s
- 🔴 **Solid red** — error (manual/optional)
- ⚫ **Off** — you replied (typed or answered a question dialog)

Works across **every** Claude Code session (any project, any terminal/IDE, multiple sessions at once) because it hooks into your **global** `~/.claude/settings.json`.

No dependencies — a single Python file using only the standard library, talking to the Hue Bridge's local REST API.

---

## How it works

Claude Code fires [hooks](https://docs.claude.com/en/docs/claude-code/hooks) on session events. This project maps those events to a small script that sets your Hue light's color/blink over the bridge's local API (`http://<bridge-ip>/api/<token>/lights/<id>/state`).

| Claude Code event | Meaning | Light |
|---|---|---|
| `Notification` | needs permission / idle waiting | 🔵 blue blink |
| `PreToolUse` (matcher `AskUserQuestion`) | about to ask you a question | 🔵 blue blink |
| `Stop` | finished a turn | 🔵 blue if my last message ends with `?`, else 🟢 green → off after 5s |
| `PostToolUse` (matcher `AskUserQuestion`) | you answered/cancelled the question | ⚫ off |
| `UserPromptSubmit` | you sent a message | ⚫ off |

The `Stop` handler is "smart": it reads the hook's transcript and, if Claude's final message is a question, keeps the light **blue** instead of falsely signalling success.

---

## Requirements

- A Philips Hue Bridge on the same LAN, with at least one **color** bulb
- Python 3 (macOS/Linux ship with it; `python3 --version`)
- Claude Code

---

## Quick setup (automated)

```bash
git clone https://github.com/<you>/claude-hue-notify.git
cd claude-hue-notify
./setup.sh
```

`setup.sh` will:
1. Discover your bridge IP (via Philips cloud discovery, or ask you).
2. Ask you to **press the bridge's round link button**, then create an API token.
3. List your lights and let you **pick which one** is the notifier.
4. Write `~/.config/hue-notify/config.json`.
5. Run a quick green-light test.
6. Print the exact `hooks` block to paste into `~/.claude/settings.json`.

Then add the printed hooks to `~/.claude/settings.json` (see below) and **restart Claude Code**.

---

## Manual setup

### 1. Find your bridge IP

```bash
curl -s https://discovery.meethue.com          # cloud discovery
# or via mDNS on macOS:
dns-sd -B _hue._tcp                             # find the bridge name
```

### 2. Create an API token (pair)

Press the round **link button** on top of the bridge, then within 30s:

```bash
curl -X POST http://<BRIDGE_IP>/api/ \
  -H 'Content-Type: application/json' \
  -d '{"devicetype":"claude_hue_notify#mac"}'
# -> [{"success":{"username":"<YOUR_TOKEN>"}}]
```

If you see `link button not pressed`, press it and retry.

### 3. Choose your light

```bash
cp config.example.json ~/.config/hue-notify/config.json
# edit bridge_ip + token first, then list lights:
python3 hue-notify.py list
#   [1] 'Hue Iris 1'      | type: Extended color light  | color: yes
#   [6] 'Bedroom'         | type: Extended color light  | color: yes
#   [7] 'Hue color lamp 1'| type: Extended color light  | color: yes
```

Put the id you want in `config.json` as `light_id`. Pick a **color** light so red/green/blue are distinguishable.

### 4. config.json

`~/.config/hue-notify/config.json` (this file is git-ignored — keep your token private):

```json
{
  "bridge_ip": "192.168.0.4",
  "token": "YOUR_HUE_API_TOKEN",
  "light_id": "7"
}
```

> You can keep the repo anywhere and point the script elsewhere with the
> `HUE_NOTIFY_CONFIG` env var if you don't want `~/.config/hue-notify/`.

### 5. Wire up the hooks

Add this to your global `~/.claude/settings.json` (merge into an existing `"hooks"` object if you already have one). Replace `/path/to` with the absolute path to your clone:

```json
"hooks": {
  "Notification":     [ { "hooks": [ { "type": "command", "command": "python3 /path/to/claude-hue-notify/hue-notify.py waiting" } ] } ],
  "PreToolUse":       [ { "matcher": "AskUserQuestion", "hooks": [ { "type": "command", "command": "python3 /path/to/claude-hue-notify/hue-notify.py waiting" } ] } ],
  "PostToolUse":      [ { "matcher": "AskUserQuestion", "hooks": [ { "type": "command", "command": "python3 /path/to/claude-hue-notify/hue-notify.py off" } ] } ],
  "UserPromptSubmit": [ { "hooks": [ { "type": "command", "command": "python3 /path/to/claude-hue-notify/hue-notify.py off" } ] } ],
  "Stop":             [ { "hooks": [ { "type": "command", "command": "python3 /path/to/claude-hue-notify/hue-notify.py stop" } ] } ]
}
```

**Restart Claude Code** (open a new session) for the hooks to load.

---

## CLI usage

```bash
python3 hue-notify.py waiting   # blue, blinks ~15s
python3 hue-notify.py done      # green, auto-off after 5s
python3 hue-notify.py error     # solid red
python3 hue-notify.py working   # solid blue
python3 hue-notify.py off       # off
python3 hue-notify.py list      # list bridge lights
python3 hue-notify.py stop      # (used by the Stop hook; reads hook JSON on stdin)
```

## Customizing

Open `hue-notify.py`:

- **Colors** — `COLORS` (Hue `hue` 0–65535, `sat` 0–254).
- **Per-state behavior** — `STATES` (`bri` brightness, `alert: "lselect"` = blink ~15s, `"none"` = steady).
- **Auto-off delay** — `DONE_OFF_AFTER` (seconds).

---

## Notes & limitations

- **Shared single light.** With multiple concurrent sessions, they all drive the same light; the last event wins. It signals "at least one session needs you / finished", not which one.
- **No native error event.** Claude Code has no clean "error" hook, so `error` (red) isn't auto-wired — call it yourself or add a custom hook.
- **Hooks run commands on your machine.** Review any hook before adding it. This script only talks to your local bridge and never blocks a turn (it exits 0 on any failure).

## Troubleshooting

| Problem | Fix |
|---|---|
| `link button not pressed` | Press the bridge button, retry within 30s. |
| Light never changes | `python3 hue-notify.py done` manually; check `bridge_ip`/`token`/`light_id`; ping the bridge. |
| Hooks don't fire | Restart Claude Code; validate JSON: `python3 -c "import json;json.load(open('$HOME/.claude/settings.json'))"`. |
| Blue doesn't blink | Some bulbs support `lselect` weakly; increase `bri` or use a color bulb. |

## License

MIT © 2026 Oğuzhan Togay
