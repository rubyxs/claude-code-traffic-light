#!/usr/bin/env python3
"""
Codex menu bar traffic light for macOS.

Status mapping:
- Green: Codex is actively working on the selected project.
- Yellow: Codex likely needs approval for an escalated command.
- Red: no active turn was detected for the selected project.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import rumps
from AppKit import NSBezierPath, NSColor, NSImage
from Foundation import NSMakeRect


if getattr(sys, "frozen", False):
    os.chdir(os.path.dirname(sys.executable))


CODEX_HOME = Path.home() / ".codex"
STATE_DB_PATH = CODEX_HOME / "state_5.sqlite"
LOG_DB_PATH = CODEX_HOME / "logs_2.sqlite"
APP_DIR = CODEX_HOME / "traffic_light"
SELECTED_FILE = APP_DIR / "selected_project"

POLL_INTERVAL = 0.4
BLINK_INTERVAL = 0.5
MENU_REFRESH_INTERVAL = 3.0
ACTIVE_GRACE_MS = 20_000
APPROVAL_GRACE_MS = 120_000
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


def _find_latest_event_id(messages: list[str], marker: str) -> int:
    for message in messages:
        if marker in message:
            prefix, _, _ = message.partition("|")
            try:
                return int(prefix)
            except ValueError:
                return 0
    return 0


def _load_recent_log_messages(thread_id: str) -> list[str]:
    if not LOG_DB_PATH.exists():
        return []

    with connect_readonly(LOG_DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT id, COALESCE(feedback_log_body, '')
            FROM logs
            WHERE thread_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (thread_id, LOG_SCAN_LIMIT),
        ).fetchall()

    return [f"{row[0]}|{row[1]}" for row in rows]


def infer_state(project: ProjectInfo | None) -> StateSnapshot:
    if project is None:
        return StateSnapshot("red", "No Codex project found", 0)

    messages = _load_recent_log_messages(project.thread_id)
    latest_created = _find_latest_event_id(messages, '"type":"response.created"')
    latest_progress = _find_latest_event_id(messages, '"type":"response.in_progress"')
    latest_completed = _find_latest_event_id(messages, '"type":"response.completed"')
    latest_approval = _find_latest_event_id(messages, "require_escalated")
    latest_activity = max(latest_created, latest_progress, latest_completed, latest_approval)
    age_ms = max(0, now_ms() - project.updated_at_ms)

    if (
        latest_approval > max(latest_created, latest_progress, latest_completed)
        and age_ms <= APPROVAL_GRACE_MS
    ):
        return StateSnapshot("yellow", "Waiting for approval", project.updated_at_ms)

    if max(latest_created, latest_progress) > latest_completed and age_ms <= ACTIVE_GRACE_MS:
        return StateSnapshot("green", "Codex is actively working", project.updated_at_ms)

    if latest_activity and age_ms <= ACTIVE_GRACE_MS and latest_completed == 0:
        return StateSnapshot("green", "Recent Codex activity detected", project.updated_at_ms)

    return StateSnapshot("red", "Idle or turn finished", project.updated_at_ms)


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


def render_status_icon(state: str, blink_on: bool) -> NSImage:
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
    active_state = state if state != "yellow" or blink_on else None
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

        if lamp_state == active_state:
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
        self.state = "red"
        self.blink_on = True
        self.selected_cwd = get_selected_project()
        self.last_projects: list[str] = []
        self.last_menu_build_time = 0.0
        self.notify_config = load_config_value().get("notify", [])
        self.project_lookup: dict[str, ProjectInfo] = {}

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

        if self.notify_config:
            self.menu.add(rumps.separator)
            self.menu.add(rumps.MenuItem("Codex Notify", callback=None))
            self.menu.add(rumps.MenuItem(f"  {self.notify_config[-1]}"))

        self.menu.add(rumps.separator)
        self.menu.add(rumps.MenuItem("Legend", callback=None))
        self.menu.add(rumps.MenuItem("🟢 Working"))
        self.menu.add(rumps.MenuItem("🟡 Approval needed"))
        self.menu.add(rumps.MenuItem("🔴 Idle / finished"))

        self.last_projects = [project.cwd for project in projects]
        self.last_menu_build_time = time.time()

    def _on_select_project(self, sender: rumps.MenuItem) -> None:
        for project in self.project_lookup.values():
            if sender.title == project.label:
                self.selected_cwd = project.cwd
                set_selected_project(project.cwd)
                self.state = "red"
                self.blink_on = True
                self._build_menu()
                self.update_display()
                return

    def check_state(self, _sender) -> None:
        current = get_project_info(self.selected_cwd)
        snapshot = infer_state(current)
        if snapshot.state != self.state:
            self.state = snapshot.state
            self.blink_on = True

        now = time.time()
        projects = list_projects()
        project_paths = [project.cwd for project in projects]
        if (
            project_paths != self.last_projects
            or now - self.last_menu_build_time > MENU_REFRESH_INTERVAL
        ):
            self._build_menu()

        self.update_display()

    def blink(self, _sender) -> None:
        self.blink_on = not self.blink_on
        self.update_display()

    def update_display(self) -> None:
        self.title = None
        self._icon = "__rendered__"
        self._icon_nsimage = render_status_icon(self.state, self.blink_on)
        try:
            self._nsapp.setStatusBarIcon()
        except AttributeError:
            pass


def main() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    TrafficLightApp().run()


if __name__ == "__main__":
    main()
