---
description: Connect your Hue bridge and pick the lamp that shows Claude's status
---

Set up Hue Notify for the user with the `hue` MCP tools, one step at a time, and keep your messages short.

1. Call `hue_status`. If it works and lists notifier lamps, Hue Notify is already set up: say which lamps it uses, offer to change them or to give this project its own lamp (`hue_assign_project_light`), and stop.
2. Otherwise call `hue_discover_bridges`. If it finds none, ask the user for the bridge's IP address (shown in the Hue app under Settings → My Hue System → the bridge).
3. Ask the user to press the round link button on top of the bridge and tell you when they have. Then call `hue_pair_bridge` with the IP. If it says the button was not pressed, ask once more and retry.
4. Call `hue_list_lights` and ask which lamp should show Claude's status (several are fine). Prefer lamps with color. Call `hue_set_notifier_lights`.
5. Call `hue_signal` with `success` so the user sees the lamp turn green, then `off` once they confirm.
6. Tell them what the colors mean (blue blinking: Claude needs you; green: done; off: you replied), that it takes effect in new sessions, and that `/hue-notify:setup` can be run again any time. Mention they can give a project its own lamp ("use the desk lamp for this project").
