# Codex Traffic Light

Small macOS menu bar traffic light for Codex.

It is adapted from the Claude version, but instead of modifying hooks it reads Codex's local state directly:

- `~/.codex/state_5.sqlite`
- `~/.codex/logs_2.sqlite`

## Status mapping

- `🟢` green: Codex is actively working on the selected project
- `🟡` yellow blinking: Codex likely needs approval for an escalated command
- `🔴` red: the project is idle or the latest turn has finished

## How it works

The app watches the most recent non-archived Codex thread for each project path and infers state from recent log events:

- `response.created` / `response.in_progress` -> active
- recent `require_escalated` request with no newer response status -> approval needed
- otherwise -> idle

This is heuristic because Codex does not currently expose the same hook system that the Claude version uses.

## One-time install for auto-start

```bash
chmod +x install.sh
./install.sh
```

That will:

- create a local virtual environment
- install Python dependencies
- install a `launchd` agent
- start the traffic light immediately
- make it launch automatically after login and restart

## Stop or remove it

```bash
chmod +x uninstall.sh
./uninstall.sh
```

## Run manually

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python traffic_light.py
```

## Build a macOS app

```bash
chmod +x build.sh
./build.sh
```

The built app will be placed at `dist/CodexTrafficLight.app`.

## Notes

- The project list comes from your local Codex thread database.
- The selected project is stored in `~/.codex/traffic_light/selected_project`.
- Runtime logs are written to `~/.codex/traffic_light/launchd.out.log` and `~/.codex/traffic_light/launchd.err.log`.
- If Codex changes its local database schema in a future release, the detection logic may need a small update.
