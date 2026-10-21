#!/usr/bin/env python3
"""claude-hue-notify — signal Claude Code session state with a Philips Hue light.

Usage:
    hue-notify.py waiting   # waiting for you        -> blue, blinks (~15s)
    hue-notify.py done      # task finished          -> green, turns off after 5s
    hue-notify.py error     # something went wrong   -> solid red
    hue-notify.py working   # working (optional)     -> solid blue
    hue-notify.py warning   # heads-up               -> solid amber
                            #   ("attention" and "success" work too: the PHP library's names)
    hue-notify.py off       # turn the light off
    hue-notify.py stop      # for the Stop hook: reads hook JSON on stdin and picks
                            #   "waiting" if my last message ends with "?", else "done"
    hue-notify.py list      # list bridge lights (for setup)
    hue-notify.py pair      # interactive: discover bridge, pair, pick a light, write config

Config: ~/.config/hue-notify/config.json  ({bridge_ip, token, light_id, light_ids?, pinned_public_key?})
        "light_ids" (optional list) drives several lights at once; falls back to "light_id".
        "pinned_public_key" (sha256//<base64 SPKI>, as curl and the PHP library use) pins the
        bridge's key; HTTPS requests to any other refuse to send the token. Set on pairing, or
        on first use for older configs.
        Override the path with the HUE_NOTIFY_CONFIG env var.
Pause:  while a "paused" file exists next to the config, light changes are skipped.
        Empty file = paused indefinitely; a unix timestamp = paused until then.
No dependencies — Python stdlib only.
"""
import base64
import hashlib
import http.client
import json
import ssl
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
    "amber": {"hue": 8000,  "sat": 254},
}

# Names the PHP library (philips-hue-client) uses for the same states.
# The shared contract: https://github.com/oguzhanT/philips-hue-client/blob/main/docs/signals.md
ALIASES = {"attention": "waiting", "success": "done"}

# state -> Hue light state body
STATES = {
    "waiting": {"on": True, "bri": 200, **COLORS["blue"],  "alert": "lselect"},  # blue, blinks (~15s)
    "done":    {"on": True, "bri": 254, **COLORS["green"], "alert": "none"},      # solid green (auto-off later)
    "error":   {"on": True, "bri": 254, **COLORS["red"],   "alert": "none"},      # solid red
    "working": {"on": True, "bri": 150, **COLORS["blue"],  "alert": "none"},      # solid blue
    "warning": {"on": True, "bri": 254, **COLORS["amber"], "alert": "none"},      # solid amber
    "off":     {"on": False, "alert": "none"},                                    # off + cancel any running blink
}

DONE_OFF_AFTER = 5  # seconds: how long "done" stays on before auto-off

PAUSE_PATH = os.path.join(os.path.dirname(CONFIG_PATH), "paused")


def restore_path(light_id):
    """Where a sender that restores (the PHP library) saved a lamp's state."""
    safe = "".join(c for c in str(light_id) if c.isalnum() or c in "-_")
    return os.path.join(os.path.dirname(CONFIG_PATH), f"signal-restore-{safe}.json")


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def save_config(cfg):
    """Writes the config owner-only (it holds the bridge token), atomically."""
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    tmp = f"{CONFIG_PATH}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    os.replace(tmp, CONFIG_PATH)


class PinMismatch(Exception):
    """The bridge presented a different certificate than the pinned one."""


def _tlv(der, i):
    """One DER element at i: (tag, content start, end)."""
    tag = der[i]
    length = der[i + 1]
    i += 2
    if length & 0x80:
        n = length & 0x7F
        length = int.from_bytes(der[i:i + n], "big")
        i += n
    return tag, i, i + length


def public_key_pin(der):
    """sha256//<base64> of the certificate's SubjectPublicKeyInfo: the same pin
    the PHP library (pinned_public_key) and curl use. Unlike a hash of the whole
    certificate it survives the bridge renewing its certificate with the same key."""
    _, cert, _ = _tlv(der, 0)
    _, start, end = _tlv(der, cert)  # TBSCertificate
    fields = []
    while start < end:
        tag, _, nxt = _tlv(der, start)
        if tag != 0xA0:  # skip the optional [0] version
            fields.append((start, nxt))
        start = nxt
    # serial, signature algorithm, issuer, validity, subject, subjectPublicKeyInfo
    spki_start, spki_end = fields[5]
    digest = hashlib.sha256(der[spki_start:spki_end]).digest()
    return "sha256//" + base64.b64encode(digest).decode()


def certificate_common_names(der):
    """The CN values in a DER certificate, in order (issuer first, then subject)."""
    oid = b"\x06\x03\x55\x04\x03"
    names, i = [], 0
    while True:
        i = der.find(oid, i)
        if i < 0:
            return names
        j = i + len(oid)
        length = der[j + 1]
        names.append(der[j + 2:j + 2 + length].decode("utf-8", "replace"))
        i = j


class _PinnedConnection(http.client.HTTPSConnection):
    """HTTPS to the bridge, checked against a certificate fingerprint after
    the handshake and before the request (and its token) is sent: http.client
    connects lazily inside send(), so connect() runs before any byte goes out.

    The bridge's certificate is signed by Philips' own CA, which is not in the
    system store, so the chain is not verified; the pin is the check."""

    def __init__(self, host, expected_pin, timeout):
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        super().__init__(host, timeout=timeout, context=context)
        self.expected_pin = expected_pin
        self.seen_pin = None
        self.seen_common_names = []

    def connect(self):
        super().connect()
        der = self.sock.getpeercert(binary_form=True) or b""
        self.seen_pin = public_key_pin(der)
        self.seen_common_names = certificate_common_names(der)
        if self.expected_pin and self.seen_pin != self.expected_pin:
            self.close()
            raise PinMismatch(
                f"The bridge at {self.host} presented a different certificate than the pinned one. "
                f"If you replaced the bridge, run `hue-notify.py pair` again."
            )


def bridge_request(cfg, method, path, body=None, timeout=5, authenticated=True):
    """One request to the bridge's v1 API over pinned HTTPS. Returns the JSON.

    Without a pin yet (setups from before 1.3), the first connection's
    certificate is pinned and saved: trust on first use, like SSH.
    """
    if cfg.get("insecure_http"):
        conn = http.client.HTTPConnection(cfg["bridge_ip"], timeout=timeout)
    else:
        if not cfg.get("pinned_public_key") and cfg.get("token") and os.path.exists(CONFIG_PATH):
            # Another process may have pinned since this config was loaded.
            cfg["pinned_public_key"] = load_config().get("pinned_public_key")
        conn = _PinnedConnection(cfg["bridge_ip"], cfg.get("pinned_public_key"), timeout)
    url = f"/api/{cfg['token']}/{path}" if authenticated else f"/api/{path}"
    payload = json.dumps(body).encode() if body is not None else None
    try:
        conn.request(method, url, body=payload, headers={"Content-Type": "application/json"})
        data = conn.getresponse().read()
    finally:
        conn.close()
    if isinstance(conn, _PinnedConnection) and not cfg.get("pinned_public_key") and conn.seen_pin:
        _pin_on_first_use(cfg, conn.seen_pin)
    return json.loads(data or b"null")


def _pin_on_first_use(cfg, seen_pin):
    """Saves the first pin, unless another process (a parallel hook, HueBar)
    pinned meanwhile: then that pin must match, and nothing is rewritten."""
    cfg["pinned_public_key"] = seen_pin
    if not cfg.get("token") or not os.path.exists(CONFIG_PATH):
        return
    on_disk = load_config()
    pinned = on_disk.get("pinned_public_key")
    if pinned and pinned != seen_pin:
        raise PinMismatch(
            f"The bridge at {cfg['bridge_ip']} presented a different key than the one pinned in {CONFIG_PATH}."
        )
    if not pinned:
        on_disk["pinned_public_key"] = seen_pin
        save_config(on_disk)


def notifier_light_ids(cfg):
    """Active notifier lights: "light_ids" (list) if set, else the single "light_id"."""
    ids = cfg.get("light_ids")
    if isinstance(ids, list) and ids:
        return [str(i) for i in ids]
    return [str(cfg["light_id"])]


def set_light(cfg, body, light_id=None):
    """Set one light, or every active notifier light. Keeps going if one light fails."""
    errors = []
    for lid in ([light_id] if light_id else notifier_light_ids(cfg)):
        try:
            bridge_request(cfg, "PUT", f"lights/{lid}/state", body)
        except PinMismatch:
            raise  # never retry with the other lamps against an impostor
        except Exception as e:
            errors.append(e)
            continue
        # This change is newer than any state another sender is waiting to
        # restore, so that pending restore must not undo it.
        try:
            os.remove(restore_path(lid))
        except OSError:
            pass
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
    lights = bridge_request(cfg, "GET", "lights")
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
    # Pin the certificate first: the token we are about to receive must only
    # ever travel to this bridge.
    probe = _PinnedConnection(bridge_ip, None, 8)
    try:
        probe.connect()
    finally:
        probe.close()
    names = probe.seen_common_names
    bridge_id = names[1] if len(names) > 1 else (names[0] if names else "?")
    print(f"   Bridge certificate is for bridge id {bridge_id} (issued by {names[0] if names else '?'}).")
    print(f"   Pinning its public key {probe.seen_pin[:20]}... Pair on a network you trust.")
    cfg = {"bridge_ip": bridge_ip, "token": "", "pinned_public_key": probe.seen_pin}

    token = ""
    for _ in range(3):
        try:
            resp = bridge_request(cfg, "POST", "", {"devicetype": "claude_hue_notify#pair"}, timeout=8,
                                  authenticated=False)
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
    cfg["token"] = token
    print("\n-> Lights on this bridge:")
    lights = bridge_request(cfg, "GET", "lights")
    for lid, v in lights.items():
        st = v.get("state", {})
        color = "color" if ("xy" in st or "hue" in st) else "white"
        print(f"   [{lid}] {v.get('name')!r}  ({color})")
    light_id = input("\nWhich light id should be the notifier? ").strip()

    # 4) write config
    cfg["light_id"] = light_id
    save_config(cfg)
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

    state = ALIASES.get(sys.argv[1], sys.argv[1])

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
