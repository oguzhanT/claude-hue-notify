#!/usr/bin/env python3
"""Lampsignal — signal Claude Code session state with a Philips Hue light.

Usage:
    lampsignal.py waiting   # waiting for you        -> blue, blinks (~15s)
    lampsignal.py done      # task finished          -> green, turns off after 5s
    lampsignal.py error     # something went wrong   -> solid red
    lampsignal.py working   # working (optional)     -> solid blue
    lampsignal.py warning   # heads-up               -> solid amber
                            #   ("attention" and "success" work too: the PHP library's names)
    lampsignal.py off       # turn the light off
    lampsignal.py stop      # for the Stop hook: reads hook JSON on stdin and picks
                            #   "waiting" if my last message ends with "?", else "done"
    lampsignal.py list      # list bridge lights (for setup)
    lampsignal.py pair      # interactive: discover bridge, pair, pick a light, write config

Config: ~/.config/hue-notify/config.json  ({bridge_ip, token, light_id, light_ids?, pinned_public_key?})
        "light_ids" (optional list) drives several lights at once; falls back to "light_id".
        "project_lights" (optional {path: [ids]}) gives a project's sessions their own lamp(s).
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
import socket
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
# The shared contract: https://github.com/oguzhanT/lampsignal-php/blob/main/docs/signals.md
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
                f"If you replaced the bridge, run `lampsignal.py pair` again."
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
    """Saves the first pin, unless another process (a parallel hook, the Mac app)
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


def project_light_ids(cfg, cwd):
    """The lamps for a project: the longest "project_lights" path that contains
    cwd, else the notifier's lamps. Lets each project's sessions use their own lamp.

        "project_lights": {"~/code/shop": ["1"], "~/code/blog": ["7", "12"]}
    """
    mapping = cfg.get("project_lights") or {}
    if cwd and isinstance(mapping, dict):
        here = os.path.realpath(os.path.expanduser(cwd))
        best, best_len = None, -1
        for path, ids in mapping.items():
            root = os.path.realpath(os.path.expanduser(path))
            if (here == root or here.startswith(root.rstrip(os.sep) + os.sep)) and len(root) > best_len:
                best, best_len = ids, len(root)
        if best:
            return [str(i) for i in (best if isinstance(best, list) else [best])]
    return notifier_light_ids(cfg)


def set_light(cfg, body, light_id=None, lights=None):
    """Set one light, the given lights, or every active notifier light. Keeps
    going if one light fails."""
    errors = []
    targets = [light_id] if light_id else (lights or notifier_light_ids(cfg))
    for lid in targets:
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


# ── discovery and pairing ────────────────────────────────────────────────────

def _mdns_name(packet, offset, depth=0):
    """A (possibly compressed) DNS name at offset -> (name, offset after it)."""
    if depth > 10:
        raise ValueError("name pointer loop")
    labels = []
    while True:
        length = packet[offset]
        if length == 0:
            return ".".join(labels), offset + 1
        if length & 0xC0 == 0xC0:
            pointer = ((length & 0x3F) << 8) | packet[offset + 1]
            name, _ = _mdns_name(packet, pointer, depth + 1)
            labels.append(name)
            return ".".join(l for l in labels if l), offset + 2
        labels.append(packet[offset + 1:offset + 1 + length].decode("utf-8", "replace"))
        offset += 1 + length


def _mdns_records(packet):
    import struct
    qd, an, ns, ar = struct.unpack("!4H", packet[4:12])
    offset = 12
    for _ in range(qd):
        _, offset = _mdns_name(packet, offset)
        offset += 4
    records = []
    for _ in range(an + ns + ar):
        name, offset = _mdns_name(packet, offset)
        rtype, _, _, rdlength = struct.unpack("!HHIH", packet[offset:offset + 10])
        offset += 10
        start, offset = offset, offset + rdlength
        if offset > len(packet):
            raise ValueError("truncated record")
        if rtype == 1 and rdlength == 4:
            data = socket.inet_ntoa(packet[start:start + 4])
        elif rtype == 12:
            data = _mdns_name(packet, start)[0]
        elif rtype == 33:
            port = struct.unpack("!H", packet[start + 4:start + 6])[0]
            data = (port, _mdns_name(packet, start + 6)[0])
        elif rtype == 16:
            data, i = {}, start
            while i < offset:
                entry = packet[i + 1:i + 1 + packet[i]].decode("utf-8", "replace")
                i += 1 + packet[i]
                key, _, value = entry.partition("=")
                data[key.lower()] = value
        else:
            continue
        records.append((name.lower(), rtype, data))
    return records


def discover_bridges(timeout=2.0):
    """Bridges on this network: local mDNS (no internet needed), then Philips'
    discovery service. -> [{"ip": ..., "id": ...}]"""
    import struct
    found = {}
    query = struct.pack("!6H", 0, 0, 1, 0, 0, 0)
    for label in ("_hue", "_tcp", "local"):
        query += bytes([len(label)]) + label.encode()
    query += b"\x00" + struct.pack("!2H", 12, 0x8001)  # PTR, unicast response
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(0.3)
        sock.sendto(query, ("224.0.0.251", 5353))
        records, deadline = [], time.time() + timeout
        while time.time() < deadline:
            try:
                packet, _ = sock.recvfrom(9000)
                records += _mdns_records(packet)
            except socket.timeout:
                continue
            except (ValueError, IndexError, struct.error):
                continue
        sock.close()
        srv = {n: d for n, t, d in records if t == 33}
        txt = {n: d for n, t, d in records if t == 16}
        addr = {n: d for n, t, d in records if t == 1}
        for name, rtype, instance in records:
            if rtype == 12 and name == "_hue._tcp.local":
                instance = instance.lower()
                target = srv.get(instance, (None, None))[1]
                ip = addr.get((target or "").lower())
                if ip:
                    bridge_id = txt.get(instance, {}).get("bridgeid", instance.split(".")[0]).lower()
                    found[bridge_id] = {"ip": ip, "id": bridge_id}
    except OSError:
        pass
    if not found:
        try:
            with urllib.request.urlopen("https://discovery.meethue.com", timeout=5) as r:
                for b in json.loads(r.read()) or []:
                    found[b.get("id", "").lower()] = {"ip": b.get("internalipaddress"), "id": b.get("id", "").lower()}
        except Exception:
            pass
    return [b for b in found.values() if b.get("ip")]


class LinkButtonNotPressed(Exception):
    """The bridge wants its link button pressed before it issues a token."""


def pair_bridge(bridge_ip):
    """Non-interactive pairing: pins the bridge's key, asks for a token and
    saves both, keeping the lamp choice if it is the same bridge.
    Raises LinkButtonNotPressed until the button has been pressed.
    -> (config, bridge id from the certificate)"""
    probe = _PinnedConnection(bridge_ip, None, 8)
    try:
        probe.connect()
    finally:
        probe.close()
    names = probe.seen_common_names
    bridge_id = names[1] if len(names) > 1 else (names[0] if names else "")
    cfg = {"bridge_ip": bridge_ip, "token": "", "pinned_public_key": probe.seen_pin}
    if bridge_id:
        cfg["bridge_id"] = bridge_id
    resp = bridge_request(cfg, "POST", "", {"devicetype": "claude_hue_notify#pair"}, timeout=8,
                          authenticated=False)
    item = resp[0] if isinstance(resp, list) and resp else {}
    if item.get("error", {}).get("type") == 101:
        raise LinkButtonNotPressed("Press the round link button on the Hue bridge, then try again within 30 s.")
    token = item.get("success", {}).get("username")
    if not token:
        raise RuntimeError(f"Pairing failed: {resp}")
    try:
        previous = load_config()
    except (OSError, ValueError):
        previous = {}
    # Same bridge (by its id, else its key), maybe at a new address: keep the
    # lamp choices. A different bridge: its lamp ids mean nothing here.
    same = (previous.get("bridge_id") and previous.get("bridge_id") == bridge_id) or \
        (previous.get("pinned_public_key") == probe.seen_pin)
    if same:
        previous.update(cfg, token=token)
        cfg = previous
    else:
        cfg["token"] = token
    save_config(cfg)
    return cfg, bridge_id


def pair():
    """Interactive setup in a terminal: find the bridge, pair, pick a lamp."""
    print("-> Looking for bridges on this network...")
    bridges = discover_bridges()
    if bridges:
        bridge_ip = bridges[0]["ip"]
        print(f"   Found bridge {bridges[0]['id']} at {bridge_ip}")
    else:
        bridge_ip = input("No bridge found. Enter your Hue bridge IP: ").strip()

    print("\n>> PRESS the round link button on top of your Hue Bridge, then press Enter.")
    input()
    cfg = None
    for _ in range(3):
        try:
            cfg, bridge_id = pair_bridge(bridge_ip)
            print(f"   Paired with bridge {bridge_id or '?'}; its key is pinned. Pair on a network you trust.")
            break
        except LinkButtonNotPressed:
            print("   Link button not detected. Press it and Enter to retry...")
            input()
        except Exception as e:
            print(f"   Error: {e}. Retry? Press Enter.")
            input()
    if cfg is None:
        print("Pairing failed. Make sure you press the button, then re-run 'pair'.")
        sys.exit(1)

    print("\n-> Lights on this bridge:")
    lights = bridge_request(cfg, "GET", "lights")
    for lid, v in lights.items():
        st = v.get("state", {})
        color = "color" if ("xy" in st or "hue" in st) else "white"
        print(f"   [{lid}] {v.get('name')!r}  ({color})")
    choice = input("\nWhich light id(s) should be the notifier? (e.g. 7 or 1,7) ").strip()
    ids = [c.strip() for c in choice.split(",") if c.strip()]
    if ids:
        cfg["light_id"], cfg["light_ids"] = ids[0], ids
    save_config(cfg)
    print(f"\n-> Wrote {CONFIG_PATH}")

    print("-> Testing (light should turn green briefly)...")
    try:
        set_light(cfg, STATES["done"])
    except Exception as e:
        print(f"   (test failed: {e})")
    print("\nDone. If installed as a plugin, hooks are already active on next session.")
    print("Otherwise run: python3 install-hooks.py")


_HOOK_INPUT = None


def hook_input():
    """The JSON Claude Code passes a hook on stdin (session_id, cwd,
    transcript_path, ...); {} when run by hand from a terminal."""
    global _HOOK_INPUT
    if _HOOK_INPUT is None:
        _HOOK_INPUT = {}
        try:
            # Claude Code writes the JSON and closes stdin; a caller that leaves
            # an empty pipe open must not hang the hook, so wait briefly.
            import select
            if not sys.stdin.isatty() and select.select([sys.stdin], [], [], 0.2)[0]:
                data = json.loads(sys.stdin.read() or "{}")
                _HOOK_INPUT = data if isinstance(data, dict) else {}
        except Exception:
            pass
    return _HOOK_INPUT


def decide_stop_state():
    """For the Stop hook: return 'waiting' if my last assistant message ends
    with '?', else 'done'. Never raises."""
    try:
        payload = hook_input()
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

    # Paused (e.g. from the Lampsignal menu bar app): leave the light alone.
    if state != "_delayed_off" and is_paused():
        return

    # Stop hook: blue if my last message is a question, else green.
    if state == "stop":
        state = decide_stop_state()

    # Hidden command: delayed off (spawned detached after "done").
    if state == "_delayed_off":
        delay = int(sys.argv[2]) if len(sys.argv) > 2 else DONE_OFF_AFTER
        lights = sys.argv[3].split(",") if len(sys.argv) > 3 and sys.argv[3] else None
        time.sleep(delay)
        try:
            set_light(load_config(), STATES["off"], lights=lights)
        except Exception:
            pass
        return

    if state not in STATES:
        print(f"unknown state: {state}", file=sys.stderr)
        print(__doc__)
        sys.exit(1)

    try:
        cfg = load_config()
        # The session's project picks the lamp (project_lights), else the notifier's lamps.
        lights = project_light_ids(cfg, hook_input().get("cwd"))
        set_light(cfg, STATES[state], lights=lights)
    except Exception as e:
        # Never block / fail a hook.
        print(f"lampsignal warning: {e}", file=sys.stderr)
        sys.exit(0)

    # "done": spawn a detached process that turns the light off after N seconds.
    if state == "done":
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "_delayed_off", str(DONE_OFF_AFTER), ",".join(lights)],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


if __name__ == "__main__":
    main()
