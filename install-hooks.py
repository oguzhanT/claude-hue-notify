#!/usr/bin/env python3
"""Merge (or remove) claude-hue-notify hooks in ~/.claude/settings.json — safely & idempotently.

Usage:
    python3 install-hooks.py            # add/update hooks
    python3 install-hooks.py --remove   # remove our hooks
    python3 install-hooks.py --settings /path/to/settings.json
    python3 install-hooks.py --script   /path/to/hue-notify.py

- Backs up settings.json to settings.json.bak before writing.
- Idempotent: re-running replaces our previous entries (matched by "hue-notify.py"),
  and never touches hooks you added yourself.
"""
import argparse
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MARK = "hue-notify.py"  # how we recognize our own hook commands

# event -> (matcher or None, hue-notify state)
HOOKS = [
    ("Notification",     None,              "waiting"),
    ("PreToolUse",       "AskUserQuestion", "waiting"),
    ("PostToolUse",      "AskUserQuestion", "off"),
    ("UserPromptSubmit", None,              "off"),
    ("Stop",             None,              "stop"),
]


def group(matcher, command):
    g = {"hooks": [{"type": "command", "command": command}]}
    if matcher:
        g["matcher"] = matcher
    return g


def is_ours(grp):
    return any(MARK in h.get("command", "") for h in grp.get("hooks", []))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--settings", default=os.path.expanduser("~/.claude/settings.json"))
    ap.add_argument("--script", default=os.path.join(HERE, "hue-notify.py"))
    ap.add_argument("--remove", action="store_true")
    args = ap.parse_args()

    script = os.path.abspath(os.path.expanduser(args.script))
    settings_path = os.path.expanduser(args.settings)

    # Load (or start fresh)
    data = {}
    if os.path.exists(settings_path):
        with open(settings_path) as f:
            txt = f.read().strip()
        if txt:
            try:
                data = json.loads(txt)
            except json.JSONDecodeError as e:
                print(f"ERROR: {settings_path} is not valid JSON: {e}", file=sys.stderr)
                sys.exit(1)
    else:
        os.makedirs(os.path.dirname(settings_path), exist_ok=True)

    hooks = data.get("hooks", {})

    # Strip any of our previous entries from every event (idempotent + clean remove)
    for event in list(hooks.keys()):
        hooks[event] = [g for g in hooks[event] if not is_ours(g)]
        if not hooks[event]:
            del hooks[event]

    if not args.remove:
        for event, matcher, state in HOOKS:
            hooks.setdefault(event, []).append(
                group(matcher, f"python3 {script} {state}")
            )

    if hooks:
        data["hooks"] = hooks
    else:
        data.pop("hooks", None)

    # Backup then write pretty JSON
    if os.path.exists(settings_path):
        shutil.copy2(settings_path, settings_path + ".bak")
    with open(settings_path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")

    action = "Removed" if args.remove else "Installed"
    print(f"{action} claude-hue-notify hooks in {settings_path}")
    if not args.remove:
        print(f"  -> using script: {script}")
    print("  -> backup: " + settings_path + ".bak")
    print("Restart Claude Code for changes to take effect.")


if __name__ == "__main__":
    main()
