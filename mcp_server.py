#!/usr/bin/env python3
"""MCP server for claude-hue-notify: lets Claude see and control your Hue lights.

Speaks the Model Context Protocol over stdio (JSON-RPC 2.0, one message per
line). Uses the same config and pause file as hue-notify.py, so the bridge you
paired with `hue-notify.py pair` is the one it talks to.

Tools: hue_status, hue_list_lights, hue_set_light, hue_signal, hue_list_scenes,
hue_activate_scene, hue_pause, hue_resume.

No dependencies — Python stdlib only.
"""
import importlib.util
import json
import os
import sys
import time
import urllib.request

_spec = importlib.util.spec_from_file_location(
    "hue_notify", os.path.join(os.path.dirname(os.path.abspath(__file__)), "hue-notify.py")
)
hn = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hn)

SERVER_INFO = {"name": "hue", "version": "1.2.0"}
PROTOCOL_VERSIONS = ["2025-06-18", "2025-03-26", "2024-11-05"]

SIGNALS = ["attention", "success", "error", "warning", "working", "off"]
SIGNAL_STATE = {"attention": "waiting", "success": "done", "error": "error",
                "warning": "warning", "working": "working", "off": "off"}


class ToolError(Exception):
    """A problem to report to Claude as a failed tool call, not a protocol error."""


# ── bridge helpers ────────────────────────────────────────────────────────────

def bridge(method, path, body=None):
    cfg = hn.load_config()
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(hn.api_url(cfg, path), data=data, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read() or b"null")
    except OSError as e:
        raise ToolError(f"Cannot reach the bridge: {e}".replace(cfg.get("token", "\0"), "***"))
    errors = [item["error"]["description"] for item in (result if isinstance(result, list) else [])
              if isinstance(item, dict) and "error" in item]
    if errors:
        raise ToolError("Bridge refused: " + "; ".join(errors))
    return result


def all_lights():
    return bridge("GET", "lights")


def resolve_light(ref, lights=None):
    """A light id or a name (case-insensitive) -> id."""
    lights = lights if lights is not None else all_lights()
    ref = str(ref).strip()
    if ref in lights:
        return ref
    for lid, light in lights.items():
        if light.get("name", "").lower() == ref.lower():
            return lid
    names = ", ".join(f"{l.get('name')} ({i})" for i, l in lights.items())
    raise ToolError(f"No light '{ref}'. Lights: {names}")


def hex_to_xy(hex_color):
    """Philips' wide-gamut conversion, the same as the PHP library's Color::toXy()."""
    h = hex_color.strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6 or any(c not in "0123456789abcdefABCDEF" for c in h):
        raise ToolError(f"Not a hex color: {hex_color!r}")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    lin = [((c + 0.055) / 1.055) ** 2.4 if c > 0.04045 else c / 12.92 for c in (r, g, b)]
    x = lin[0] * 0.664511 + lin[1] * 0.154324 + lin[2] * 0.162028
    y = lin[0] * 0.283881 + lin[1] * 0.668433 + lin[2] * 0.047685
    z = lin[0] * 0.000088 + lin[1] * 0.072310 + lin[2] * 0.986039
    total = x + y + z
    if total == 0:
        return [0.3127, 0.3290]
    return [round(x / total, 4), round(y / total, 4)]


def pause_state():
    if not os.path.exists(hn.PAUSE_PATH):
        return {"paused": False}
    raw = open(hn.PAUSE_PATH).read().strip()
    if not raw:
        return {"paused": True, "until": None}
    try:
        until = float(raw)
    except ValueError:
        return {"paused": True, "until": None}
    if time.time() >= until:
        return {"paused": False}
    return {"paused": True, "until": time.strftime("%Y-%m-%d %H:%M", time.localtime(until))}


# ── tools ────────────────────────────────────────────────────────────────────

def tool_status(args):
    cfg = hn.load_config()
    return {"bridge_ip": cfg.get("bridge_ip"), "notifier_lights": hn.notifier_light_ids(cfg), **pause_state()}


def tool_list_lights(args):
    out = []
    for lid, light in all_lights().items():
        st = light.get("state", {})
        out.append({
            "id": lid,
            "name": light.get("name"),
            "on": st.get("on"),
            "reachable": st.get("reachable"),
            "brightness": round(st.get("bri", 0) * 100 / 254) if "bri" in st else None,
            "color": "xy" in st or "hue" in st,
            "color_temperature": "ct" in st,
        })
    return out


def tool_set_light(args):
    lights = all_lights()
    lid = resolve_light(args["light"], lights)
    state = {}
    if "on" in args:
        state["on"] = bool(args["on"])
    if "brightness" in args:
        pct = max(0, min(100, float(args["brightness"])))
        if pct == 0:
            state["on"] = False
        else:
            state.update({"on": True, "bri": max(1, round(pct * 254 / 100))})
    if "color" in args:
        state.update({"on": True, "xy": hex_to_xy(args["color"])})
    if "color_temperature" in args:
        kelvin = max(1, int(args["color_temperature"]))
        state.update({"on": True, "ct": max(153, min(500, round(1_000_000 / kelvin)))})
    if "transition_ms" in args:
        state["transitiontime"] = max(0, round(int(args["transition_ms"]) / 100))
    if not state:
        raise ToolError("Nothing to change: give on, brightness, color or color_temperature.")
    state.setdefault("alert", "none")
    bridge("PUT", f"lights/{lid}/state", state)
    try:
        os.remove(hn.restore_path(lid))  # a newer state than any pending restore
    except OSError:
        pass
    return f"Light '{lights[lid].get('name')}' ({lid}) updated: {json.dumps(state)}"


def tool_signal(args):
    signal = args["signal"]
    if signal not in SIGNALS:
        raise ToolError(f"Unknown signal {signal!r}. Use one of: {', '.join(SIGNALS)}")
    if pause_state()["paused"]:
        return "Signals are paused; nothing changed. Use hue_resume to resume."
    cfg = hn.load_config()
    state = hn.STATES[SIGNAL_STATE[signal]]
    if args.get("lights"):
        lights = all_lights()
        targets = [resolve_light(ref, lights) for ref in args["lights"]]
    else:
        targets = hn.notifier_light_ids(cfg)
    failed = []
    for lid in targets:
        try:
            hn.set_light(cfg, state, lid)
        except Exception as e:  # keep going with the other lamps
            failed.append(f"{lid}: {e}")
    if failed:
        raise ToolError(f"Signal '{signal}' failed on: " + "; ".join(failed))
    return f"Signal '{signal}' shown on light(s) {', '.join(targets)}."


def tool_list_scenes(args):
    groups = bridge("GET", "groups")
    out = []
    for sid, scene in bridge("GET", "scenes").items():
        gid = scene.get("group")
        out.append({"id": sid, "name": scene.get("name"),
                    "room": groups.get(gid, {}).get("name") if gid else None})
    return out


def tool_activate_scene(args):
    scenes = bridge("GET", "scenes")
    ref = str(args["scene"]).strip()
    matches = [sid for sid, s in scenes.items()
               if sid == ref or s.get("name", "").lower() == ref.lower()]
    if args.get("room"):
        groups = bridge("GET", "groups")
        room = str(args["room"]).lower()
        matches = [sid for sid in matches
                   if groups.get(scenes[sid].get("group", ""), {}).get("name", "").lower() == room]
    if not matches:
        raise ToolError(f"No scene '{ref}'. Use hue_list_scenes to see them.")
    if len(matches) > 1:
        raise ToolError(f"Several scenes are called '{ref}'; pass room or the scene id.")
    sid = matches[0]
    gid = scenes[sid].get("group") or "0"
    bridge("PUT", f"groups/{gid}/action", {"scene": sid})
    return f"Scene '{scenes[sid].get('name')}' activated."


def tool_pause(args):
    os.makedirs(os.path.dirname(hn.PAUSE_PATH), exist_ok=True)
    minutes = args.get("minutes")
    with open(hn.PAUSE_PATH, "w") as f:
        f.write("" if minutes is None else str(int(time.time() + float(minutes) * 60)))
    return "Signals paused" + ("" if minutes is None else f" for {minutes} minutes") + "."


def tool_resume(args):
    try:
        os.remove(hn.PAUSE_PATH)
    except OSError:
        pass
    return "Signals resumed."


LIGHT_REF = {"type": "string", "description": "Light id or name, e.g. \"7\" or \"Desk\""}

TOOLS = {
    "hue_status": (tool_status, "Bridge address, the notifier's lamps, and whether signals are paused.",
                   {"type": "object", "properties": {}}, True),
    "hue_list_lights": (tool_list_lights, "List the lights: id, name, on/off, brightness %, color support.",
                        {"type": "object", "properties": {}}, True),
    "hue_set_light": (tool_set_light, "Change one light: switch it, dim it, color it or set its white tone.", {
        "type": "object",
        "properties": {
            "light": LIGHT_REF,
            "on": {"type": "boolean"},
            "brightness": {"type": "number", "minimum": 0, "maximum": 100, "description": "Percent; 0 turns it off"},
            "color": {"type": "string", "description": "Hex color, e.g. #FF5733"},
            "color_temperature": {"type": "integer", "minimum": 2000, "maximum": 6500, "description": "Kelvin"},
            "transition_ms": {"type": "integer", "minimum": 0},
        },
        "required": ["light"],
    }, False),
    "hue_signal": (tool_signal,
                   "Show a status on the notifier's lamps (or the given ones): attention = blue blink, "
                   "success = green, error = red, warning = amber, working = dim blue, off. Respects pause.", {
                       "type": "object",
                       "properties": {
                           "signal": {"type": "string", "enum": SIGNALS},
                           "lights": {"type": "array", "items": LIGHT_REF,
                                      "description": "Defaults to the notifier's lamps"},
                       },
                       "required": ["signal"],
                   }, False),
    "hue_list_scenes": (tool_list_scenes, "List the scenes saved on the bridge with their rooms.",
                        {"type": "object", "properties": {}}, True),
    "hue_activate_scene": (tool_activate_scene, "Activate a saved scene by name or id.", {
        "type": "object",
        "properties": {"scene": {"type": "string"},
                       "room": {"type": "string", "description": "Disambiguates scenes with the same name"}},
        "required": ["scene"],
    }, False),
    "hue_pause": (tool_pause, "Pause notifier signals (all senders), for some minutes or until resumed.", {
        "type": "object",
        "properties": {"minutes": {"type": "number", "minimum": 1}},
    }, False),
    "hue_resume": (tool_resume, "Resume notifier signals.", {"type": "object", "properties": {}}, False),
}


# ── protocol ─────────────────────────────────────────────────────────────────

def handle(message):
    method = message.get("method")
    params = message.get("params") or {}

    if method == "initialize":
        requested = params.get("protocolVersion")
        return {
            "protocolVersion": requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": [
            {"name": name, "description": desc, "inputSchema": schema,
             "annotations": {"readOnlyHint": read_only}}
            for name, (_, desc, schema, read_only) in TOOLS.items()
        ]}
    if method == "tools/call":
        name = params.get("name")
        if name not in TOOLS:
            raise LookupError(f"Unknown tool: {name}")
        func = TOOLS[name][0]
        try:
            result = func(params.get("arguments") or {})
            text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, indent=1)
            return {"content": [{"type": "text", "text": text}], "isError": False}
        except FileNotFoundError:
            return _tool_error("Not paired yet: run `python3 hue-notify.py pair` from the plugin folder.")
        except (ToolError, KeyError, ValueError, TypeError) as e:
            return _tool_error(str(e) or e.__class__.__name__)
    raise LookupError(f"Method not found: {method}")


def _tool_error(text):
    return {"content": [{"type": "text", "text": text}], "isError": True}


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            _send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
            continue
        if "id" not in message:
            continue  # a notification, e.g. notifications/initialized
        try:
            _send({"jsonrpc": "2.0", "id": message["id"], "result": handle(message)})
        except LookupError as e:
            _send({"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": str(e)}})
        except Exception as e:  # never die on one bad request
            _send({"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32603, "message": str(e)}})


def _send(payload):
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
