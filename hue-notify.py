#!/usr/bin/env python3
"""claude-hue-notify — signal Claude Code session state with a Philips Hue light.

Usage:
    hue-notify.py waiting   # waiting for you        -> blue, blinks (~15s)
    hue-notify.py done      # task finished          -> green, turns off after 5s
    hue-notify.py error     # something went wrong   -> solid red
    hue-notify.py working   # working (optional)     -> solid blue
    hue-notify.py off       # turn the light off
    hue-notify.py stop      # for the Stop hook: reads hook JSON on stdin and picks
                            #   "waiting" if my last message ends with "?", else "done"
    hue-notify.py list      # list bridge lights (for setup)
    hue-notify.py pair      # interactive: discover bridge, pair, pick a light, write config

Config: ~/.config/hue-notify/config.json  ({bridge_ip, token, light_id, light_ids?})
        "light_ids" (optional list) drives several lights at once; falls back to "light_id".
        Override the path with the HUE_NOTIFY_CONFIG env var.
Pause:  while a "paused" file exists next to the config, light changes are skipped.
        Empty file = paused indefinitely; a unix timestamp = paused until then.
No dependencies — Python stdlib only.
"""
import json
import os
import sys
import subprocess
import time
import urllib.request

CONFIG_PATH = os.path.expanduser(
    os.environ.get("HUE_NOTIFY_CONFIG", "~/.config/hue-notify/config.json")
)

# Hue hue/sat color values (hue: 0-65535, sat: 0-254)
COLORS = {
    "red":   {"hue": 0,     "sat": 254},
    "green": {"hue": 25500, "sat": 254},
    "blue":  {"hue": 46920, "sat": 254},
}

# state -> Hue light state body
STATES = {
    "waiting": {"on": True, "bri": 200, **COLORS["blue"],  "alert": "lselect"},  # blue, blinks (~15s)
    "done":    {"on": True, "bri": 254, **COLORS["green"], "alert": "none"},      # solid green (auto-off later)
    "error":   {"on": True, "bri": 254, **COLORS["red"],   "alert": "none"},      # solid red
    "working": {"on": True, "bri": 150, **COLORS["blue"],  "alert": "none"},      # solid blue
    "off":     {"on": False, "alert": "none"},                                    # off + cancel any running blink
}

DONE_OFF_AFTER = 5  # seconds: how long "done" stays on before auto-off

PAUSE_PATH = os.path.join(os.path.dirname(CONFIG_PATH), "paused")


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def api_url(cfg, path):
    return f"http://{cfg['bridge_ip']}/api/{cfg['token']}/{path}"


def notifier_light_ids(cfg):
    """Active notifier lights: "light_ids" (list) if set, else the single "light_id"."""
    ids = cfg.get("light_ids")
    if isinstance(ids, list) and ids:
        return [str(i) for i in ids]
    return [str(cfg["light_id"])]


def set_light(cfg, body, light_id=None):
    """Set one light, or every active notifier light. Keeps going if one light fails."""
    data = json.dumps(body).encode()
    errors = []
    for lid in ([light_id] if light_id else notifier_light_ids(cfg)):
        req = urllib.request.Request(api_url(cfg, f"lights/{lid}/state"), data=data, method="PUT")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                resp.read()
        except Exception as e:
            errors.append(e)
    if errors:
        raise errors[0]


def is_paused():
    try:
        with open(PAUSE_PATH) as f:
            raw = f.read().strip()
    except OSError:
        return False
    if not raw:
        return True
    try:
        return time.time() < float(raw)
    except ValueError:
        return True


def list_lights(cfg):
    with urllib.request.urlopen(api_url(cfg, "lights"), timeout=5) as resp:
        lights = json.loads(resp.read())
    for lid, v in lights.items():
        st = v.get("state", {})
        color = "yes" if ("xy" in st or "hue" in st) else "no"
        print(f"  [{lid}] {v.get('name')!r}  | type: {v.get('type')}  | color: {color}")


def pair():
    """Interactive setup: discover bridge, create a token, pick a light, write config."""
    # 1) bridge IP
    bridge_ip = ""
    try:
        with urllib.request.urlopen("https://discovery.meethue.com", timeout=5) as r:
            found = json.loads(r.read())
        if found:
            bridge_ip = found[0].get("internalipaddress", "")
    except Exception:
        pass
    if bridge_ip:
        print(f"-> Discovered bridge at {bridge_ip}")
    else:
        bridge_ip = input("Enter your Hue bridge IP: ").strip()

    # 2) create token (wait for link button)
    print("\n>> PRESS the round link button on top of your Hue Bridge, then press Enter.")
    input()
    url = f"http://{bridge_ip}/api/"
    body = json.dumps({"devicetype": "claude_hue_notify#pair"}).encode()
    token = ""
    for _ in range(3):
        try:
            req = urllib.request.Request(url, data=body, method="POST")
            req.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(req, timeout=8) as r:
                resp = json.loads(r.read())
            if resp and "success" in resp[0]:
                token = resp[0]["success"]["username"]
                break
            print("   Link button not detected. Press it and Enter to retry...")
            input()
        except Exception as e:
            print(f"   Error: {e}. Retry? Press Enter.")
            input()
    if not token:
        print("Pairing failed. Make sure you press the button, then re-run 'pair'.")
        sys.exit(1)
    print("   Token acquired.")

    # 3) list lights & choose
    cfg = {"bridge_ip": bridge_ip, "token": token}
    print("\n-> Lights on this bridge:")
    with urllib.request.urlopen(api_url(cfg, "lights"), timeout=5) as r:
        lights = json.loads(r.read())
    for lid, v in lights.items():
        st = v.get("state", {})
        color = "color" if ("xy" in st or "hue" in st) else "white"
        print(f"   [{lid}] {v.get('name')!r}  ({color})")
    light_id = input("\nWhich light id should be the notifier? ").strip()

    # 4) write config
    cfg["light_id"] = light_id
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    print(f"\n-> Wrote {CONFIG_PATH}")

    # 5) test
    print("-> Testing (light should turn green briefly)...")
    try:
        set_light(cfg, STATES["done"])
    except Exception as e:
        print(f"   (test failed: {e})")
    print("\nDone. If installed as a plugin, hooks are already active on next session.")
    print("Otherwise run: python3 install-hooks.py")


def decide_stop_state():
    """For the Stop hook: parse hook JSON from stdin, return 'waiting' if my last
    assistant message ends with '?', else 'done'. Never raises."""
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        tpath = os.path.expanduser(payload.get("transcript_path", ""))
        if not tpath or not os.path.exists(tpath):
            return "done"
        last_text = ""
        with open(tpath) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except Exception:
                    continue
                msg = entry.get("message", {})
                if msg.get("role") != "assistant":
                    continue
                content = msg.get("content", "")
                if isinstance(content, list):
                    texts = [b.get("text", "") for b in content
                             if isinstance(b, dict) and b.get("type") == "text"]
                    content = "\n".join(t for t in texts if t)
                if isinstance(content, str) and content.strip():
                    last_text = content
        lines = [ln.rstrip() for ln in last_text.splitlines() if ln.strip()]
        if lines and lines[-1].endswith("?"):
            return "waiting"
    except Exception:
        pass
    return "done"


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    state = sys.argv[1]

    if state == "pair":
        pair()
        return

    if state == "list":
        list_lights(load_config())
        return

    # Paused (e.g. from the HueBar menu bar app): leave the light alone.
    if state != "_delayed_off" and is_paused():
        return

    # Stop hook: blue if my last message is a question, else green.
    if state == "stop":
        state = decide_stop_state()

    # Hidden command: delayed off (spawned detached after "done").
    if state == "_delayed_off":
        delay = int(sys.argv[2]) if len(sys.argv) > 2 else DONE_OFF_AFTER
        time.sleep(delay)
        try:
            set_light(load_config(), STATES["off"])
        except Exception:
            pass
        return

    if state not in STATES:
        print(f"unknown state: {state}", file=sys.stderr)
        print(__doc__)
        sys.exit(1)

    try:
        cfg = load_config()
        set_light(cfg, STATES[state])
    except Exception as e:
        # Never block / fail a hook.
        print(f"hue-notify warning: {e}", file=sys.stderr)
        sys.exit(0)

    # "done": spawn a detached process that turns the light off after N seconds.
    if state == "done":
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "_delayed_off", str(DONE_OFF_AFTER)],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


if __name__ == "__main__":
    main()
