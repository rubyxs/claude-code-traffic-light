#!/usr/bin/env python3
"""
Codex menu bar traffic light for macOS.

Status mapping:
- Animated red/yellow/green sweep: Codex is thinking.
- Steady yellow: Codex is actively working on the selected project.
- Flashing red/yellow: Codex likely needs approval for an escalated command.
- Green: the selected project's latest turn is complete.
"""

from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

import rumps
from AppKit import NSBezierPath, NSColor, NSImage, NSBeep
from Foundation import NSMakeRect


if getattr(sys, "frozen", False):
    os.chdir(os.path.dirname(sys.executable))


CODEX_HOME = Path.home() / ".codex"
STATE_DB_PATH = CODEX_HOME / "state_5.sqlite"
LOG_DB_PATH = CODEX_HOME / "logs_2.sqlite"
APP_DIR = CODEX_HOME / "traffic_light"
SELECTED_FILE = APP_DIR / "selected_project"
AUTO_SWITCH_FILE = APP_DIR / "auto_switch_when_done"
APPROVAL_SOUND_PATH = Path("/System/Library/Sounds/Ping.aiff")
DONE_SOUND_PATH = Path("/System/Library/Sounds/Glass.aiff")

POLL_INTERVAL = 0.4
BLINK_INTERVAL = 0.5
MENU_REFRESH_INTERVAL = 3.0
ACTIVE_GRACE_MS = 20_000
FRESH_ACTIVITY_MS = 8_000
COMPLETION_SETTLE_MS = 3_000
AUTO_SWITCH_DONE_SECONDS = 600
APPROVAL_SOUND_WINDOW_SECONDS = 10.0
APPROVAL_SOUND_INTERVAL_SECONDS = 2.0
SOUND_VOLUME = 1.5
LOG_SCAN_LIMIT = 250

ICON_SCALE = 1.5
ICON_WIDTH = int(48 * ICON_SCALE)
ICON_HEIGHT = int(18 * ICON_SCALE)
HOUSING_INSET_X = int(4 * ICON_SCALE)
HOUSING_INSET_Y = int(2 * ICON_SCALE)
LAMP_DIAMETER = int(8 * ICON_SCALE)
LAMP_GLOW_DIAMETER = int(11 * ICON_SCALE)
LAMP_SPACING = int(4 * ICON_SCALE)


@dataclass
class ProjectInfo:
    cwd: str
    label: str
    thread_id: str
    title: str
    model: str
    updated_at_ms: int


@dataclass
class StateSnapshot:
    state: str
    reason: str
    updated_at_ms: int


def now_ms() -> int:
    return int(time.time() * 1000)


def is_today_local(timestamp_ms: int) -> bool:
    if timestamp_ms <= 0:
        return False
    return datetime.fromtimestamp(timestamp_ms / 1000).date() == datetime.now().date()


def load_config_value() -> dict:
    config_path = CODEX_HOME / "config.toml"
    if not config_path.exists():
        return {}

    try:
        import tomllib

        return tomllib.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def get_default_selected_project() -> str | None:
    projects = list_projects()
    return projects[0].cwd if projects else None


def get_selected_project() -> str | None:
    try:
        value = SELECTED_FILE.read_text(encoding="utf-8").strip()
        if value:
            return value
    except Exception:
        pass
    return get_default_selected_project()


def set_selected_project(cwd: str) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    SELECTED_FILE.write_text(cwd, encoding="utf-8")


def get_auto_switch_enabled() -> bool:
    try:
        value = AUTO_SWITCH_FILE.read_text(encoding="utf-8").strip().lower()
        return value in {"1", "true", "yes", "on"}
    except Exception:
        return False


def set_auto_switch_enabled(enabled: bool) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    AUTO_SWITCH_FILE.write_text("1\n" if enabled else "0\n", encoding="utf-8")


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def _project_label_parts(cwds: Iterable[str]) -> dict[str, str]:
    paths = [Path(cwd) for cwd in cwds]
    labels = {str(path): path.name or str(path) for path in paths}

    by_name: dict[str, list[Path]] = {}
    for path in paths:
        by_name.setdefault(path.name or str(path), []).append(path)

    for same_name_paths in by_name.values():
        if len(same_name_paths) == 1:
            continue
        for path in same_name_paths:
            parent = path.parent.name
            labels[str(path)] = f"{parent}/{path.name}" if parent else str(path)

    return labels


def list_projects() -> list[ProjectInfo]:
    if not STATE_DB_PATH.exists():
        return []

    with connect_readonly(STATE_DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT id, cwd, title, COALESCE(model, ''), COALESCE(updated_at_ms, updated_at * 1000)
            FROM (
                SELECT
                    id,
                    cwd,
                    title,
                    model,
                    updated_at,
                    updated_at_ms,
                    ROW_NUMBER() OVER (
                        PARTITION BY cwd
                        ORDER BY COALESCE(updated_at_ms, updated_at * 1000) DESC, id DESC
                    ) AS rn
                FROM threads
                WHERE archived = 0
            )
            WHERE rn = 1
            ORDER BY updated_at_ms DESC, id DESC
            """
        ).fetchall()

    if not rows:
        return []

    labels = _project_label_parts(row[1] for row in rows)
    return [
        ProjectInfo(
            cwd=row[1],
            label=labels.get(row[1], Path(row[1]).name or row[1]),
            thread_id=row[0],
            title=row[2] or "(untitled)",
            model=row[3] or "unknown",
            updated_at_ms=int(row[4] or 0),
        )
        for row in rows
    ]


def get_project_info(cwd: str | None) -> ProjectInfo | None:
    if not cwd:
        return None
    for project in list_projects():
        if project.cwd == cwd:
            return project
    return None


def get_fallback_project(current_cwd: str | None, projects: list[ProjectInfo]) -> ProjectInfo | None:
    if not projects:
        return None

    default_cwd = get_default_selected_project()
    if default_cwd and default_cwd != current_cwd:
        for project in projects:
            if project.cwd == default_cwd:
                return project

    for project in projects:
        if project.cwd != current_cwd:
            return project

    return None


def _find_latest_event_id(messages: list[str], marker: str) -> int:
    for message in messages:
        if marker in message:
            try:
                return int(message.split("|", 1)[0])
            except ValueError:
                return 0
    return 0


EVENT_TYPE_PATTERNS = (
    re.compile(r'websocket event: \{"type":"([^"]+)"'),
    re.compile(r'Received message \{"type":"([^"]+)"'),
)


def _find_latest_response_event_type(messages: list[str]) -> tuple[str | None, int]:
    for message in messages:
        for pattern in EVENT_TYPE_PATTERNS:
            match = pattern.search(message)
            if match:
                try:
                    return match.group(1), int(message.split("|", 1)[0])
                except ValueError:
                    return match.group(1), 0
    return None, 0


APPROVAL_SANDBOX_MARKERS = (
    '"sandbox_permissions":"require_escalated"',
    '"sandbox_permissions": "require_escalated"',
)

APPROVAL_JUSTIFICATION_MARKERS = (
    '"justification":"',
    '"justification": "',
)

STALL_TARGETS = {
    "codex_client::transport",
    "codex_api::sse::responses",
    "codex_api::endpoint::responses_websocket",
}

STALL_MARKERS = (
    "reconnecting",
    "stream error",
    "connection error",
    "network error",
    "failed to connect",
)

STALL_EXCLUDE_MARKERS = (
    "stream.poll_next",
    "websocketstream.with_context",
    "stream.with_context poll_next -> read()",
    "wouldblock",
    "stream_request{",
    'transport="responses_websocket"',
    'transport="responses_http"',
    "websocket request:",
)


def _is_approval_toolcall(body: str) -> bool:
    return (
        "ToolCall:" in body
        and any(marker in body for marker in APPROVAL_SANDBOX_MARKERS)
        and any(marker in body for marker in APPROVAL_JUSTIFICATION_MARKERS)
    )


def _find_latest_exact_approval_event_id(messages: list[str]) -> int:
    for message in messages:
        parts = message.split("|", 2)
        if len(parts) != 3:
            continue
        _, target, body = parts
        if target != "codex_core::stream_events_utils":
            continue
        if _is_approval_toolcall(body):
            try:
                return int(parts[0])
            except ValueError:
                return 0
    return 0


def _find_latest_approval_decision_event_id(messages: list[str]) -> int:
    for message in messages:
        parts = message.split("|", 2)
        if len(parts) != 3:
            continue
        id_part, target, body = parts
        if target != "codex_core::session::handlers":
            continue
        if "ExecApproval" not in body or "decision:" not in body:
            continue
        try:
            return int(id_part)
        except ValueError:
            return 0
    return 0


def _find_latest_stream_toolcall_event(messages: list[str]) -> tuple[int, bool]:
    for message in messages:
        parts = message.split("|", 2)
        if len(parts) != 3:
            continue
        id_part, target, body = parts
        if target != "codex_core::stream_events_utils":
            continue
        if "ToolCall:" not in body:
            continue
        try:
            event_id = int(id_part)
        except ValueError:
            event_id = 0
        is_approval = _is_approval_toolcall(body)
        return event_id, is_approval
    return 0, False


def _find_latest_stalled_event_id(messages: list[str]) -> int:
    for message in messages:
        parts = message.split("|", 2)
        if len(parts) != 3:
            continue
        id_part, target, body = parts
        body_lower = body.lower()
        if any(marker in body_lower for marker in STALL_EXCLUDE_MARKERS):
            continue
        if target in STALL_TARGETS and any(marker in body_lower for marker in STALL_MARKERS):
            try:
                return int(id_part)
            except ValueError:
                return 0
    return 0


def _find_latest_tool_event_id(messages: list[str]) -> int:
    tool_markers = (
        '"type":"response.output_item.done","item":{"id":"fc_',
        '"type":"response.function_call_arguments.done"',
        'tool_name="exec_command"',
        'tool_name="apply_patch"',
        'ToolCall:',
    )
    for message in messages:
        parts = message.split("|", 2)
        if len(parts) == 3:
            _, target, body = parts
            if target == "codex_core::stream_events_utils" and _is_approval_toolcall(body):
                continue
        if any(marker in message for marker in tool_markers):
            try:
                return int(message.split("|", 1)[0])
            except ValueError:
                return 0
    return 0


def _load_recent_log_messages(thread_id: str) -> list[str]:
    if not LOG_DB_PATH.exists():
        return []

    with connect_readonly(LOG_DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT id, target, COALESCE(feedback_log_body, '')
            FROM logs
            WHERE thread_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (thread_id, LOG_SCAN_LIMIT),
        ).fetchall()

    return [f"{row[0]}|{row[1]}|{row[2]}" for row in rows]


def infer_state(project: ProjectInfo | None) -> StateSnapshot:
    if project is None:
        return StateSnapshot("done", "No Codex project found", 0)

    messages = _load_recent_log_messages(project.thread_id)
    latest_event_type, latest_event_id = _find_latest_response_event_type(messages)
    latest_approval = _find_latest_exact_approval_event_id(messages)
    latest_approval_decision = _find_latest_approval_decision_event_id(messages)
    latest_stream_toolcall_id, latest_stream_toolcall_is_approval = _find_latest_stream_toolcall_event(messages)
    latest_stalled = _find_latest_stalled_event_id(messages)
    latest_tool = _find_latest_tool_event_id(messages)
    age_ms = max(0, now_ms() - project.updated_at_ms)
    is_fresh_activity = age_ms <= FRESH_ACTIVITY_MS
    is_recently_active = age_ms <= ACTIVE_GRACE_MS
    if latest_approval > latest_approval_decision:
        return StateSnapshot("approval", "Waiting for confirmation", project.updated_at_ms)

    latest_completed_is_stable = (
        latest_event_type == "response.completed"
        and latest_event_id >= max(latest_approval, latest_tool, latest_stream_toolcall_id, latest_stalled)
        and age_ms >= COMPLETION_SETTLE_MS
    )
    if latest_completed_is_stable:
        return StateSnapshot("done", "Development complete", project.updated_at_ms)

    thinking_events = {
        "response.created",
        "response.in_progress",
        "response.output_text.delta",
        "response.output_text.done",
        "response.content_part.done",
    }
    working_events = {
        "response.function_call_arguments.delta",
        "response.function_call_arguments.done",
        "response.output_item.added",
    }

    if latest_event_type in thinking_events | working_events and is_recently_active:
        if is_fresh_activity:
            return StateSnapshot("working", "Active development", project.updated_at_ms)

        return StateSnapshot("thinking", "Thinking", project.updated_at_ms)

    if latest_stream_toolcall_is_approval and latest_stream_toolcall_id > max(latest_event_id, latest_tool):
        return StateSnapshot("approval", "Waiting for confirmation", project.updated_at_ms)

    if latest_stalled > max(latest_event_id, latest_tool, latest_approval, latest_stream_toolcall_id) and not is_fresh_activity:
        return StateSnapshot("stalled", "Connection or stream issue", project.updated_at_ms)

    if latest_stream_toolcall_id > max(latest_event_id, latest_approval) and is_fresh_activity:
        return StateSnapshot("working", "Active development", project.updated_at_ms)

    if latest_tool and is_fresh_activity:
        return StateSnapshot("working", "Active development", project.updated_at_ms)

    if latest_event_id and is_recently_active:
        return StateSnapshot("thinking", "Thinking", project.updated_at_ms)

    if latest_approval > max(latest_event_id, latest_tool):
        return StateSnapshot("approval", "Waiting for confirmation", project.updated_at_ms)

    if project.updated_at_ms:
        return StateSnapshot("thinking", "Thinking", project.updated_at_ms)

    return StateSnapshot("done", "No Codex activity yet", project.updated_at_ms)


def _make_color(red: int, green: int, blue: int, alpha: float = 1.0) -> NSColor:
    return NSColor.colorWithCalibratedRed_green_blue_alpha_(
        red / 255.0,
        green / 255.0,
        blue / 255.0,
        alpha,
    )


def _draw_circle(rect, color: NSColor) -> None:
    path = NSBezierPath.bezierPathWithOvalInRect_(rect)
    color.setFill()
    path.fill()


def render_status_icon(state: str, animation_step: int) -> NSImage:
    image = NSImage.alloc().initWithSize_((ICON_WIDTH, ICON_HEIGHT))
    image.lockFocus()

    housing_rect = NSMakeRect(
        HOUSING_INSET_X,
        HOUSING_INSET_Y,
        ICON_WIDTH - (HOUSING_INSET_X * 2),
        ICON_HEIGHT - (HOUSING_INSET_Y * 2),
    )
    housing_radius = housing_rect.size.height / 2.0

    # Tahoe-like semi-transparent glass housing.
    housing = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
        housing_rect,
        housing_radius,
        housing_radius,
    )
    _make_color(34, 39, 48, 0.46).setFill()
    housing.fill()

    housing_edge = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
        housing_rect,
        housing_radius,
        housing_radius,
    )
    _make_color(255, 255, 255, 0.16).setStroke()
    housing_edge.setLineWidth_(0.8)
    housing_edge.stroke()

    # Soft glass highlight across the top half.
    highlight_rect = NSMakeRect(
        housing_rect.origin.x + 1,
        housing_rect.origin.y + (housing_rect.size.height / 2.0),
        housing_rect.size.width - 2,
        (housing_rect.size.height / 2.0) - 1,
    )
    highlight = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
        highlight_rect,
        highlight_rect.size.height / 2.0,
        highlight_rect.size.height / 2.0,
    )
    _make_color(255, 255, 255, 0.08).setFill()
    highlight.fill()

    lamp_y = (ICON_HEIGHT - LAMP_DIAMETER) / 2.0
    glow_y = (ICON_HEIGHT - LAMP_GLOW_DIAMETER) / 2.0
    lamp_states = ["red", "yellow", "green"]
    lamp_colors = {
        "red": _make_color(255, 90, 84, 0.92),
        "yellow": _make_color(245, 191, 72, 0.93),
        "green": _make_color(67, 212, 113, 0.92),
    }
    lamp_glows = {
        "red": _make_color(255, 90, 84, 0.18),
        "yellow": _make_color(245, 191, 72, 0.16),
        "green": _make_color(67, 212, 113, 0.16),
    }
    off_outer = _make_color(90, 96, 106, 0.58)
    off_inner = _make_color(146, 152, 164, 0.28)
    thinking_index = animation_step % 3
    approval_flash_on = (animation_step % 2) == 0

    lamps_total_width = (LAMP_DIAMETER * 3) + (LAMP_SPACING * 2)
    first_lamp_x = housing_rect.origin.x + ((housing_rect.size.width - lamps_total_width) / 2.0)
    for index, lamp_state in enumerate(lamp_states):
        lamp_x = first_lamp_x + index * (LAMP_DIAMETER + LAMP_SPACING)
        lamp_rect = NSMakeRect(lamp_x, lamp_y, LAMP_DIAMETER, LAMP_DIAMETER)
        glow_rect = NSMakeRect(
            lamp_x - ((LAMP_GLOW_DIAMETER - LAMP_DIAMETER) / 2.0),
            glow_y,
            LAMP_GLOW_DIAMETER,
            LAMP_GLOW_DIAMETER,
        )

        lamp_on = False
        if state == "thinking":
            lamp_on = index == thinking_index
        elif state == "working":
            lamp_on = lamp_state == "yellow"
        elif state == "approval":
            lamp_on = lamp_state in ("red", "yellow") and approval_flash_on
        elif state == "stalled":
            lamp_on = lamp_state == "red"
        elif state == "done":
            lamp_on = lamp_state == "green"

        if lamp_on:
            _draw_circle(glow_rect, lamp_glows[lamp_state])
            _draw_circle(lamp_rect, lamp_colors[lamp_state])
            _draw_circle(
                NSMakeRect(lamp_x + 1.4, lamp_y + 4.0, LAMP_DIAMETER - 2.8, LAMP_DIAMETER - 4.8),
                _make_color(255, 255, 255, 0.14),
            )
        else:
            _draw_circle(lamp_rect, off_outer)
            _draw_circle(
                NSMakeRect(lamp_x + 1.0, lamp_y + 1.0, LAMP_DIAMETER - 2.0, LAMP_DIAMETER - 2.0),
                off_inner,
            )

    image.unlockFocus()
    image.setTemplate_(False)
    image.setSize_((ICON_WIDTH, ICON_HEIGHT))
    return image


class TrafficLightApp(rumps.App):
    def __init__(self) -> None:
        super().__init__("Codex Traffic Light", title=None, quit_button="Quit")
        self.state = "done"
        self.animation_step = 0
        self.selected_cwd = get_selected_project()
        self.auto_switch_enabled = get_auto_switch_enabled()
        self.done_since_ts: float | None = None
        self.last_projects: list[str] = []
        self.last_menu_build_time = 0.0
        self.notify_config = load_config_value().get("notify", [])
        self.project_lookup: dict[str, ProjectInfo] = {}
        self.approval_sound_started_at: float | None = None
        self.last_approval_sound_at: float | None = None

        rumps.Timer(self.check_state, POLL_INTERVAL).start()
        rumps.Timer(self.blink, BLINK_INTERVAL).start()

        self._build_menu()
        self.update_display()

    def _refresh_projects(self) -> list[ProjectInfo]:
        projects = list_projects()
        self.project_lookup = {project.cwd: project for project in projects}
        if projects and self.selected_cwd not in self.project_lookup:
            self.selected_cwd = projects[0].cwd
            set_selected_project(self.selected_cwd)
        return projects

    def _build_menu(self) -> None:
        self.menu.clear()
        projects = self._refresh_projects()

        project_menu = rumps.MenuItem("Projects")
        if not projects:
            project_menu.add(rumps.MenuItem("(No active Codex projects)"))
        else:
            for project in projects:
                item = rumps.MenuItem(project.label)
                item.set_callback(self._on_select_project)
                if project.cwd == self.selected_cwd:
                    item.state = True
                project_menu.add(item)
        self.menu.add(project_menu)

        current = self.project_lookup.get(self.selected_cwd or "")
        snapshot = infer_state(current)

        self.menu.add(rumps.separator)
        self.menu.add(rumps.MenuItem("Current Project", callback=None))
        self.menu.add(rumps.MenuItem(f"  {current.label if current else 'None'}"))
        if current:
            self.menu.add(rumps.MenuItem(f"  Model: {current.model}"))
            self.menu.add(rumps.MenuItem(f"  Task: {current.title[:60]}"))
        self.menu.add(rumps.MenuItem(f"  State: {snapshot.reason}"))

        self.menu.add(rumps.separator)
        auto_switch_state = "On" if self.auto_switch_enabled else "Off"
        auto_switch_item = rumps.MenuItem(f"Auto-switch when done: {auto_switch_state}")
        auto_switch_item.set_callback(self._on_toggle_auto_switch)
        self.menu.add(auto_switch_item)

        if self.notify_config:
            self.menu.add(rumps.separator)
            self.menu.add(rumps.MenuItem("Codex Notify", callback=None))
            self.menu.add(rumps.MenuItem(f"  {self.notify_config[-1]}"))

        self.menu.add(rumps.separator)
        self.menu.add(rumps.MenuItem("Legend", callback=None))
        self.menu.add(rumps.MenuItem("跑马灯 - Thinking"))
        self.menu.add(rumps.MenuItem("🟡 - Active development"))
        self.menu.add(rumps.MenuItem("🔴🟡 flashing - Confirmation needed"))
        self.menu.add(rumps.MenuItem("🔴 - Connection or stream issue"))
        self.menu.add(rumps.MenuItem("🟢 - Development complete"))

        self.last_projects = [project.cwd for project in projects]
        self.last_menu_build_time = time.time()

    def _on_select_project(self, sender: rumps.MenuItem) -> None:
        for project in self.project_lookup.values():
            if sender.title == project.label:
                self.selected_cwd = project.cwd
                set_selected_project(project.cwd)
                self.done_since_ts = None
                self.state = "done"
                self.animation_step = 0
                self._build_menu()
                self.update_display()
                return

    def _on_toggle_auto_switch(self, sender: rumps.MenuItem) -> None:
        self.auto_switch_enabled = not self.auto_switch_enabled
        set_auto_switch_enabled(self.auto_switch_enabled)
        if not self.auto_switch_enabled:
            self.done_since_ts = None
        self._build_menu()

    def _switch_to_project(self, project: ProjectInfo) -> None:
        self.selected_cwd = project.cwd
        set_selected_project(project.cwd)
        self.done_since_ts = None
        self.state = "done"
        self.animation_step = 0

    def _play_sound(self, sound_path: Path) -> None:
        try:
            subprocess.Popen(
                ["afplay", "--volume", str(SOUND_VOLUME), str(sound_path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            try:
                NSBeep()
            except Exception:
                pass

    def _play_approval_sound(self) -> None:
        self._play_sound(APPROVAL_SOUND_PATH)

    def _play_done_sound(self) -> None:
        self._play_sound(DONE_SOUND_PATH)

    def _reset_approval_sound(self) -> None:
        self.approval_sound_started_at = None
        self.last_approval_sound_at = None

    def _maybe_play_approval_sound(self, now: float) -> None:
        if self.approval_sound_started_at is None:
            self.approval_sound_started_at = now
            self.last_approval_sound_at = now
            self._play_approval_sound()
            return

        if now - self.approval_sound_started_at >= APPROVAL_SOUND_WINDOW_SECONDS:
            return

        if (
            self.last_approval_sound_at is None
            or now - self.last_approval_sound_at >= APPROVAL_SOUND_INTERVAL_SECONDS
        ):
            self.last_approval_sound_at = now
            self._play_approval_sound()

    def _maybe_auto_switch(
        self,
        current: ProjectInfo | None,
        snapshot: StateSnapshot,
        projects: list[ProjectInfo],
    ) -> tuple[ProjectInfo | None, StateSnapshot]:
        if not self.auto_switch_enabled or current is None:
            self.done_since_ts = None
            return current, snapshot

        if snapshot.state != "done":
            self.done_since_ts = None
            return current, snapshot

        now = time.time()
        if self.done_since_ts is None:
            self.done_since_ts = now
            return current, snapshot

        if now - self.done_since_ts < AUTO_SWITCH_DONE_SECONDS:
            return current, snapshot

        for project in projects:
            if project.cwd == current.cwd:
                continue
            if not is_today_local(project.updated_at_ms):
                continue
            candidate_snapshot = infer_state(project)
            if candidate_snapshot.state != "done":
                self._switch_to_project(project)
                return project, candidate_snapshot

        fallback_project = get_fallback_project(current.cwd, projects)
        if fallback_project is not None:
            fallback_snapshot = infer_state(fallback_project)
            self._switch_to_project(fallback_project)
            return fallback_project, fallback_snapshot

        return current, snapshot

    def check_state(self, _sender) -> None:
        projects = list_projects()
        self.project_lookup = {project.cwd: project for project in projects}
        if projects and self.selected_cwd not in self.project_lookup:
            self.selected_cwd = projects[0].cwd
            set_selected_project(self.selected_cwd)
            self.done_since_ts = None

        current = self.project_lookup.get(self.selected_cwd or "")
        snapshot = infer_state(current)
        current, snapshot = self._maybe_auto_switch(current, snapshot, projects)
        now = time.time()

        if snapshot.state != self.state:
            if snapshot.state == "done":
                self._play_done_sound()
            if snapshot.state != "approval":
                self._reset_approval_sound()
            self.state = snapshot.state
            self.animation_step = 0

        if snapshot.state == "approval":
            self._maybe_play_approval_sound(now)
        else:
            self._reset_approval_sound()

        project_paths = [project.cwd for project in projects]
        if (
            project_paths != self.last_projects
            or now - self.last_menu_build_time > MENU_REFRESH_INTERVAL
        ):
            self._build_menu()

        self.update_display()

    def blink(self, _sender) -> None:
        self.animation_step += 1
        self.update_display()

    def update_display(self) -> None:
        self.title = None
        self._icon = "__rendered__"
        self._icon_nsimage = render_status_icon(self.state, self.animation_step)
        try:
            self._nsapp.setStatusBarIcon()
        except AttributeError:
            pass


def main() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    TrafficLightApp().run()


if __name__ == "__main__":
    main()
