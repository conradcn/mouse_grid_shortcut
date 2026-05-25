"""Standalone mouse grid overlay.

Triggers with Ctrl+Alt+M. While the overlay is showing:
  1-9       narrow to that cell (numpad layout: 1 = bottom-left)
  Backspace undo last narrow
  Enter/Space   left-click at the current cursor position and close
  Esc       close without clicking

Voice integration: Talon (or any other tool) can drive this by sending the
same key presses while the overlay is showing.

Implementation note
-------------------
All hotkeys (toggle + in-overlay keys) are registered via Win32
``RegisterHotKey``. We deliberately do NOT use a global low-level keyboard
hook (e.g. the ``keyboard`` library with ``suppress=True``): such a hook
routes every keystroke through our Python process and, if our process
stalls, Windows' shell input can wedge and Explorer can be killed by the
shell watchdog, causing a restart loop. ``RegisterHotKey`` posts
``WM_HOTKEY`` to our thread's message queue and has no such side effect.
"""

from __future__ import annotations

import ctypes
import itertools
import os
import sys
from ctypes import wintypes
from dataclasses import dataclass

from PyQt6 import QtCore, QtGui, QtWidgets

ONE_BOTTOM_LEFT = True
MAX_NARROW = 5
NARROW_EXPANSION = 0
CLICK_DELAY_MS = 60  # time after overlay close before synthetic click


# ---------- Win32 bindings ----------

user32 = ctypes.WinDLL("user32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# Per-monitor DPI awareness so Win32 returns physical pixels.
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", POINT),
    ]


# Signatures we care about (helps 64-bit pointer truncation).
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowRect.restype = wintypes.BOOL
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
user32.GetWindowThreadProcessId.argtypes = [
    wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.RegisterHotKey.argtypes = [
    wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
user32.RegisterHotKey.restype = wintypes.BOOL
user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
user32.UnregisterHotKey.restype = wintypes.BOOL
user32.SetWindowPos.argtypes = [
    wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.SetWindowPos.restype = wintypes.BOOL
user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
user32.GetCursorPos.restype = wintypes.BOOL
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.SetCursorPos.restype = wintypes.BOOL
user32.mouse_event.argtypes = [
    wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
    wintypes.DWORD, ctypes.c_void_p]
user32.mouse_event.restype = None
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short

dwmapi.DwmGetWindowAttribute.argtypes = [
    wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long  # HRESULT

kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD)]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL


# Win32 constants

DWMWA_EXTENDED_FRAME_BOUNDS = 9

HWND_TOPMOST = wintypes.HWND(-1)
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040

MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004

WM_HOTKEY = 0x0312
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_NOREPEAT = 0x4000

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

# Virtual key codes
VK_BACK = 0x08
VK_RETURN = 0x0D
VK_ESCAPE = 0x1B
VK_SPACE = 0x20
VK_M = 0x4D
VK_0 = 0x30
VK_NUMPAD0 = 0x60
VK_LBUTTON = 0x01
VK_RBUTTON = 0x02
VK_MBUTTON = 0x04


# ---------- Win32 helpers ----------


def get_cursor_pos() -> tuple[int, int]:
    p = POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


def set_cursor_pos(x: int, y: int) -> None:
    user32.SetCursorPos(int(x), int(y))


def left_click() -> None:
    user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, None)
    user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, None)


def window_class(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    n = user32.GetClassNameW(wintypes.HWND(hwnd), buf, 256)
    return buf.value if n > 0 else ""


def window_process_basename(hwnd: int) -> str:
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
    if not pid.value:
        return ""
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not h:
        return ""
    try:
        size = wintypes.DWORD(260)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value).lower()
        return ""
    finally:
        kernel32.CloseHandle(h)


# Classes that are always Explorer's shell surfaces.
SHELL_CLASSES = {
    "Progman", "WorkerW",
    "Shell_TrayWnd", "Shell_SecondaryTrayWnd",
    "NotifyIconOverflowWindow", "TrayNotifyWnd",
    "Shell_LightDismissOverlay",
}

# Shell UI host processes (Start menu, search, lock screen, etc.)
SHELL_PROCESSES = {
    "startmenuexperiencehost.exe",
    "searchhost.exe",
    "searchapp.exe",
    "shellexperiencehost.exe",
    "lockapp.exe",
    "textinputhost.exe",
}


def is_shell_window(hwnd: int) -> bool:
    if not hwnd:
        return True
    cls = window_class(hwnd)
    if cls in SHELL_CLASSES:
        return True
    proc = window_process_basename(hwnd)
    if proc in SHELL_PROCESSES:
        return True
    return False


def foreground_window_rect() -> tuple[int, tuple[int, int, int, int]] | None:
    """Return ``(hwnd, (x, y, w, h))`` of the foreground window in physical px,
    using DWM visual bounds so the Win11 invisible resize border is excluded.
    Returns None for shell windows or anything unusable."""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    if is_shell_window(hwnd):
        return None
    rect = wintypes.RECT()
    hr = dwmapi.DwmGetWindowAttribute(
        wintypes.HWND(hwnd),
        wintypes.DWORD(DWMWA_EXTENDED_FRAME_BOUNDS),
        ctypes.byref(rect),
        ctypes.sizeof(rect),
    )
    if hr != 0:
        if not user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
            return None
    w = rect.right - rect.left
    h = rect.bottom - rect.top
    if w <= 0 or h <= 0:
        return None
    return int(hwnd), (rect.left, rect.top, w, h)


def move_window_phys(hwnd: int, x: int, y: int, w: int, h: int) -> None:
    user32.SetWindowPos(
        wintypes.HWND(hwnd),
        HWND_TOPMOST,
        int(x), int(y), int(w), int(h),
        SWP_NOACTIVATE | SWP_SHOWWINDOW,
    )


# ---------- Hotkey manager ----------


class HotkeyManager(QtCore.QAbstractNativeEventFilter):
    """Tracks RegisterHotKey ids and routes WM_HOTKEY to callbacks.

    Hotkeys are registered on the current (main) thread via
    ``RegisterHotKey(NULL, ...)``. WM_HOTKEY arrives in the thread message
    queue and is dispatched here via Qt's native event filter."""

    def __init__(self) -> None:
        super().__init__()
        self._next_id = 1
        self._callbacks: dict[int, callable] = {}

    def register(self, mods: int, vk: int, cb) -> int | None:
        hid = self._next_id
        ok = user32.RegisterHotKey(
            wintypes.HWND(0), hid, mods | MOD_NOREPEAT, vk)
        if not ok:
            # Most commonly: another app already owns this hotkey.
            return None
        self._next_id += 1
        self._callbacks[hid] = cb
        return hid

    def unregister(self, hid: int | None) -> None:
        if hid is None:
            return
        user32.UnregisterHotKey(wintypes.HWND(0), hid)
        self._callbacks.pop(hid, None)

    def nativeEventFilter(self, eventType, message):  # noqa: N802
        # eventType is a QByteArray; on Windows it's b"windows_generic_MSG".
        if bytes(eventType) != b"windows_generic_MSG":
            return False, 0
        msg = MSG.from_address(int(message))
        if msg.message == WM_HOTKEY:
            cb = self._callbacks.get(int(msg.wParam))
            if cb is not None:
                try:
                    cb()
                except Exception:
                    # Swallow callback errors so we don't poison the event loop.
                    import traceback
                    traceback.print_exc()
        return False, 0


# ---------- Grid state ----------


@dataclass
class Rect:
    """Physical-pixel rectangle on the virtual desktop."""
    x: float
    y: float
    w: float
    h: float

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2

    def copy(self) -> "Rect":
        return Rect(self.x, self.y, self.w, self.h)


def cell_index(row: int, col: int) -> int:
    if ONE_BOTTOM_LEFT:
        return (2 - row) * 3 + col + 1
    return row * 3 + col + 1


def narrow_rect(rect: Rect, which: int) -> Rect:
    row = (which - 1) // 3
    col = (which - 1) % 3
    if ONE_BOTTOM_LEFT:
        row = 2 - row
    bdr = NARROW_EXPANSION
    return Rect(
        rect.x + col * rect.w / 3 - bdr,
        rect.y + row * rect.h / 3 - bdr,
        rect.w / 3 + bdr * 2,
        rect.h / 3 + bdr * 2,
    )


# ---------- Overlay widget ----------


MOUSE_MOVE_THRESHOLD_PX = 8
MOUSE_POLL_INTERVAL_MS = 25


def _any_mouse_button_down() -> bool:
    for vk in (VK_LBUTTON, VK_RBUTTON, VK_MBUTTON):
        if user32.GetAsyncKeyState(vk) & 0x8000:
            return True
    return False


class GridOverlay(QtWidgets.QWidget):
    """Frameless transparent overlay positioned via SetWindowPos in physical
    pixels. paintEvent maps physical-px grid math to widget logical units
    using ``widget.width()/window_phys_w`` as the scale factor.

    Emits ``close_requested`` when the user physically moves the cursor or
    presses a mouse button while the overlay is up. We deliberately ignore
    cursor moves that we ourselves caused via ``set_cursor_pos`` by comparing
    against ``_expected_cursor``, which we update in lockstep with our moves.
    """

    close_requested = QtCore.pyqtSignal()

    def __init__(self, window_phys: tuple[int, int, int, int]):
        super().__init__(
            None,
            QtCore.Qt.WindowType.FramelessWindowHint
            | QtCore.Qt.WindowType.WindowStaysOnTopHint
            | QtCore.Qt.WindowType.Tool
            | QtCore.Qt.WindowType.WindowTransparentForInput,
        )
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.window_phys = window_phys
        x, y, w, h = window_phys
        self.rect_state = Rect(x, y, w, h)
        self.history: list[Rect] = []
        self.count = 0
        self._expected_cursor: tuple[int, int] = get_cursor_pos()
        self._poll_timer = QtCore.QTimer(self)
        self._poll_timer.setInterval(MOUSE_POLL_INTERVAL_MS)
        self._poll_timer.timeout.connect(self._poll_mouse)

    def show_at(self) -> None:
        self.show()
        move_window_phys(int(self.winId()), *self.window_phys)
        # Capture the cursor + button baseline AFTER the window is up so we
        # don't mis-fire on the click that may have just toggled us via tray.
        self._expected_cursor = get_cursor_pos()
        # Drain any latched async key state so a stale "button-down-recently"
        # bit from before we showed doesn't immediately close us.
        for vk in (VK_LBUTTON, VK_RBUTTON, VK_MBUTTON):
            user32.GetAsyncKeyState(vk)
        self._poll_timer.start()

    def stop_polling(self) -> None:
        self._poll_timer.stop()

    def _poll_mouse(self) -> None:
        cx, cy = get_cursor_pos()
        ex, ey = self._expected_cursor
        if abs(cx - ex) > MOUSE_MOVE_THRESHOLD_PX or \
                abs(cy - ey) > MOUSE_MOVE_THRESHOLD_PX:
            self.close_requested.emit()
            return
        if _any_mouse_button_down():
            self.close_requested.emit()

    def narrow(self, which: int) -> None:
        if not 1 <= which <= 9:
            return
        self.history.append(self.rect_state.copy())
        new_rect = narrow_rect(self.rect_state, which)
        if self.count < MAX_NARROW:
            self.rect_state = new_rect
            self.count += 1
        nx, ny = int(new_rect.cx), int(new_rect.cy)
        set_cursor_pos(nx, ny)
        self._expected_cursor = (nx, ny)
        self.update()

    def reset(self) -> None:
        x, y, w, h = self.window_phys
        self.rect_state = Rect(x, y, w, h)
        self.history.clear()
        self.count = 0
        nx, ny = int(self.rect_state.cx), int(self.rect_state.cy)
        set_cursor_pos(nx, ny)
        self._expected_cursor = (nx, ny)
        self.update()

    def go_back(self) -> bool:
        if not self.history:
            return False
        self.rect_state = self.history.pop()
        self.count = max(0, self.count - 1)
        nx, ny = int(self.rect_state.cx), int(self.rect_state.cy)
        set_cursor_pos(nx, ny)
        self._expected_cursor = (nx, ny)
        self.update()
        return True

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:  # noqa: N802
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QtGui.QPainter.RenderHint.TextAntialiasing, True)

        wx, wy, ww, _wh = self.window_phys
        scale = self.width() / ww if ww else 1.0

        rx = (self.rect_state.x - wx) * scale
        ry = (self.rect_state.y - wy) * scale
        rw = self.rect_state.w * scale
        rh = self.rect_state.h * scale

        # Pick deepest level whose label fits in BOTH width and height.
        # Font sizes are PIXELS. ~75% of the previous tuning.
        #   - text_w ≈ 0.6 * font_px * digits (Segoe UI bold)
        #   - text_h ≈ 1.3 * font_px
        #   - bg pad: 8 px horizontal, 4 px vertical, plus 4 px cell margin
        min_font = 9
        max_font = 16
        chosen_levels = 1
        chosen_font = max_font
        for levels in (3, 2, 1):
            cell_w_l = rw / (3 ** levels)
            cell_h_l = rh / (3 ** levels)
            font_w_lim = (cell_w_l - 12) / (0.6 * max(1, levels))
            font_h_lim = (cell_h_l - 8) / 1.3
            font_px = int(min(font_w_lim, font_h_lim, max_font))
            if font_px >= min_font:
                chosen_levels = levels
                chosen_font = font_px
                break

        pen = QtGui.QPen(QtGui.QColor(255, 0, 0, 220))
        pen.setWidth(1)
        painter.setPen(pen)

        for level in range(1, chosen_levels + 1):
            divisor = 3 ** level
            step_w = rw / divisor
            step_h = rh / divisor
            for k in range(1, divisor):
                painter.drawLine(int(rx + k * step_w), int(ry),
                                 int(rx + k * step_w), int(ry + rh))
                painter.drawLine(int(rx), int(ry + k * step_h),
                                 int(rx + rw), int(ry + k * step_h))

        font = QtGui.QFont("Segoe UI")
        font.setPixelSize(chosen_font)
        font.setBold(True)
        painter.setFont(font)
        fm = QtGui.QFontMetrics(font)

        bg_color = QtGui.QColor(40, 40, 40, 130)
        text_color = QtGui.QColor(245, 245, 245, 180)  # whitesmoke

        cell_w = rw / (3 ** chosen_levels)
        cell_h = rh / (3 ** chosen_levels)
        for indices in itertools.product(itertools.product(range(3), range(3)),
                                         repeat=chosen_levels):
            label = "".join(str(cell_index(rr, cc)) for rr, cc in indices)
            col = sum(c * (3 ** (chosen_levels - 1 - i))
                      for i, (_, c) in enumerate(indices))
            row = sum(rr * (3 ** (chosen_levels - 1 - i))
                      for i, (rr, _) in enumerate(indices))
            cx = rx + (col + 0.5) * cell_w
            cy = ry + (row + 0.5) * cell_h

            text_w = fm.horizontalAdvance(label)
            text_h = fm.height()
            bg_rect = QtCore.QRectF(cx - text_w / 2 - 4, cy - text_h / 2 - 2,
                                    text_w + 8, text_h + 4)
            painter.setPen(QtCore.Qt.PenStyle.NoPen)
            painter.setBrush(bg_color)
            painter.drawRoundedRect(bg_rect, 3, 3)
            painter.setPen(text_color)
            painter.drawText(bg_rect, QtCore.Qt.AlignmentFlag.AlignCenter, label)


# ---------- Controller ----------


# Keys captured while the overlay is open. Each row: (label, vk).
OVERLAY_KEYS: list[tuple[str, int]] = (
    [(str(d), VK_0 + d) for d in range(1, 10)]
    + [(str(d), VK_NUMPAD0 + d) for d in range(1, 10)]
    + [
        ("esc", VK_ESCAPE),
        ("enter", VK_RETURN),
        ("space", VK_SPACE),
        ("backspace", VK_BACK),
    ]
)


class GridController(QtCore.QObject):
    def __init__(self, app: QtWidgets.QApplication, hotkeys: HotkeyManager):
        super().__init__()
        self.app = app
        self.hotkeys = hotkeys
        self.overlay: GridOverlay | None = None
        self._overlay_hids: list[int] = []

    # ---- toggle ----

    def toggle(self) -> None:
        if self.overlay is not None:
            self.overlay.reset()
        else:
            self._open()

    def _open(self) -> None:
        info = foreground_window_rect()
        if info is None:
            return
        _hwnd, window_phys = info
        self.overlay = GridOverlay(window_phys)
        self.overlay.close_requested.connect(self._close)
        self.overlay.show_at()
        self._register_overlay_keys()

    def _close(self) -> None:
        self._unregister_overlay_keys()
        if self.overlay is not None:
            self.overlay.stop_polling()
            self.overlay.hide()  # synchronously unmap before any synthetic click
            self.overlay.deleteLater()
            self.overlay = None

    # ---- in-overlay key registration ----

    def _register_overlay_keys(self) -> None:
        for label, vk in OVERLAY_KEYS:
            cb = self._make_key_handler(label)
            hid = self.hotkeys.register(0, vk, cb)
            if hid is not None:
                self._overlay_hids.append(hid)
            # If a hotkey is already taken by another app we just skip it;
            # the rest of the grid still works.

    def _unregister_overlay_keys(self) -> None:
        for hid in self._overlay_hids:
            self.hotkeys.unregister(hid)
        self._overlay_hids.clear()

    def _make_key_handler(self, label: str):
        def handler():
            self._handle_key(label)
        return handler

    def _handle_key(self, name: str) -> None:
        if self.overlay is None:
            return
        if name in ("esc",):
            self._close()
            return
        if name in ("enter", "space"):
            self._close()
            QtCore.QTimer.singleShot(CLICK_DELAY_MS, left_click)
            return
        if name == "backspace":
            if not self.overlay.go_back():
                self._close()
            return
        if name.isdigit():
            d = int(name)
            if 1 <= d <= 9:
                self.overlay.narrow(d)


# ---------- Main ----------


def main() -> int:
    app = QtWidgets.QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    hotkeys = HotkeyManager()
    app.installNativeEventFilter(hotkeys)

    controller = GridController(app, hotkeys)

    toggle_hid = hotkeys.register(MOD_CONTROL | MOD_ALT, VK_M, controller.toggle)
    if toggle_hid is None:
        QtWidgets.QMessageBox.critical(
            None, "Mouse Grid",
            "Could not register Ctrl+Alt+M global hotkey. "
            "Another app is probably using it.")
        return 1

    icon = app.style().standardIcon(
        QtWidgets.QStyle.StandardPixmap.SP_ComputerIcon)
    tray = QtWidgets.QSystemTrayIcon(icon)
    tray.setToolTip("Mouse Grid (Ctrl+Alt+M)")
    menu = QtWidgets.QMenu()
    act_toggle = menu.addAction("Toggle grid")
    act_toggle.triggered.connect(controller.toggle)
    menu.addSeparator()
    act_quit = menu.addAction("Quit")
    act_quit.triggered.connect(app.quit)
    tray.setContextMenu(menu)
    tray.show()

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
