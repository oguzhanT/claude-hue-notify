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

_spec = importlib.util.spec_from_file_location(
    "hue_notify", os.path.join(os.path.dirname(os.path.abspath(__file__)), "hue-notify.py")
)
hn = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hn)

SERVER_INFO = {"name": "hue", "version": "1.4.0"}
PROTOCOL_VERSIONS = ["2025-06-18", "2025-03-26", "2024-11-05"]

SIGNALS = ["attention", "success", "error", "warning", "working", "off"]
SIGNAL_STATE = {"attention": "waiting", "success": "done", "error": "error",
                "warning": "warning", "working": "working", "off": "off"}


class ToolError(Exception):
    """A problem to report to Claude as a failed tool call, not a protocol error."""


class RpcError(Exception):
    """A JSON-RPC error answer."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def arg(args, name, kind, minimum=None, maximum=None, required=False):
    """Reads one tool argument, checking what the schema promises."""
    if name not in args or args[name] is None:
        if required:
            raise ToolError(f"Missing argument: {name}")
        return None
    value = args[name]
    if kind is bool:
        if not isinstance(value, bool):
            raise ToolError(f"{name} must be true or false")
        return value
    try:
        value = kind(value)
    except (TypeError, ValueError):
        raise ToolError(f"{name} must be a {kind.__name__}")
    if minimum is not None and value < minimum:
        raise ToolError(f"{name} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise ToolError(f"{name} must be at most {maximum}")
    return value


# ── bridge helpers ────────────────────────────────────────────────────────────

def bridge(method, path, body=None):
    cfg = hn.load_config()
    try:
        result = hn.bridge_request(cfg, method, path, body)
    except hn.PinMismatch as e:
        raise ToolError(str(e))
    except OSError as e:
        raise ToolError(f"Cannot reach the bridge: {e}".replace(cfg.get("token") or "\0", "***"))
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

def current_project():
    """The project this Claude Code session runs in."""
    return os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()


def tool_status(args):
    cfg = hn.load_config()
    project = current_project()
    return {
        "bridge_ip": cfg.get("bridge_ip"),
        "notifier_lights": hn.notifier_light_ids(cfg),
        "this_project": project,
        "this_project_lights": hn.project_light_ids(cfg, project),
        "project_lights": cfg.get("project_lights", {}),
        **pause_state(),
    }


def tool_discover_bridges(args):
    bridges = hn.discover_bridges()
    if not bridges:
        return "No bridge found on this network. Ask the user for the bridge's IP address."
    return bridges


def tool_pair_bridge(args):
    ip = arg(args, "ip", str, required=True).strip()
    try:
        cfg, bridge_id = hn.pair_bridge(ip)
    except hn.LinkButtonNotPressed as e:
        raise ToolError(f"{e} Then call hue_pair_bridge again.")
    except OSError as e:
        raise ToolError(f"Cannot reach a bridge at {ip}: {e}")
    lamps = "keeps its lamps" if cfg.get("light_id") else "has no lamp yet: call hue_set_notifier_lights"
    return f"Paired with bridge {bridge_id or ip}; its key is pinned and the config {lamps}."


def _with_config(change):
    try:
        cfg = hn.load_config()
    except FileNotFoundError:
        raise ToolError("Not paired yet: use hue_discover_bridges and hue_pair_bridge first.")
    change(cfg)
    hn.save_config(cfg)
    return cfg


def tool_set_notifier_lights(args):
    refs = args.get("lights")
    if not isinstance(refs, list) or not refs:
        raise ToolError("lights must be a non-empty list of light ids or names")
    lights = all_lights()
    ids = [resolve_light(ref, lights) for ref in refs]

    def change(cfg):
        cfg["light_id"], cfg["light_ids"] = ids[0], ids
    _with_config(change)
    return "Notifier lamps: " + ", ".join(f"{lights[i].get('name')} ({i})" for i in ids)


def tool_assign_project_light(args):
    refs = args.get("lights")
    if not isinstance(refs, list) or not refs:
        raise ToolError("lights must be a non-empty list of light ids or names")
    project = arg(args, "project", str) or current_project()
    lights = all_lights()
    ids = [resolve_light(ref, lights) for ref in refs]

    def change(cfg):
        mapping = cfg.get("project_lights") if isinstance(cfg.get("project_lights"), dict) else {}
        mapping[project] = ids
        cfg["project_lights"] = mapping
    _with_config(change)
    names = ", ".join(f"{lights[i].get('name')} ({i})" for i in ids)
    return f"Sessions in {project} (and its subfolders) now signal on {names}."


def tool_unassign_project_light(args):
    project = arg(args, "project", str) or current_project()
    removed = []

    def change(cfg):
        mapping = cfg.get("project_lights") or {}
        if project in mapping:
            removed.append(mapping.pop(project))
        cfg["project_lights"] = mapping
    _with_config(change)
    if not removed:
        return f"{project} had no lamp of its own."
    return f"{project} uses the notifier's lamps again."


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
    ref = arg(args, "light", str, required=True)
    on = arg(args, "on", bool)
    pct = arg(args, "brightness", float, 0, 100)
    color = arg(args, "color", str)
    kelvin = arg(args, "color_temperature", int, 2000, 6500)
    transition = arg(args, "transition_ms", int, 0)

    lights = all_lights()
    lid = resolve_light(ref, lights)
    state = {}
    if on is not None:
        state["on"] = on
    if pct is not None:
        if pct == 0:
            state["on"] = False
        else:
            state.update({"on": True, "bri": max(1, round(pct * 254 / 100))})
    if color is not None:
        state.update({"on": True, "xy": hex_to_xy(color)})
    if kelvin is not None:
        state.update({"on": True, "ct": max(153, min(500, round(1_000_000 / kelvin)))})
    if transition is not None:
        state["transitiontime"] = round(transition / 100)
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
    signal = arg(args, "signal", str, required=True)
    if signal not in SIGNALS:
        raise ToolError(f"Unknown signal {signal!r}. Use one of: {', '.join(SIGNALS)}")
    if pause_state()["paused"]:
        return "Signals are paused; nothing changed. Use hue_resume to resume."
    cfg = hn.load_config()
    state = hn.STATES[SIGNAL_STATE[signal]]
    refs = args.get("lights")
    if refs is not None and not isinstance(refs, list):
        raise ToolError("lights must be a list of light ids or names")
    if refs:
        lights = all_lights()
        targets = [resolve_light(ref, lights) for ref in refs]
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
    ref = arg(args, "scene", str, required=True).strip()
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
    minutes = arg(args, "minutes", float, 1)
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
    "hue_status": (tool_status,
                   "Bridge address, the notifier's lamps, which lamps this project's sessions use, "
                   "and whether signals are paused.",
                   {"type": "object", "properties": {}}, True),
    "hue_discover_bridges": (tool_discover_bridges,
                             "Find Hue bridges on the local network (mDNS, then Philips' discovery service).",
                             {"type": "object", "properties": {}}, True),
    "hue_pair_bridge": (tool_pair_bridge,
                        "Pair with a bridge: ask the user to press the bridge's round link button first, then "
                        "call this within 30 s. Pins the bridge's key and saves the token.", {
                            "type": "object",
                            "properties": {"ip": {"type": "string", "description": "Bridge IP address"}},
                            "required": ["ip"],
                        }, False),
    "hue_set_notifier_lights": (tool_set_notifier_lights,
                                "Choose the lamp(s) that show Claude's status by default.", {
                                    "type": "object",
                                    "properties": {"lights": {"type": "array", "items": LIGHT_REF, "minItems": 1}},
                                    "required": ["lights"],
                                }, False),
    "hue_assign_project_light": (tool_assign_project_light,
                                 "Give a project its own lamp(s): sessions in that folder signal there instead "
                                 "of on the notifier's lamps. Defaults to this session's project.", {
                                     "type": "object",
                                     "properties": {
                                         "lights": {"type": "array", "items": LIGHT_REF, "minItems": 1},
                                         "project": {"type": "string", "description": "Folder; defaults to this project"},
                                     },
                                     "required": ["lights"],
                                 }, False),
    "hue_unassign_project_light": (tool_unassign_project_light,
                                   "Make a project use the notifier's lamps again. Defaults to this project.", {
                                       "type": "object",
                                       "properties": {"project": {"type": "string"}},
                                   }, False),
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
                   "success = green, error = red, warning = amber, working = dim blue, off. The color stays "
                   "until something changes it (unlike the hook's 'done', which turns off after 5 s); "
                   "send 'off' to clear it. Respects pause.", {
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
            raise RpcError(-32602, f"Unknown tool: {name}")
        func = TOOLS[name][0]
        try:
            result = func(params.get("arguments") or {})
            text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, indent=1)
            return {"content": [{"type": "text", "text": text}], "isError": False}
        except FileNotFoundError:
            return _tool_error("Not paired yet: use hue_discover_bridges, then hue_pair_bridge "
                               "(or run /hue-notify:setup).")
        except (ToolError, ValueError, TypeError) as e:
            return _tool_error(str(e) or e.__class__.__name__)
    raise RpcError(-32601, f"Method not found: {method}")


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
        if not isinstance(message, dict):
            _send({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid request"}})
            continue
        if "id" not in message:
            continue  # a notification, e.g. notifications/initialized
        request_id = message["id"]
        try:
            _send({"jsonrpc": "2.0", "id": request_id, "result": handle(message)})
        except RpcError as e:
            _send({"jsonrpc": "2.0", "id": request_id, "error": {"code": e.code, "message": str(e)}})
        except Exception as e:  # never die on one bad request
            _send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32603, "message": str(e)}})


def _send(payload):
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
