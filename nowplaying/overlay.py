"""Frameless always-on-top lyric HUD for the Plasma desktop."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (QAction, QColor, QFont, QFontMetrics, QIcon, QPainter,
                         QPainterPath, QPen, QPixmap)
from PyQt6.QtWidgets import (QApplication, QMenu, QSystemTrayIcon, QVBoxLayout,
                             QWidget)

from . import client, config
from .state import State

APP_ID = "nowplaying"
WIDTH = 880
HEIGHT = 132
FPS = 30


def settings_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    d = Path(base) / APP_ID
    d.mkdir(parents=True, exist_ok=True)
    return d / "overlay.json"


def load_settings() -> dict:
    try:
        return json.loads(settings_path().read_text())
    except (OSError, ValueError):
        return {}


def save_settings(data: dict) -> None:
    try:
        settings_path().write_text(json.dumps(data, indent=2))
    except OSError:
        pass


class Overlay(QWidget):
    state_changed = pyqtSignal(object)

    def __init__(self, source: str = "auto", click_through: bool = False) -> None:
        super().__init__()
        self.state = State()
        self.source = source
        self._drag_origin = None
        self._stop = threading.Event()

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(WIDTH, HEIGHT)
        self.setWindowTitle("nowplaying overlay")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._restore_position()
        if click_through:
            self.set_click_through(True)

        self.state_changed.connect(self._on_state)
        threading.Thread(target=self._reader, daemon=True).start()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.update)
        self._timer.start(int(1000 / FPS))

    # --- placement -----------------------------------------------------------
    def _restore_position(self) -> None:
        saved = load_settings().get("pos")
        screen = QApplication.primaryScreen()
        area = screen.availableGeometry() if screen else None
        if saved and isinstance(saved, list) and len(saved) == 2:
            self.move(int(saved[0]), int(saved[1]))
        elif area:
            # Default: centred just above the panel.
            self.move(area.center().x() - WIDTH // 2, area.bottom() - HEIGHT - 12)

    def set_click_through(self, on: bool) -> None:
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, on)
        s = load_settings()
        s["click_through"] = on
        save_settings(s)

    # --- daemon feed ---------------------------------------------------------
    def _reader(self) -> None:
        while not self._stop.is_set():
            try:
                sock = client.connect(autostart=True, source=self.source)
                for state in client.stream(sock):
                    self.state_changed.emit(state)
                    if self._stop.is_set():
                        return
            except (ConnectionError, OSError, socket.error):
                pass
            if self._stop.wait(2):
                return

    def _on_state(self, state: State) -> None:
        self.state = state
        self.update()

    # --- painting ------------------------------------------------------------
    def _current_lines(self) -> tuple[str, str, str]:
        s = self.state
        pos = s.position()
        if not s.lyrics:
            if s.status == "playing":
                return (s.message or "no synced lyrics", "", "")
            return ({
                "idle": "waiting for audio…",
                "searching": "listening…",
                "paused": "paused",
                "error": s.message or "error",
            }.get(s.status, s.message or "…"), "", "")
        idx = -1
        lo, hi = 0, len(s.lyrics) - 1
        while lo <= hi:
            mid = (lo + hi) // 2
            if s.lyrics[mid][0] <= pos:
                idx, lo = mid, mid + 1
            else:
                hi = mid - 1
        prev_line = s.lyrics[idx - 1][1] if idx > 0 else ""
        cur = s.lyrics[idx][1] if idx >= 0 else "♪"
        nxt = s.lyrics[idx + 1][1] if 0 <= idx + 1 < len(s.lyrics) else ""
        return cur or "♪", nxt, prev_line

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        path = QPainterPath()
        path.addRoundedRect(0.0, 0.0, float(WIDTH), float(HEIGHT), 18.0, 18.0)
        painter.fillPath(path, QColor(12, 12, 16, 214))
        painter.setPen(QPen(QColor(190, 120, 255, 90), 1.5))
        painter.drawPath(path)

        s = self.state
        cur, nxt, _prev = self._current_lines()

        # Title row
        painter.setFont(QFont("Noto Sans", 10, QFont.Weight.DemiBold))
        painter.setPen(QColor(150, 200, 255, 220))
        title = f"♪  {s.artist} — {s.title}" if s.title else "♪  nowplaying"
        if s.duration:
            title += f"   ·   {int(s.position()//60)}:{int(s.position()%60):02d}"
            title += f" / {int(s.duration//60)}:{int(s.duration%60):02d}"
        painter.drawText(24, 30, self._elide(title, painter, WIDTH - 48))

        # Current line
        painter.setFont(QFont("Noto Sans", 19, QFont.Weight.Bold))
        painter.setPen(QColor(255, 255, 255, 245))
        painter.drawText(24, 74, self._elide(cur, painter, WIDTH - 48))

        # Next line
        painter.setFont(QFont("Noto Sans", 12))
        painter.setPen(QColor(190, 190, 200, 150))
        painter.drawText(24, 104, self._elide(nxt, painter, WIDTH - 48))

        # Progress hairline
        if s.duration:
            frac = max(0.0, min(1.0, s.position() / s.duration))
            painter.setPen(QPen(QColor(190, 120, 255, 170), 2))
            painter.drawLine(24, HEIGHT - 12, 24 + int((WIDTH - 48) * frac), HEIGHT - 12)

    @staticmethod
    def _elide(text: str, painter: QPainter, width: int) -> str:
        metrics = QFontMetrics(painter.font())
        return metrics.elidedText(text, Qt.TextElideMode.ElideRight, width)

    # --- interaction ---------------------------------------------------------
    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_origin = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag_origin is not None:
            self.move(event.globalPosition().toPoint() - self._drag_origin)
            event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._drag_origin = None
        s = load_settings()
        s["pos"] = [self.x(), self.y()]
        save_settings(s)

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        self._menu().exec(event.globalPos())

    def _menu(self) -> QMenu:
        menu = QMenu(self)
        ct = QAction("Click-through (unlock from tray)", self, checkable=True)
        ct.setChecked(self.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents))
        ct.triggered.connect(self.set_click_through)
        menu.addAction(ct)
        reset = QAction("Reset position", self)
        reset.triggered.connect(lambda: (save_settings({}), self._restore_position()))
        menu.addAction(reset)
        menu.addSeparator()
        quit_action = QAction("Quit overlay", self)
        quit_action.triggered.connect(QApplication.quit)
        menu.addAction(quit_action)
        return menu


def _tray_icon(app: QApplication, overlay: Overlay) -> QSystemTrayIcon | None:
    if not QSystemTrayIcon.isSystemTrayAvailable():
        return None
    pixmap = QPixmap(64, 64)
    pixmap.fill(QColor(0, 0, 0, 0))
    p = QPainter(pixmap)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(QColor(200, 140, 255))
    p.setFont(QFont("Noto Sans", 44, QFont.Weight.Bold))
    p.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "♪")
    p.end()

    tray = QSystemTrayIcon(QIcon(pixmap), app)
    tray.setToolTip("nowplaying")
    menu = QMenu()
    show = QAction("Show / hide overlay", menu)
    show.triggered.connect(lambda: overlay.setVisible(not overlay.isVisible()))
    menu.addAction(show)
    ct = QAction("Click-through", menu, checkable=True)
    ct.setChecked(overlay.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents))
    ct.triggered.connect(overlay.set_click_through)
    menu.addAction(ct)
    menu.addSeparator()
    quit_action = QAction("Quit", menu)
    quit_action.triggered.connect(app.quit)
    menu.addAction(quit_action)
    tray.setContextMenu(menu)
    tray.show()
    return tray


def install_kwin_rule() -> int:
    """Force keep-above via a KWin window rule.

    Wayland has no client-side 'always on top', so KWin has to be told. This
    writes a rule matching the overlay's app id and reloads KWin.
    """
    rules = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "kwinrulesrc"
    key = "nowplaying-overlay"
    existing = rules.read_text() if rules.exists() else ""
    if f"[{key}]" in existing:
        print("KWin rule already installed")
    else:
        for k, v in [
            ("Description", "nowplaying overlay: keep above"),
            ("above", "true"),
            ("aboverule", "2"),
            ("skiptaskbar", "true"),
            ("skiptaskbarrule", "2"),
            ("skippager", "true"),
            ("skippagerrule", "2"),
            ("wmclass", APP_ID),
            ("wmclasscomplete", "false"),
            ("wmclassmatch", "1"),
        ]:
            subprocess.run(["kwriteconfig6", "--file", "kwinrulesrc",
                            "--group", key, "--key", k, v], check=False)
        # Register the rule in the index KWin actually reads.
        current = subprocess.run(
            ["kreadconfig6", "--file", "kwinrulesrc", "--group", "General",
             "--key", "rules"], capture_output=True, text=True, check=False
        ).stdout.strip()
        names = [n for n in current.split(",") if n] if current else []
        if key not in names:
            names.append(key)
        subprocess.run(["kwriteconfig6", "--file", "kwinrulesrc", "--group", "General",
                        "--key", "rules", ",".join(names)], check=False)
        subprocess.run(["kwriteconfig6", "--file", "kwinrulesrc", "--group", "General",
                        "--key", "count", str(len(names))], check=False)
        print(f"KWin rule '{key}' installed")
    reload_rc = subprocess.run(
        ["qdbus6", "org.kde.KWin", "/KWin", "reconfigure"], check=False,
        capture_output=True)
    print("KWin reconfigured" if reload_rc.returncode == 0
          else "run 'qdbus6 org.kde.KWin /KWin reconfigure' to apply")
    return 0


def main(source: str = "auto", click_through: bool = False,
         install_rule: bool = False) -> int:
    if install_rule:
        return install_kwin_rule()
    app = QApplication(sys.argv)
    app.setApplicationName(APP_ID)
    app.setDesktopFileName(APP_ID)
    app.setQuitOnLastWindowClosed(False)

    saved = load_settings()
    overlay = Overlay(source=source,
                      click_through=click_through or saved.get("click_through", False))
    overlay.show()
    _tray_icon(app, overlay)
    return app.exec()
