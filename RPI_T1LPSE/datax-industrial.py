#!/usr/bin/env python3
"""
ADI DataX(TM) - 10BASE-T1L Industrial Reference Design (minimal)

A trimmed-down companion app that only covers board connectivity and the
CN0575 temperature sensor:

- Discover / ping every board on the 10BASE-T1L network
- Open a TCP "connect" check against each board's command port
- Read live temperature data from the CN0575 (ADT75 sensor)

No servo, color-sensor, SWIOT1L fan, or ADXL355 vibration panels are
included -- see the full reference design app for those.

Styled to the design system in design_system.py: approved palette, light/dark
themes, card-based panels, 8px-radius primary buttons and the 4-48px spacing
scale. Every spacing and radius value is run through UI.dp() so the scale keeps
its proportions on a high-density display (see the UI class).

Requirements:
    pip3 install matplotlib   # optional, enables the temperature graph
    pip3 install pillow       # optional, smooth logo scaling

Usage:
    python3 datax-industrial.py              # real hardware, light theme
    python3 datax-industrial.py --theme dark # real hardware, dark theme
    python3 datax-industrial.py --demo       # synthetic data, no hardware
    python3 datax-industrial.py --ui-scale 1 # override density autodetect
"""

import argparse
import ipaddress
import json
import math
import os
import re
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import tkinter as tk
import tkinter.font as tkfont
from tkinter import scrolledtext

import design_system as ds
from design_system import (
    PRIMARY, NEUTRAL, SUCCESS, WARNING, ERROR, INFO,
    STATUS_COLOR, XS, SM, MD, LG, XL, XXL,
    status_text_color, pick_font, Button, Card, TextField, ThemeMixin,
)

try:
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
    HAS_MATPLOTLIB = True
except ImportError:
    HAS_MATPLOTLIB = False
    print("Warning: matplotlib not found. The temperature graph will be disabled.")
    print("Install with: pip3 install matplotlib")

# ---------------------------------------------------------------------------
# Network configuration
#
# Nothing here is hard-coded into the UI. The defaults below describe the
# reference setup as shipped; every one of them can be replaced from the
# command line or a JSON file, because anyone bringing this up on their own
# bench will have put the boards on their own subnet.
#
#     --board "NAME=IP"     repeatable; any use replaces the default set
#     --cn0575 IP           the CN0575 / ADT75 sensor node
#     --port N              TCP command port, shared by every board
#     --config FILE         the same three keys as JSON
#
# Precedence is command line > config file > defaults, so a saved config can be
# kept for the bench and overridden per run.
# ---------------------------------------------------------------------------
DEFAULT_BOARDS = {
    "APARD32690 #1": "192.168.98.50",
    "APARD32690 #2": "192.168.98.60",
    "SWIOT1L":       "192.168.97.40",
}
DEFAULT_CN0575_IP = "192.168.10.2"
DEFAULT_TCP_PORT = 10000

# Resolved at startup by apply_args(). Read these, never the DEFAULT_* above.
BOARDS = dict(DEFAULT_BOARDS)
CN0575_IP = DEFAULT_CN0575_IP
TCP_PORT = DEFAULT_TCP_PORT
CONFIG_PATH = None         # the file Save writes to; set by apply_args()

TCP_TIMEOUT = 2.0
AUTO_REFRESH_MS = 5000
GRAPH_MAX_POINTS = 60
LOG_MAX_LINES = 2000       # ring the log, or a live session grows without end

# Header logo target height, in density-independent px (see ControlPanel._load_logo).
LOGO_HEIGHT = 30


class ConfigError(Exception):
    """A bad --board / --cn0575 / --port / --config value, reported to stderr."""


def _valid_ip(value, what):
    """Accept an IPv4/IPv6 literal or a resolvable hostname."""
    value = value.strip()
    if not value:
        raise ConfigError(f"{what}: address is empty")
    try:
        ipaddress.ip_address(value)
        return value
    except ValueError:
        pass
    # A hostname is legitimate on a bench with mDNS or /etc/hosts entries, but
    # a typo should still be caught now rather than as a mystery timeout later.
    try:
        socket.getaddrinfo(value, None)
    except OSError:
        raise ConfigError(
            f"{what}: {value!r} is neither an IP address nor a resolvable host")
    return value


def _valid_port(value, what="--port"):
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{what}: {value!r} is not a number")
    if not 1 <= port <= 65535:
        raise ConfigError(f"{what}: {port} is outside 1-65535")
    return port


def default_config_path():
    """Where the GUI saves when no --config was given.

    $XDG_CONFIG_HOME is read on every call rather than captured at import, so
    a test can point it somewhere empty and not pick up the real bench file of
    whoever is running the suite.
    """
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config")
    return Path(base) / "datax-industrial" / "boards.json"


def active_config_path(parsed):
    """The file Save writes to: --config when given, else the default."""
    return Path(parsed.config) if parsed.config else default_config_path()


def save_config_file(path, boards, cn0575, port):
    """Write boards/cn0575/port as JSON, in the shape load_config_file reads.

    Written to a sibling temp file and moved into place, because os.replace is
    atomic: a save interrupted halfway leaves the previous bench config intact
    rather than a truncated file the next launch refuses to load.
    """
    path = Path(path)
    payload = {"boards": dict(boards), "cn0575": cn0575, "port": int(port)}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    except OSError as exc:
        raise ConfigError(f"cannot save {path}: {exc.strerror or exc}")
    return path


def load_config_file(path):
    """Read boards/cn0575/port from JSON. Returns a dict of what it found."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except OSError as exc:
        raise ConfigError(f"--config {path}: {exc.strerror or exc}")
    except json.JSONDecodeError as exc:
        raise ConfigError(f"--config {path}: not valid JSON ({exc})")
    if not isinstance(raw, dict):
        raise ConfigError(f"--config {path}: expected a JSON object")

    out = {}
    if "boards" in raw:
        boards = raw["boards"]
        if not isinstance(boards, dict) or not boards:
            raise ConfigError(
                f"--config {path}: 'boards' must be a non-empty "
                '{"name": "ip"} object')
        out["boards"] = {str(name): _valid_ip(ip, f"config boards[{name!r}]")
                         for name, ip in boards.items()}
    if "cn0575" in raw:
        out["cn0575"] = _valid_ip(str(raw["cn0575"]), "config 'cn0575'")
    if "port" in raw:
        out["port"] = _valid_port(raw["port"], "config 'port'")
    return out


def parse_board_spec(spec):
    """Split one --board NAME=IP into (name, ip)."""
    name, sep, ip = spec.partition("=")
    if not sep:
        raise ConfigError(f"--board {spec!r}: expected NAME=IP")
    name = name.strip()
    if not name:
        raise ConfigError(f"--board {spec!r}: name is empty")
    return name, _valid_ip(ip, f"--board {name!r}")


def resolve_network(parsed):
    """Fold defaults, --config and the CLI into (boards, cn0575_ip, port)."""
    boards, cn0575, port = dict(DEFAULT_BOARDS), DEFAULT_CN0575_IP, \
        DEFAULT_TCP_PORT

    # An explicit --config must exist: naming a file that is not there is a
    # typo worth reporting. The default path is different -- on a first run
    # nothing has saved it yet, so its absence is normal and silent.
    source = None
    if parsed.config:
        source = parsed.config
    else:
        fallback = default_config_path()
        if fallback.is_file():
            source = fallback
    if source is not None:
        found = load_config_file(source)
        boards = found.get("boards", boards)
        cn0575 = found.get("cn0575", cn0575)
        port = found.get("port", port)

    if parsed.board:
        # Replace rather than merge: a user listing their own boards does not
        # want the shipped defaults still being pinged alongside them.
        boards = {}
        for spec in parsed.board:
            name, ip = parse_board_spec(spec)
            boards[name] = ip
    if parsed.cn0575:
        cn0575 = _valid_ip(parsed.cn0575, "--cn0575")
    if parsed.port is not None:
        port = _valid_port(parsed.port)
    return boards, cn0575, port


# ---------------------------------------------------------------------------
# CLI arguments
# ---------------------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(
        description="ADI DataX(TM) 10BASE-T1L industrial reference design: "
                    "board connectivity and CN0575 temperature.",
        epilog='example: datax-industrial.py --board "Node A=10.0.0.5" '
               '--cn0575 10.0.0.9 --port 10000')
    p.add_argument("--demo", action="store_true",
                   help="Synthetic data for every panel (no hardware needed)")
    p.add_argument("--theme", choices=["light", "dark"], default="light")

    net = p.add_argument_group("network")
    net.add_argument("--board", action="append", metavar="NAME=IP",
                     help="Board to monitor; repeatable. Any use replaces the "
                          "built-in list.")
    net.add_argument("--cn0575", metavar="IP",
                     help=f"CN0575 sensor address (default {DEFAULT_CN0575_IP})")
    net.add_argument("--port", metavar="N",
                     help=f"TCP command port (default {DEFAULT_TCP_PORT})")
    net.add_argument("--config", metavar="FILE",
                     help="JSON file with boards / cn0575 / port; also where "
                          "the Configure button saves. Defaults to "
                          "~/.config/datax-industrial/boards.json, which is "
                          "read when it exists")
    net.add_argument("--ping-count", type=int, default=1,
                     help="ICMP echo count")
    net.add_argument("--ping-timeout", type=float, default=1.0,
                     help="ICMP echo timeout (seconds)")

    ui = p.add_argument_group("appearance")
    ui.add_argument("--ui-scale", type=float, default=None, metavar="F",
                    help="Override the UI density factor (default: autodetect)")
    ui.add_argument("--custom-titlebar", action="store_true",
                    help="Draw the app's own thick title bar instead of the "
                         "window manager's. Gives up WM snapping and "
                         "edge-resize; see TitleBar.")
    return p


# Set by apply_args() before any UI is built. Module-level because the panels
# and the connectivity helpers read it at call time.
args = None


def apply_args(parsed):
    """Install the parsed arguments as module state.

    Rebinds the network globals and, in demo mode, swaps the three I/O helpers
    for their synthetic stand-ins.
    """
    global args, BOARDS, CN0575_IP, TCP_PORT, CONFIG_PATH
    global ping_host, tcp_connect_test, send_cn0575_command

    args = parsed
    BOARDS, CN0575_IP, TCP_PORT = resolve_network(parsed)
    CONFIG_PATH = active_config_path(parsed)

    if parsed.demo:
        ping_host = demo_ping_host
        tcp_connect_test = demo_tcp_connect_test
        send_cn0575_command = demo_send_cn0575_command
    return parsed


# ---------------------------------------------------------------------------
# Connectivity helpers
#
# Linux only: the ping flags below are iputils' (-c count, -W timeout in
# seconds). BSD/macOS read -W as milliseconds and Windows spells them -n/-w,
# so this would need a platform branch to travel. See README.
# ---------------------------------------------------------------------------
def ping_host(ip, count=None, timeout=None):
    """ICMP ping. Returns (ok, latency_ms_or_None)."""
    count = args.ping_count if count is None else count
    timeout = args.ping_timeout if timeout is None else timeout
    try:
        proc = subprocess.run(
            ["ping", "-c", str(count), "-W", str(int(math.ceil(timeout))), ip],
            capture_output=True, text=True, timeout=timeout * count + 2,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False, None
    if proc.returncode != 0:
        return False, None
    m = re.search(r"time[=<]([\d.]+)", proc.stdout)
    return True, (float(m.group(1)) if m else None)


# port/timeout default to None, not to the globals: a default argument is
# evaluated once at definition time, so binding TCP_PORT here would freeze the
# shipped port into the signature and silently ignore --port.
def tcp_connect_test(ip, port=None, timeout=None):
    """Plain TCP connect check against the board's command port."""
    port = TCP_PORT if port is None else port
    timeout = TCP_TIMEOUT if timeout is None else timeout
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def send_cn0575_command(cmd, timeout=None):
    """One-shot TCP command to the CN0575 command server."""
    timeout = TCP_TIMEOUT if timeout is None else timeout
    try:
        with socket.create_connection((CN0575_IP, TCP_PORT), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall((cmd + "\n").encode("ascii"))
            data = s.recv(1024)
            return data.decode("ascii").strip() if data else None
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Demo mode: synthetic stand-ins for the hardware
# ---------------------------------------------------------------------------
_demo_t0 = None


def _demo_elapsed():
    global _demo_t0
    if _demo_t0 is None:
        _demo_t0 = time.monotonic()
    return time.monotonic() - _demo_t0


def demo_ping_host(ip, count=None, timeout=None):
    return True, 0.3 + 0.2 * math.sin(_demo_elapsed())


def demo_tcp_connect_test(ip, port=None, timeout=None):
    return True


def demo_send_cn0575_command(cmd, timeout=None):
    elapsed = _demo_elapsed()
    if cmd == "READ_TEMP":
        return f"TEMP:{22.0 + 3.0 * math.sin(elapsed * 0.05):.1f}"
    return None


# ---------------------------------------------------------------------------
# Display geometry
# ---------------------------------------------------------------------------
_MONITOR_RE = re.compile(r"(\d+)/\d+x(\d+)/\d+\+(-?\d+)\+(-?\d+)")


def monitor_geometry(widget):
    """(x, y, w, h) of the monitor `widget` is on, as device px.

    winfo_screenwidth/height describe the whole X screen, which on a
    multi-head desktop is every monitor laid side by side -- sizing a window
    to it makes one window span all of them. Tk 8.6 exposes no per-monitor
    API, so this asks xrandr and picks the output containing the window's
    origin, falling back to the primary output and finally to the full screen,
    which is at least honest when xrandr is absent.
    """
    full = (0, 0, widget.winfo_screenwidth(), widget.winfo_screenheight())
    try:
        out = subprocess.run(["xrandr", "--listmonitors"], capture_output=True,
                             text=True, timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        return full
    if out.returncode != 0:
        return full

    monitors, primary = [], None
    for line in out.stdout.splitlines():
        m = _MONITOR_RE.search(line)
        if not m:
            continue
        w, h, x, y = (int(g) for g in m.groups())
        rect = (x, y, w, h)
        monitors.append(rect)
        if "*" in line.split(":", 1)[-1].split()[0]:
            primary = rect
    if not monitors:
        return full

    try:
        ox, oy = widget.winfo_rootx(), widget.winfo_rooty()
    except tk.TclError:
        return primary or monitors[0]
    for x, y, w, h in monitors:
        if x <= ox < x + w and y <= oy < y + h:
            return (x, y, w, h)
    return primary or monitors[0]


# ---------------------------------------------------------------------------
# UI density
# ---------------------------------------------------------------------------
class UI:
    """Density-independent metrics for the whole UI.

    The design system fixes spacing in px against an implied ~1080p display.
    A maximized window on a 3840x2400 panel that still reports 96 DPI -- which
    is what X11 does on most high-density laptop screens -- renders those px at
    a third of their intended apparent size, so 10pt body text ends up at about
    0.5% of the window height and the whole UI reads as a toy.

    So every px value goes through dp() and every point size through Tk's
    `scaling` factor, both derived from one autodetected density. The ratios the
    design system specifies are preserved exactly; only the unit changes.

    Normalising by the reported DPI matters: on a display that *does* report its
    true density, Tk already scales point sizes, and scaling again on top would
    double-count.
    """

    scale = 1.0
    XS = XS
    SM = SM
    MD = MD
    LG = LG
    XL = XL
    XXL = XXL

    @classmethod
    def configure(cls, root, override=None):
        dpi = root.winfo_fpixels("1i") or 96.0
        if override:
            cls.scale = override
        else:
            # Height drives it: a dashboard is laid out in rows, and height is
            # the axis that runs out first.
            raw = (root.winfo_screenheight() / 1000.0) / (dpi / 96.0)
            cls.scale = min(2.6, max(1.0, raw))

        for name in ("XS", "SM", "MD", "LG", "XL", "XXL"):
            setattr(cls, name, cls.dp(getattr(ds, name)))

        # Tk converts positive font sizes from points using this factor, so the
        # type scale scales without restating a single point size.
        root.tk.call("tk", "scaling", (dpi / 72.0) * cls.scale)

        # design_system.Button sizes its own padding and corner radius from
        # these module globals; rebinding them keeps shared components in step
        # with the rest of the UI instead of shrinking against it.
        ds.XS, ds.SM, ds.MD = cls.XS, cls.SM, cls.MD
        ds.RADIUS = cls.dp(ds.RADIUS)

    @classmethod
    def dp(cls, value):
        """Density-independent px -> device px."""
        return max(1, int(round(value * cls.scale)))


# ---------------------------------------------------------------------------
# Shared widget helpers
# ---------------------------------------------------------------------------
# Tinted companion to each STATUS_COLOR, used for dot halos and badge fills.
# Per theme, because a 100-shade wash that reads as "tinted white" on a light
# card turns into a glaring block on a dark one.
STATUS_TINT = {
    "light": {"ok": SUCCESS[100], "warn": WARNING[100],
              "error": ERROR[100], "idle": NEUTRAL[200], "info": INFO[100]},
    "dark":  {"ok": "#14301F", "warn": "#3A2A08", "error": "#3A1515",
              "idle": NEUTRAL[700], "info": "#152543"},
}

# The design system's status keys cover hardware state (ok/warn/error/idle).
# The UI also needs a neutral "informational" marker -- the demo-mode dot in
# the status bar -- which is not a hardware state, so it is added here rather
# than in the shared status vocabulary.
STATUS_FILL = dict(STATUS_COLOR, info=INFO[500])
INFO_TEXT = {"light": INFO[700], "dark": INFO[500]}


def text_color(theme_name, key):
    """status_text_color(), extended with the local 'info' key."""
    if key == "info":
        return INFO_TEXT[theme_name]
    return status_text_color(theme_name, key)


class Themed:
    """Mixin: wire a widget's _apply_theme into the app's theme registry."""

    def bind_theme(self, app):
        self.app = app
        app.on_theme(self._apply_theme)

    def _apply_theme(self):
        raise NotImplementedError


def hairline(parent, app, pady=0):
    """A 1px rule in the border colour, for separating card regions."""
    rule = tk.Frame(parent, height=UI.dp(1), bd=0, highlightthickness=0)
    rule.pack(fill="x", pady=pady)
    app.on_theme(lambda: rule.configure(bg=app.theme["border"]))
    return rule


class StatusDot(tk.Canvas, Themed):
    """Filled dot with a tinted halo -- the at-a-glance state marker."""

    def __init__(self, master, app, key="idle", size=None):
        self._size = size or UI.dp(12)
        super().__init__(master, width=self._size, height=self._size,
                         highlightthickness=0, bd=0)
        self.key = key
        self.bind_theme(app)
        self._apply_theme()

    def set(self, key):
        if key != self.key:
            self.key = key
            self._apply_theme()

    def _apply_theme(self):
        t = self.app.theme
        self.delete("all")
        self.configure(bg=self.app.parent_bg(self))
        s = self._size
        halo = STATUS_TINT[self.app.theme_name][self.key]
        core = STATUS_FILL[self.key]
        self.create_oval(0, 0, s - 1, s - 1, fill=halo, outline=halo)
        inset = s * 0.26
        self.create_oval(inset, inset, s - 1 - inset, s - 1 - inset,
                         fill=core, outline=core)


class StatusBadge(tk.Frame, Themed):
    """Dot + label, coloured by state. The primary status readout."""

    def __init__(self, master, app, text="Unknown", key="idle"):
        super().__init__(master, bd=0, highlightthickness=0)
        self.key = key
        self.dot = StatusDot(self, app, key)
        self.dot.pack(side="left")
        self.label = tk.Label(self, text=text, anchor="w", font=app.font_badge)
        self.label.pack(side="left", padx=(UI.dp(6), 0))
        self.bind_theme(app)
        self._apply_theme()

    def set(self, text, key):
        self.key = key
        self.label.configure(text=text)
        self.dot.set(key)
        self._apply_theme()

    def _apply_theme(self):
        bg = self.app.parent_bg(self)
        self.configure(bg=bg)
        self.label.configure(bg=bg,
                             fg=text_color(self.app.theme_name, self.key))
        # The dot reads *this* frame's background, so it has to be repainted
        # after the line above -- and this whole method runs again in the
        # window's post-theme pass, because theme hooks fire in construction
        # order and a badge is built before the card behind it is coloured.
        self.dot._apply_theme()


class Toggle(tk.Canvas, Themed):
    """Canvas-drawn switch: a checkbox styled like the rest of the system.

    ttk.Checkbutton's indicator is drawn by the theme engine and cannot be
    given the palette's primary colour or the 8px-radius language, so it always
    looked like a stray OS control in the middle of a card.
    """

    def __init__(self, master, app, text, variable, command=None):
        self.var, self.command = variable, command
        self._hover = False
        self._tw, self._th = UI.dp(34), UI.dp(18)
        self._label_pad = UI.dp(8)
        width = self._tw + self._label_pad + app.font_ui.measure(text)
        super().__init__(master, width=width, height=max(self._th, UI.dp(20)),
                         highlightthickness=0, bd=0, takefocus=1, cursor="hand2")
        self.text = text
        for seq, fn in (("<Button-1>", self._toggle), ("<Return>", self._toggle),
                        ("<space>", self._toggle),
                        ("<Enter>", self._enter), ("<Leave>", self._leave),
                        ("<FocusIn>", self._redraw), ("<FocusOut>", self._redraw)):
            self.bind(seq, fn)
        self.bind_theme(app)
        self._apply_theme()

    def _enter(self, _):  self._hover = True;  self._apply_theme()
    def _leave(self, _):  self._hover = False; self._apply_theme()
    def _redraw(self, _): self._apply_theme()

    def _toggle(self, _=None):
        self.var.set(not self.var.get())
        self.focus_set()
        self._apply_theme()
        if self.command:
            self.command()

    def _apply_theme(self):
        t = self.app.theme
        self.delete("all")
        bg = self.app.parent_bg(self)
        self.configure(bg=bg)

        on = bool(self.var.get())
        h = self._th
        cy = self.winfo_reqheight() / 2
        y1, y2 = cy - h / 2, cy + h / 2
        track = t["primary"] if on else (t["border"] if not self._hover
                                        else t["text_dis"])
        # A pill is a rounded rect at full radius; reuse the shared point list.
        self.create_polygon(ds.rounded_points(0, y1, self._tw, y2, h / 2),
                            smooth=True, fill=track, outline=track)
        r = h / 2 - UI.dp(3)
        kx = self._tw - h / 2 if on else h / 2
        knob = t["on_primary"] if on else t["card"]
        self.create_oval(kx - r, cy - r, kx + r, cy + r, fill=knob, outline=knob)
        self.create_text(self._tw + self._label_pad, cy, text=self.text,
                         anchor="w", fill=t["text2"], font=self.app.font_ui)
        if self.focus_get() is self:
            self.create_polygon(
                ds.rounded_points(-UI.dp(2), y1 - UI.dp(2),
                                  self._tw + UI.dp(2), y2 + UI.dp(2), h / 2),
                smooth=True, fill="", outline=PRIMARY[300], width=UI.dp(2))


class HeaderButton(Button):
    """Button tuned for the navy header bar.

    The shared secondary variant paints itself from theme["card"], which is
    white -- correct on a page background, but on the navy header it reads as a
    cut-out rather than a control. Only render() differs.
    """

    def render(self):
        t = self.app.theme
        self.delete("all")
        bg = self.app.parent_bg(self)
        self.configure(bg=bg)

        edge = PRIMARY[700]
        if self._pressed:
            fill = PRIMARY[900]
        elif self._hover:
            fill = PRIMARY[700]
        else:
            fill = bg
        self.create_polygon(
            ds.rounded_points(1, 1, self._bw - 1, self._bh - 1, ds.RADIUS),
            smooth=True, fill=fill, outline=edge, width=UI.dp(1))

        caption = f"{self.icon}  {self.label}" if self.icon else self.label
        self.create_text(self._bw / 2, self._bh / 2 + (1 if self._pressed else 0),
                         text=caption, fill=NEUTRAL[50], font=self.app.font_btn)
        if self.focus_get() is self and self._enabled:
            self.create_polygon(
                ds.rounded_points(3, 3, self._bw - 3, self._bh - 3, ds.RADIUS - 2),
                smooth=True, fill="", outline=PRIMARY[300], width=UI.dp(2))


class TitleBar(tk.Frame, Themed):
    """The window's own title bar, drawn by the app.

    A window manager's title bar is a fixed height set by the compositor's
    theme; an application cannot resize it. Drawing our own is the only way to
    control that band, so the native frame is switched off with
    overrideredirect() and this stands in for it: drag to move, double-click to
    toggle maximize, and minimise / maximise / close at the right.

    What the native frame does for free and this has to re-implement is listed
    here so the trade is explicit: moving, maximize/restore, and a resize grip
    (added to the status bar). Window-manager snapping, edge-resize, and on
    some window managers the taskbar entry and alt-tab slot are *not*
    recovered, which is why the native frame is the default and this is opt-in
    behind --custom-titlebar.
    """

    HEIGHT_DP = 38

    def __init__(self, master, app, title):
        super().__init__(master, height=UI.dp(self.HEIGHT_DP), bd=0,
                         highlightthickness=0)
        self.pack_propagate(False)
        self.win = app
        self._drag = None
        self._restore_geom = None

        self._label = tk.Label(self, text=title, font=app.font_bold, anchor="w")
        self._label.pack(side="left", padx=UI.MD)

        # Close sits outermost, the conventional position, and is the only one
        # that goes red on hover -- it is the only destructive control here.
        self._buttons = []
        for glyph, command, danger in (("\u2715", self.close,    True),
                                       ("\u2750", self.maximize, False),
                                       ("\u2500", self.minimize, False)):
            btn = tk.Label(self, text=glyph, font=app.font_ui,
                           width=4, anchor="center")
            btn.pack(side="right", fill="y")
            btn.bind("<Button-1>", lambda e, c=command: c())
            btn.bind("<Enter>", lambda e, b=btn, d=danger: self._hover(b, d))
            btn.bind("<Leave>", lambda e, b=btn: self._hover(b, None))
            self._buttons.append(btn)

        for target in (self, self._label):
            target.bind("<ButtonPress-1>", self._press)
            target.bind("<B1-Motion>", self._motion)
            target.bind("<Double-Button-1>", lambda e: self.maximize())

        self.bind_theme(app)

    # -- appearance --------------------------------------------------------
    def _hover(self, button, danger):
        if danger is None:
            button.configure(bg=self._bg, fg=NEUTRAL[300])
        elif danger:
            button.configure(bg=ERROR[500], fg=NEUTRAL[50])
        else:
            button.configure(bg=PRIMARY[700], fg=NEUTRAL[50])

    def _apply_theme(self):
        # Chrome, not content: one step darker than the header band in both
        # themes, so the window edge stays legible against any wallpaper.
        self._bg = "#001E3C"
        self.configure(bg=self._bg)
        self._label.configure(bg=self._bg, fg=PRIMARY[300])
        for btn in self._buttons:
            btn.configure(bg=self._bg, fg=NEUTRAL[300])

    # -- window controls ---------------------------------------------------
    def _press(self, event):
        self._drag = (event.x_root - self.win.winfo_rootx(),
                      event.y_root - self.win.winfo_rooty())

    def _motion(self, event):
        if self._drag is None:
            return
        if self._restore_geom is not None:
            # What a native frame does: dragging a maximized window restores
            # it first, rather than sliding it off-screen at full size.
            self.maximize()
            self._drag = (self.win.winfo_width() // 2, self._drag[1])
        dx, dy = self._drag
        self.win.geometry(f"+{event.x_root - dx}+{event.y_root - dy}")

    def minimize(self):
        # iconify() is refused on an override-redirect window, so the native
        # frame is restored for the round trip and removed again on remap.
        self.win.overrideredirect(False)
        self.win.iconify()
        self.win.bind("<Map>", self._on_map)

    def _on_map(self, _event):
        self.win.unbind("<Map>")
        self.win.overrideredirect(True)

    def maximize(self):
        if self._restore_geom is None:
            self._restore_geom = self.win.winfo_geometry()
            mx, my, mw, mh = monitor_geometry(self.win)
            self.win.geometry(f"{mw}x{mh}+{mx}+{my}")
        else:
            self.win.geometry(self._restore_geom)
            self._restore_geom = None

    def close(self):
        self.win._on_close()


class ResizeGrip(tk.Canvas, Themed):
    """Bottom-right drag handle, since override-redirect loses edge-resize."""

    def __init__(self, master, app):
        size = UI.dp(14)
        super().__init__(master, width=size, height=size, bd=0,
                         highlightthickness=0, cursor="bottom_right_corner")
        self.win = app
        self._size = size
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<B1-Motion>", self._motion)
        self.bind_theme(app)

    def _press(self, event):
        self._origin = (event.x_root, event.y_root,
                        self.win.winfo_width(), self.win.winfo_height())

    def _motion(self, event):
        x0, y0, w0, h0 = self._origin
        w = max(self.win.winfo_reqwidth(), w0 + event.x_root - x0)
        h = max(self.win.winfo_reqheight(), h0 + event.y_root - y0)
        self.win.geometry(f"{w}x{h}")

    def _apply_theme(self):
        t = self.app.theme
        self.delete("all")
        self.configure(bg=t["surface"])
        step = UI.dp(4)
        for i in range(1, 4):
            self.create_line(self._size - i * step, self._size,
                             self._size, self._size - i * step,
                             fill=t["text_dis"], width=UI.dp(1))


# ---------------------------------------------------------------------------
# Card scaffolding
# ---------------------------------------------------------------------------
class PanelCard(tk.Frame, Themed):
    """A Card with a status accent stripe, a title block and a header badge.

    Layout is: accent stripe | padded body { header row, rule, content }. The
    stripe carries the panel's worst current state, so a wall of cards can be
    triaged from across the room without reading any of them.
    """

    def __init__(self, parent, app, title, subtitle="", badge=True):
        super().__init__(parent, bd=0, highlightthickness=0)
        self._surfaces = []
        self._inks = []

        self._card = Card(self, app, pad=UI.MD)
        self._card.outer().pack(fill="both", expand=True)
        body = self._card.body

        # Packed `before` the body, which was packed fill=BOTH expand=True in
        # Card.__init__ and would otherwise leave the stripe no width at all.
        self.stripe = tk.Frame(self._card, width=UI.dp(3), bd=0,
                               highlightthickness=0)
        self.stripe.pack(side="left", fill="y", before=body)
        self._accent = "idle"

        head = tk.Frame(body, bd=0, highlightthickness=0)
        head.pack(fill="x")
        self._surfaces += [self, body, head]

        titles = tk.Frame(head, bd=0, highlightthickness=0)
        titles.pack(side="left", anchor="w")
        self._surfaces.append(titles)

        self.title_lbl = tk.Label(titles, text=title, anchor="w", font=app.font_h2)
        self.title_lbl.pack(fill="x")
        self._inks.append((self.title_lbl, "text"))

        self.subtitle_lbl = None
        if subtitle:
            self.subtitle_lbl = tk.Label(titles, text=subtitle, anchor="w",
                                         font=app.font_mono)
            self.subtitle_lbl.pack(fill="x", pady=(UI.dp(2), 0))
            self._inks.append((self.subtitle_lbl, "muted"))

        self.head_right = tk.Frame(head, bd=0, highlightthickness=0)
        self.head_right.pack(side="right", anchor="ne")
        self._surfaces.append(self.head_right)
        self.badge = StatusBadge(self.head_right, app) if badge else None
        if self.badge:
            self.badge.pack(anchor="e")

        hairline(body, app, pady=(UI.SM, UI.SM))

        self.content = tk.Frame(body, bd=0, highlightthickness=0)
        self.content.pack(fill="both", expand=True)
        self._surfaces.append(self.content)

        self.bind_theme(app)

    def surface(self, *widgets):
        """Register frames to be repainted in the card colour."""
        self._surfaces.extend(widgets)
        return widgets[0]

    def ink(self, widget, token="text"):
        self._inks.append((widget, token))
        return widget

    def set_accent(self, key):
        self._accent = key
        self.stripe.configure(bg=STATUS_FILL[key])

    def _apply_theme(self):
        t = self.app.theme
        for w in self._surfaces:
            try:
                w.configure(bg=t["card"] if w is not self else t["bg"])
            except tk.TclError:
                pass
        for w, token in self._inks:
            try:
                w.configure(bg=t["card"], fg=t[token])
            except tk.TclError:
                pass
        self.set_accent(self._accent)
        self.style_plot()

    def style_plot(self):
        """Overridden by panels that embed a matplotlib figure."""


class StatTile(tk.Frame, Themed):
    """Label and value on one line, value right-aligned.

    Sentence-case label, no trailing colon, same sans as the rest of the UI.
    Right-aligning the value means a stack of these reads down as a column of
    numbers rather than as ragged pairs.
    """

    def __init__(self, master, app, label, value="--"):
        super().__init__(master, bd=0, highlightthickness=0)
        self.bind_theme(app)
        self.columnconfigure(1, weight=1)
        self.caption = tk.Label(self, text=label, anchor="w",
                                font=app.font_caption)
        self.caption.grid(row=0, column=0, sticky="w")
        self.value = tk.Label(self, text=value, anchor="e", font=app.font_stat)
        self.value.grid(row=0, column=1, sticky="e", padx=(UI.LG, 0))

    def set(self, value):
        self.value.configure(text=value)

    def _apply_theme(self):
        t = self.app.theme
        self.configure(bg=t["card"])
        self.caption.configure(bg=t["card"], fg=t["muted"])
        self.value.configure(bg=t["card"], fg=t["text"])


class ScrollColumn(tk.Frame, Themed):
    """A vertically scrollable column of cards.

    The board rail holds one card per board and the board list comes from the
    user, so its natural height is unbounded. Sharing a fixed height between
    however many cards there are stopped working at about four: each card fell
    below its content height and the status badges were sliced through the
    middle of their glyphs. Here the cards never drop below their natural
    height and the column scrolls instead.

    When they do fit, the body is stretched to the viewport so the cards
    added with add() share the spare height, and the rail ends level with
    its neighbour instead of leaving a gap under the last card. The scrollbar
    is only mapped when the content does not fit.
    """

    def __init__(self, master, app):
        super().__init__(master, bd=0, highlightthickness=0)
        self.canvas = tk.Canvas(self, bd=0, highlightthickness=0,
                                takefocus=0)
        self.scrollbar = tk.Scrollbar(self, orient="vertical",
                                      command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)

        # The cards live in a frame embedded in the canvas; the canvas scrolls
        # the frame rather than the frame managing its own viewport.
        self.body = tk.Frame(self.canvas, bd=0, highlightthickness=0)
        self._window = self.canvas.create_window((0, 0), window=self.body,
                                                 anchor="nw")
        self.body.bind("<Configure>", self._on_body_resize)
        self.canvas.bind("<Configure>", self._on_canvas_resize)
        # X11 sends wheel events as buttons 4 and 5.
        for seq, delta in (("<Button-4>", -1), ("<Button-5>", 1)):
            self.canvas.bind_all(seq, self._wheel(delta), add="+")
        self.bind_theme(app)

    def _wheel(self, delta):
        def handler(event):
            # bind_all is global, so only scroll when the pointer is actually
            # over this column and there is something to scroll.
            if not self._scrollable():
                return
            widget = event.widget
            while widget is not None:
                if widget is self:
                    self.canvas.yview_scroll(delta, "units")
                    return
                widget = getattr(widget, "master", None)
        return handler

    def _scrollable(self):
        return self.body.winfo_reqheight() > self.canvas.winfo_height() + 1

    def add(self, card, **pack):
        """Pack a card into the column, sharing any spare height."""
        card.pack(fill="both", expand=True, **pack)
        # Once the body's height is pinned, a card growing no longer resizes
        # the body, but pack does reshuffle the cards -- so refit on theirs.
        card.bind("<Configure>", self._fit_body, add="+")

    def _fit_body(self, _event=None):
        # Never shorter than the content (that is what scrolls), never
        # shorter than the viewport (that is what fills it).
        height = max(self.canvas.winfo_height(), self.body.winfo_reqheight())
        if int(self.canvas.itemcget(self._window, "height") or 0) != height:
            self.canvas.itemconfigure(self._window, height=height)

    def _on_body_resize(self, _event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        self._sync_scrollbar()

    def _on_canvas_resize(self, event):
        # Match the embedded frame to the viewport width so cards fill it.
        self.canvas.itemconfigure(self._window, width=event.width)
        self._fit_body()
        self._sync_scrollbar()

    def _sync_scrollbar(self):
        if self._scrollable():
            if not self.scrollbar.winfo_ismapped():
                self.scrollbar.pack(side="right", fill="y")
        elif self.scrollbar.winfo_ismapped():
            self.scrollbar.pack_forget()
            self.canvas.yview_moveto(0)

    def _apply_theme(self):
        t = self.app.theme
        self.configure(bg=t["bg"])
        self.canvas.configure(bg=t["bg"])
        self.body.configure(bg=t["bg"])
        self.scrollbar.configure(bg=t["border"], troughcolor=t["bg"],
                                 activebackground=t["primary"], bd=0,
                                 width=UI.dp(12), highlightthickness=0,
                                 relief="flat", elementborderwidth=0)

# ---------------------------------------------------------------------------
# Board panels
# ---------------------------------------------------------------------------
class ConnectivityPanel(PanelCard):
    """Shared ping / TCP-connect behaviour and state roll-up."""

    def __init__(self, parent, app, board_name, ip, log, title=None,
                 subtitle=None):
        super().__init__(parent, app, title or board_name,
                         subtitle if subtitle is not None
                         else f"{ip}  ·  port {TCP_PORT}")
        self.board_name = board_name
        self.ip = ip
        self.log = log
        self._ping_state = "idle"
        self._tcp_state = "idle"
        self._inflight = 0
        self._buttons = []
        self._closing = False

    @property
    def busy(self):
        """True while any check on this board is still in flight."""
        return self._inflight > 0

    def post(self, fn, *fn_args):
        """Hand a worker-thread result back to the UI thread.

        Dropped silently once cleanup() has run: a ping against an
        unreachable host can still be in its timeout when the window closes,
        and calling after() on a torn-down widget raises from inside the
        worker, where nothing is left to catch it. Two different exceptions
        mean the same thing here -- TclError if the widget is gone, and
        RuntimeError ("main thread is not in main loop") if the interpreter
        has left mainloop entirely.
        """
        if self._closing:
            return
        try:
            self.after(0, fn, *fn_args)
        except (tk.TclError, RuntimeError):
            pass

    # -- state roll-up -----------------------------------------------------
    def _roll_up(self):
        """Card-level state is the worst of the two checks."""
        if self._inflight:
            self.badge.set("Checking…", "warn")
            self.set_accent("warn")
            return
        states = (self._ping_state, self._tcp_state)
        if "error" in states:
            key, text = "error", "Offline"
        elif states == ("ok", "ok"):
            key, text = "ok", "Online"
        elif "ok" in states:
            key, text = "warn", "Partial"
        else:
            key, text = "idle", "Not checked"
        self.badge.set(text, key)
        self.set_accent(key)
        summary = getattr(self.app, "refresh_summary", None)
        if summary:
            summary()

    def _set_busy(self, delta):
        self._inflight = max(0, self._inflight + delta)
        for b in self._buttons:
            b.set_enabled(self._inflight == 0)
        self._roll_up()

    _stamp_prefix = ""

    def _stamp(self):
        if getattr(self, "checked_lbl", None) is not None:
            self.checked_lbl.configure(
                text=self._stamp_prefix + datetime.now().strftime("%H:%M:%S"))

    # -- checks ------------------------------------------------------------
    def do_ping(self):
        self._set_busy(+1)

        def worker():
            ok, latency = ping_host(self.ip)
            self.post(self._on_ping, ok, latency)
        threading.Thread(target=worker, daemon=True).start()

    def _on_ping(self, ok, latency):
        if ok:
            text = f"Reachable · {latency:.1f} ms" if latency is not None \
                   else "Reachable"
            self._ping_state = "ok"
            self.ping_badge.set(text, "ok")
            self.log(self.board_name, f"ICMP ping OK ({text})", "ok")
        else:
            self._ping_state = "error"
            self.ping_badge.set("Unreachable", "error")
            self.log(self.board_name, f"ICMP ping failed to {self.ip}", "error")
        self._stamp()
        self._set_busy(-1)

    def do_connect(self):
        self._set_busy(+1)

        def worker():
            ok = tcp_connect_test(self.ip)
            self.post(self._on_connect, ok)
        threading.Thread(target=worker, daemon=True).start()

    def _on_connect(self, ok):
        if ok:
            self._tcp_state = "ok"
            self.tcp_badge.set("Open", "ok")
            self.log(self.board_name, f"TCP connect OK on port {TCP_PORT}", "ok")
        else:
            self._tcp_state = "error"
            self.tcp_badge.set("Closed", "error")
            self.log(self.board_name,
                     f"TCP connect failed to {self.ip}:{TCP_PORT}", "error")
        self._stamp()
        self._set_busy(-1)

    def refresh(self):
        self.do_ping()
        self.do_connect()

    @property
    def online(self):
        return self._ping_state == "ok" and self._tcp_state == "ok"

    def refresh_subtitle(self):
        """Rewrite the `address - port` line under the card title.

        Both halves are baked in at construction, so an edit to either the
        address or the shared port has to come back through here.
        """
        if self.subtitle_lbl is not None:
            self.subtitle_lbl.configure(text=f"{self.ip}  \u00b7  port {TCP_PORT}")

    def cleanup(self):
        """Stop accepting results. Panels holding timers extend this."""
        self._closing = True


class BoardPanel(ConnectivityPanel):
    """Compact card for a board that only exposes connectivity."""

    _stamp_prefix = "checked "

    def _check(self, app, parent, caption, pad):
        """A captioned status badge: `ICMP  * Reachable - 0.3 ms`."""
        box = self.surface(tk.Frame(parent, bd=0, highlightthickness=0))
        box.pack(side="left", padx=(pad, 0))
        self.ink(tk.Label(box, text=caption, anchor="w", font=app.font_caption),
                 "text2").pack(fill="x")
        badge = StatusBadge(box, app)
        badge.pack(anchor="w", pady=(UI.dp(2), 0))
        return badge

    def __init__(self, parent, app, board_name, ip, log):
        super().__init__(parent, app, board_name, ip, log)
        c = self.content

        # One row, not a stack: in a half-width rail these cards have width to
        # spare and height to save, so the two checks sit side by side with the
        # controls on the same line rather than each taking a row of its own.
        row = self.surface(tk.Frame(c, bd=0, highlightthickness=0))
        row.pack(fill="both", expand=True)

        actions = self.surface(tk.Frame(row, bd=0, highlightthickness=0))
        actions.pack(side="right")
        wide = UI.dp(104)
        self._buttons = [
            Button(actions, app, "Ping", self.do_ping, variant="secondary",
                   height=UI.dp(30), min_width=wide),
            Button(actions, app, "Test connect", self.do_connect,
                   variant="secondary", height=UI.dp(30), min_width=wide),
        ]
        self._buttons[0].pack(side="left")
        self._buttons[1].pack(side="left", padx=(UI.SM, 0))

        checks = self.surface(tk.Frame(row, bd=0, highlightthickness=0))
        checks.pack(side="left")
        self.ping_badge = self._check(app, checks, "ICMP", 0)
        self.tcp_badge = self._check(app, checks, "TCP", UI.XL)

        # Under the status badge rather than in a row of its own: the rail
        # divides its height three ways, and a third row was what did not fit.
        self.checked_lbl = self.ink(
            tk.Label(self.head_right, text="never", anchor="e",
                     font=app.font_mono), "muted")
        self.checked_lbl.pack(anchor="e", pady=(UI.dp(3), 0))

        self._apply_theme()
        self._roll_up()


class SensorPanel(ConnectivityPanel):
    """CN0575 card: connectivity, a hero temperature figure and a trend chart.

    Chart decisions, per the data-viz rules:
    - One series, so no legend box -- the card title names what is plotted.
    - 2px round-capped line, hairline solid y-grid only, no top/right spines.
      Vertical gridlines on a time axis are noise; horizontal ones carry values.
    - No area fill. A temperature axis does not start at zero, so a fill would
      imply magnitude from a baseline that is not there.
    - A marker on the latest sample only, ringed in the card colour. A dot on
      all 60 points reads as texture, and a value beside each one is chaos.
    - The endpoint value is not relabelled on the line: the hero figure sits
      directly above the chart and already is that label.
    - The line wears the series colour; every piece of text wears a text
      token. With one series and the chart a few px from the figure, a legend
      or line-key beside the hero number would only restate the card title.
    """

    UNIT = "°C"

    def __init__(self, parent, app, log):
        super().__init__(parent, app, "CN0575", CN0575_IP, log,
                         title="CN0575  ·  ADT75 temperature")
        self.temp_history = deque(maxlen=GRAPH_MAX_POINTS)
        self.time_history = deque(maxlen=GRAPH_MAX_POINTS)
        self.start_time = None
        self.auto_refresh_var = tk.BooleanVar(value=False)
        self.auto_refresh_job = None

        c = self.content

        # -- connectivity, laid out across rather than down: this card needs
        # its vertical space for the chart.
        conn = self.surface(tk.Frame(c, bd=0, highlightthickness=0))
        conn.pack(fill="x")
        self.ping_badge = StatusBadge(conn, app)
        self.ping_badge.pack(side="left")
        self.tcp_badge = StatusBadge(conn, app)
        self.tcp_badge.pack(side="left", padx=(UI.XL, 0))
        self.checked_lbl = self.ink(
            tk.Label(conn, text="never", anchor="e", font=app.font_mono),
            "muted")
        self.checked_lbl.pack(side="right")
        self.ink(tk.Label(conn, text="Last checked", font=app.font_small),
                 "text2").pack(side="right", padx=(0, UI.SM))

        hairline(c, app, pady=(UI.MD, UI.MD))

        # -- actions, packed first and to the bottom. Pack order is what
        # decides this: a side="bottom" strip only reserves its height out of
        # the cavity that is left when it is packed, so the left-hand readout
        # column has to come after it -- otherwise the column claims the full
        # height, the buttons get pushed to its right, and any shortfall is
        # taken out of the controls rather than out of the plot.
        actions = self.surface(tk.Frame(c, bd=0, highlightthickness=0))
        actions.pack(side="bottom", fill="x", pady=(UI.MD, 0))
        self._buttons = [
            Button(actions, app, "Read temp", self._read_temp, variant="primary",
                   height=UI.dp(32), min_width=UI.dp(112)),
            Button(actions, app, "Ping", self.do_ping, variant="secondary",
                   height=UI.dp(32), min_width=UI.dp(88)),
            Button(actions, app, "Test connect", self.do_connect,
                   variant="secondary", height=UI.dp(32), min_width=UI.dp(112)),
        ]
        self._buttons[0].pack(side="left")
        self._buttons[1].pack(side="left", padx=(UI.SM, 0))
        self._buttons[2].pack(side="left", padx=(UI.SM, 0))

        self.clear_btn = Button(actions, app, "Clear", self._clear_graph,
                                variant="ghost", height=UI.dp(32))
        self.clear_btn.pack(side="right")
        self.live_toggle = Toggle(actions, app, "Live (5 s)",
                                  self.auto_refresh_var,
                                  self._toggle_auto_refresh)
        self.live_toggle.pack(side="right", padx=(0, UI.LG))

        # -- hero figure + running statistics
        # A left column, not a band above the plot: a full-width chart on a
        # wide screen came out around 10:1, which is what read as crammed.
        # Beside the readout it gets both a usable aspect and the card's full
        # remaining height.
        readout = self.surface(tk.Frame(c, bd=0, highlightthickness=0))
        readout.pack(side="left", fill="y", padx=(0, UI.LG))

        hero = self.surface(tk.Frame(readout, bd=0, highlightthickness=0))
        hero.pack(anchor="w")
        # Grid, not pack: sticky="s" sets the figure and its unit on one
        # baseline, which side-by-side packing cannot do across two very
        # different font sizes.
        self.temp_value = self.ink(
            tk.Label(hero, text="--", font=app.font_display, anchor="w"), "text")
        self.temp_value.grid(row=0, column=0, sticky="sw")
        self.temp_unit = self.ink(
            tk.Label(hero, text=self.UNIT, font=app.font_h2, anchor="sw"), "text2")
        self.temp_unit.grid(row=0, column=1, sticky="sw",
                            padx=(UI.XS, 0), pady=(0, UI.dp(12)))

        tiles = self.surface(tk.Frame(readout, bd=0, highlightthickness=0))
        tiles.pack(fill="x", pady=(UI.MD, 0))
        self.tiles = {}
        for key, label in (("min", "Minimum"), ("avg", "Average"),
                           ("max", "Maximum"), ("n", "Samples")):
            tile = StatTile(tiles, app, label)
            tile.pack(fill="x", pady=(0, UI.SM))
            self.tiles[key] = tile

        # -- the chart's table twin, in its own column. Below the statistics
        # it was the tallest thing in that column and so set the whole band's
        # height, which the chart then matched. Beside the plot it costs no
        # height at all and takes width off the chart, which is the dimension
        # there was too much of. Every plotted value stays readable as a
        # number, so the hover tooltip enhances the chart rather than being
        # the only way to read it.
        samples_col = self.surface(tk.Frame(c, bd=0, highlightthickness=0))
        samples_col.pack(side="right", fill="y", padx=(UI.LG, 0))
        self.ink(tk.Label(samples_col, text="RECENT SAMPLES", anchor="w",
                          font=app.font_caption), "muted").pack(fill="x")
        self.samples_text = tk.Text(
            samples_col, width=18, height=3, state="disabled",
            font=app.font_mono, bd=0, relief="flat", highlightthickness=0,
            wrap="none", padx=0, pady=UI.XS, cursor="arrow")
        self.samples_text.pack(fill="both", expand=True, pady=(UI.XS, 0))
        self.samples_text.bind("<Configure>", self._update_samples)

        samples_rule = tk.Frame(c, width=UI.dp(1), bd=0, highlightthickness=0)
        samples_rule.pack(side="right", fill="y", pady=UI.XS)
        app.on_theme(lambda: samples_rule.configure(bg=app.theme["border"]))

        # -- chart last, so it takes the width the two columns left behind
        if HAS_MATPLOTLIB:
            self._build_chart(app)

        self._apply_theme()
        self._roll_up()

    # -- chart -------------------------------------------------------------
    def _build_chart(self, app):
        # The canvas host, and why it exists: FigureCanvasTkAgg rewrites its Tk
        # widget's requested size whenever the figure is resized, so a canvas
        # allowed to grow reports an ever-larger request back to the card,
        # which then asks for more height than the window has and gets
        # squeezed -- collapsing the plot and clipping the controls. A
        # propagation-proof host breaks that loop: the card only ever sees this
        # floor, and the canvas fills whatever the card can actually spare.
        divider = tk.Frame(self.content, width=UI.dp(1), bd=0,
                           highlightthickness=0)
        divider.pack(side="left", fill="y", pady=UI.XS)
        app.on_theme(lambda: divider.configure(bg=app.theme["border"]))

        chart_host = self.surface(tk.Frame(self.content, height=UI.dp(210),
                                           bd=0, highlightthickness=0))
        chart_host.pack(side="left", fill="both", expand=True,
                        padx=(UI.LG, 0))
        chart_host.pack_propagate(False)

        # dpi stays at the design baseline: the Tk backend reads Tk's `scaling`
        # factor and applies the density itself, so pre-scaling here would
        # count it twice and render a figure 2.4x larger than its own canvas --
        # which shows up as a chart whose axis has simply vanished off the
        # bottom edge.
        self.fig = Figure(figsize=(3.2, 1.7), dpi=80, layout="constrained")
        self.ax = self.fig.add_subplot(111)
        self.canvas = FigureCanvasTkAgg(self.fig, master=chart_host)
        widget = self.canvas.get_tk_widget()
        widget.pack(fill="both", expand=True)

        # Only now is dpi final, so this is where a device px in points is
        # worth computing -- every hairline below is expressed in it.
        self._px = 72.0 / self.fig.dpi

        self.line, = self.ax.plot([], [], linewidth=2 * self._px,
                                  solid_capstyle="round",
                                  solid_joinstyle="round", color=PRIMARY[500])
        # Latest sample, ringed in the surface colour so it stays legible
        # wherever it lands on the line.
        self.endpoint, = self.ax.plot([], [], marker="o", linestyle="none",
                                      markersize=10 * self._px,
                                      markeredgewidth=2 * self._px,
                                      color=PRIMARY[500])
        self.placeholder = self.ax.text(
            0.5, 0.5, "No samples yet \u2014 press Read temp",
            transform=self.ax.transAxes, ha="center", va="center", fontsize=9)

        self.ax.set_xlabel("Elapsed (s)", fontsize=8)
        self.ax.set_ylabel(self.UNIT, fontsize=8)
        self.ax.set_xlim(0, 10)
        self.ax.set_ylim(20, 30)

        # Hover layer. Values are never gated behind it -- the hero figure, the
        # statistics column and the axis all carry them -- so this only
        # sharpens reading an individual sample.
        self._cross = self.ax.axvline(0, linewidth=1 * self._px, visible=False)
        self._hover = self.ax.annotate(
            "", xy=(0, 0), xytext=(UI.dp(8), UI.dp(8)),
            textcoords="offset points", fontsize=8, visible=False,
            bbox=dict(boxstyle="round,pad=0.4", linewidth=1 * self._px),
            annotation_clip=False)
        self.canvas.mpl_connect("motion_notify_event", self._on_hover)
        self.canvas.mpl_connect("axes_leave_event", self._on_hover_leave)

        widget.bind("<Configure>", self._sync_figure, add="+")
        self._sync_job = self.after(250, self._sync_figure)

    def _sync_figure(self, event=None):
        """Keep the figure's pixel size equal to the canvas drawing it.

        The Tk backend scales figure.dpi by Tk's `scaling` factor on <Map>, but
        computes the figure's size in inches on <Configure> from whatever dpi
        it had at that moment. For an embedded canvas <Configure> arrives
        first, so the inches are fixed against the unscaled dpi and never
        revisited -- leaving a figure 2.4x the size of its canvas, drawn from
        the top-left corner, with the x-axis off the bottom edge. Recomputing
        against the live dpi corrects it and is harmless once it already fits.
        """
        if self._closing:
            return
        self._sync_job = None
        widget = self.canvas.get_tk_widget()
        w = event.width if event is not None else widget.winfo_width()
        h = event.height if event is not None else widget.winfo_height()
        if w < 2 or h < 2:
            return
        target = (w / self.fig.dpi, h / self.fig.dpi)
        if max(abs(a - b) for a, b in zip(self.fig.get_size_inches(),
                                          target)) > 0.01:
            self.fig.set_size_inches(*target, forward=False)
            self.canvas.draw_idle()

    def _on_hover(self, event):
        if event.inaxes is not self.ax or not self.temp_history:
            return self._on_hover_leave(event)
        # Nearest sample on x, so the pointer never has to land on the mark.
        times = list(self.time_history)
        i = min(range(len(times)), key=lambda k: abs(times[k] - event.xdata))
        x, y = times[i], list(self.temp_history)[i]
        self._cross.set_xdata([x, x])
        self._cross.set_visible(True)
        self._hover.xy = (x, y)
        self._hover.set_text(f"{y:.1f} {self.UNIT}   ·   t+{x:.0f} s")
        self._hover.set_visible(True)
        self.canvas.draw_idle()

    def _on_hover_leave(self, _event):
        if not getattr(self, "_cross", None):
            return
        if self._cross.get_visible() or self._hover.get_visible():
            self._cross.set_visible(False)
            self._hover.set_visible(False)
            self.canvas.draw_idle()

    def style_plot(self):
        t = self.app.theme
        series = PRIMARY[500] if self.app.theme_name == "light" else PRIMARY[300]
        self.samples_text.configure(bg=t["card"], fg=t["text2"],
                                    selectbackground=t["primary"],
                                    selectforeground=t["on_primary"],
                                    inactiveselectbackground=t["primary"])
        if not HAS_MATPLOTLIB or not hasattr(self, "line"):
            return

        self.line.set_color(series)
        self.endpoint.set_color(series)
        self.endpoint.set_markeredgecolor(t["card"])
        self.placeholder.set_color(t["muted"])
        self._cross.set_color(t["text_dis"])
        self._hover.set_color(t["text"])
        self._hover.get_bbox_patch().set_facecolor(t["surface"])
        self._hover.get_bbox_patch().set_edgecolor(t["border"])

        self.fig.patch.set_facecolor(t["card"])
        self.ax.set_facecolor(t["card"])
        self.ax.tick_params(colors=t["text2"], labelsize=7,
                            width=1 * self._px, length=3 * self._px)
        # Recessive chrome: the two spines the eye needs, hairline and solid.
        for side in ("top", "right"):
            self.ax.spines[side].set_visible(False)
        for side in ("left", "bottom"):
            self.ax.spines[side].set_color(t["border"])
            self.ax.spines[side].set_linewidth(1 * self._px)
        self.ax.grid(False)
        self.ax.grid(True, axis="y", color=t["border"], linewidth=1 * self._px,
                     linestyle="-")
        self.ax.set_axisbelow(True)
        self.ax.xaxis.label.set_color(t["text2"])
        self.ax.yaxis.label.set_color(t["text2"])
        self.canvas.draw_idle()

    # -- temperature -------------------------------------------------------
    def _read_temp(self):
        def worker():
            self.post(self.log, self.board_name, ">> READ_TEMP", "tx")
            resp = send_cn0575_command("READ_TEMP")
            if resp is None:
                self.post(self._on_temp_error)
                return
            self.post(self._on_temp_response, resp)
        threading.Thread(target=worker, daemon=True).start()

    def _on_temp_response(self, resp):
        self.log(self.board_name, f"<< {resp}", "rx")
        if not resp.startswith("TEMP:"):
            return
        try:
            temp = float(resp[5:])
        except ValueError:
            return
        self.temp_value.configure(text=f"{temp:.1f}")
        if self.start_time is None:
            self.start_time = datetime.now()
        elapsed = (datetime.now() - self.start_time).total_seconds()
        self.time_history.append(elapsed)
        self.temp_history.append(temp)

        temps = list(self.temp_history)
        self.tiles["min"].set(f"{min(temps):.1f}")
        self.tiles["avg"].set(f"{sum(temps) / len(temps):.1f}")
        self.tiles["max"].set(f"{max(temps):.1f}")
        self.tiles["n"].set(str(len(temps)))
        self._update_samples()
        self._update_graph()

    def _update_samples(self, _event=None):
        """Newest first, so the latest reading never scrolls out of view.

        Only whole rows are written. The column is sized by its neighbours, so
        filling it with the full history left a final row sliced through the
        middle of its glyphs -- worse than simply showing fewer.
        """
        line_h = max(1, self.app.font_mono.metrics("linespace"))
        height = self.samples_text.winfo_height()
        usable = height - 2 * UI.XS          # the Text's own pady
        visible = max(1, usable // line_h) if height > 1 else 8
        rows = list(zip(self.time_history, self.temp_history))[::-1][:visible]
        self.samples_text.configure(state="normal")
        self.samples_text.delete("1.0", "end")
        self.samples_text.insert(
            "end", "\n".join(f"t+{elapsed:6.1f} s{value:8.1f}"
                             for elapsed, value in rows))
        self.samples_text.configure(state="disabled")

    def _on_temp_error(self):
        self.log(self.board_name, "<< ERROR: no response", "error")

    def _update_graph(self):
        if not HAS_MATPLOTLIB or not self.temp_history:
            return
        times, temps = list(self.time_history), list(self.temp_history)
        self.line.set_data(times, temps)
        self.endpoint.set_data(times[-1:], temps[-1:])
        self.placeholder.set_visible(False)

        self.ax.set_xlim(max(0, times[0] - 2), max(times[-1] + 2, 10))
        if len(temps) > 1:
            lo, hi = min(temps), max(temps)
            margin = max(0.5, (hi - lo) * 0.25)
            lo, hi = lo - margin, hi + margin
        else:
            lo, hi = temps[0] - 2, temps[0] + 2
        # Snap to a round step so the axis holds still between samples instead
        # of twitching on every reading.
        step = 0.5
        self.ax.set_ylim(math.floor(lo / step) * step,
                         math.ceil(hi / step) * step)
        self.canvas.draw_idle()

    def _clear_graph(self):
        self.temp_history.clear()
        self.time_history.clear()
        self.start_time = None
        self.temp_value.configure(text="--")
        for tile in self.tiles.values():
            tile.set("--")
        self._update_samples()
        if HAS_MATPLOTLIB:
            self.line.set_data([], [])
            self.endpoint.set_data([], [])
            self.placeholder.set_visible(True)
            self._on_hover_leave(None)
            self.ax.set_xlim(0, 10)
            self.ax.set_ylim(20, 30)
            self.canvas.draw_idle()
        self.log(self.board_name, "Trend history cleared", "info")

    def _toggle_auto_refresh(self):
        if self.auto_refresh_var.get():
            self.log(self.board_name,
                     f"Live polling every {AUTO_REFRESH_MS // 1000} s", "info")
            self._auto_refresh_tick()
        else:
            self.log(self.board_name, "Live polling stopped", "info")
            if self.auto_refresh_job:
                self.after_cancel(self.auto_refresh_job)
                self.auto_refresh_job = None

    def _auto_refresh_tick(self):
        if self.auto_refresh_var.get():
            self._read_temp()
            self.auto_refresh_job = self.after(AUTO_REFRESH_MS,
                                               self._auto_refresh_tick)

    def cleanup(self):
        super().cleanup()
        self.auto_refresh_var.set(False)

        # FigureCanvasTkAgg.draw_idle() queues its redraw with after_idle and
        # tracks the job on the canvas. Left pending, it fires into the
        # destroyed widget and Tk prints `invalid command name "...idle_draw"`
        # to stderr as the app exits -- harmless, but not something a released
        # tool should print at a user.
        canvas = getattr(self, "canvas", None)
        idle_id = getattr(canvas, "_idle_draw_id", None)
        if idle_id:
            try:
                canvas.get_tk_widget().after_cancel(idle_id)
                canvas._idle_draw_id = None
            except (tk.TclError, ValueError, AttributeError):
                pass

        for attr in ("auto_refresh_job", "_sync_job"):
            job = getattr(self, attr, None)
            if job is not None:
                try:
                    self.after_cancel(job)
                except (tk.TclError, ValueError):
                    pass            # already fired, or interpreter going away
                setattr(self, attr, None)


# ---------------------------------------------------------------------------
# Communication log
# ---------------------------------------------------------------------------
class LogPanel(PanelCard):
    """Timestamped activity log. Levels are tagged, not spelled out in-line."""

    # token in the active theme, or a literal resolved per theme
    LEVELS = {
        "ok":    ("status", "ok"),
        "error": ("status", "error"),
        "warn":  ("status", "warn"),
        "info":  ("theme",  "text2"),
        "tx":    ("theme",  "primary"),
        "rx":    ("theme",  "text"),
    }

    def __init__(self, parent, app, on_clear):
        super().__init__(parent, app, "Communication log",
                         subtitle="", badge=False)
        self.clear_btn = Button(self.head_right, app, "Clear",
                                on_clear, variant="ghost", height=UI.dp(28))
        self.clear_btn.pack(anchor="e")

        # width/height are deliberately tiny: Text sizes itself in characters,
        # and its 80x24 default would demand more width than the column has,
        # starving the sibling columns in the uniform grid. The geometry
        # manager gives it its real size.
        self.text = scrolledtext.ScrolledText(
            self.content, width=1, height=3, state="disabled",
            font=app.font_mono, bd=0, relief="flat",
            highlightthickness=UI.dp(1), wrap="word",
            padx=UI.SM, pady=UI.XS,
        )
        self.text.pack(fill="both", expand=True)
        self.text.tag_configure("ts", font=app.font_mono)
        self.text.tag_configure("src", font=app.font_mono_bold)
        for level in self.LEVELS:
            self.text.tag_configure(level, font=app.font_mono)
        self._apply_theme()

    def append(self, source, message, level="info"):
        kind, value = self.LEVELS.get(level, self.LEVELS["info"])
        self.text.configure(state="normal")
        self.text.insert("end", datetime.now().strftime("%H:%M:%S  "), "ts")
        self.text.insert("end", f"{source:<14}", "src")
        self.text.insert("end", message + "\n", level)
        self._trim()
        self.text.see("end")
        self.text.configure(state="disabled")

    def _trim(self):
        """Drop the oldest lines past LOG_MAX_LINES.

        Live polling appends two lines every AUTO_REFRESH_MS, so an
        unattended session on a bench would otherwise accumulate for as long
        as it is left running. Caller holds the widget in its normal state.
        """
        # index("end-1c") is the last character, so its line number is the
        # count of lines actually present.
        lines = int(self.text.index("end-1c").split(".")[0])
        if lines > LOG_MAX_LINES:
            self.text.delete("1.0", f"{lines - LOG_MAX_LINES + 1}.0")

    def clear(self):
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")

    def _apply_theme(self):
        super()._apply_theme()
        t = self.app.theme
        if not hasattr(self, "text"):
            return
        self.text.configure(bg=t["surface"], fg=t["text"],
                            insertbackground=t["text"],
                            highlightbackground=t["border"],
                            highlightcolor=t["border"],
                            selectbackground=t["primary"],
                            selectforeground=t["on_primary"])
        self.text.tag_configure("ts", foreground=t["muted"])
        self.text.tag_configure("src", foreground=t["primary"])
        for level, (kind, value) in self.LEVELS.items():
            fg = (text_color(self.app.theme_name, value)
                  if kind == "status" else t[value])
            self.text.tag_configure(level, foreground=fg)

        # ScrolledText embeds a classic tk.Scrollbar in its own frame.
        for w in (self.text.master, *self.text.master.winfo_children()):
            if isinstance(w, tk.Scrollbar):
                w.configure(bg=t["border"], troughcolor=t["surface"],
                            activebackground=t["primary"], bd=0,
                            width=UI.dp(12), highlightthickness=0,
                            relief="flat", elementborderwidth=0)
            elif isinstance(w, tk.Frame):
                w.configure(bg=t["card"])


# ---------------------------------------------------------------------------
# Configuration dialog
# ---------------------------------------------------------------------------
class ConfigDialog(tk.Toplevel):
    """Modal editor for the board list, the sensor address and the port.

    Writes nothing itself: on Save it validates, hands the result to the
    callback and closes. The caller owns both applying it to the live panels
    and persisting it, so a validation failure here cannot leave half of a
    change applied.
    """

    def __init__(self, app, on_save):
        super().__init__(app)
        self.app = app
        self._on_save = on_save
        self._rows = []
        self._surfaces = []
        self._inks = []

        self.title("Configure boards")
        self.transient(app)
        self.resizable(False, False)

        outer = tk.Frame(self, bd=0, highlightthickness=0)
        outer.pack(fill="both", expand=True, padx=UI.XL, pady=UI.LG)
        self._surfaces += [self, outer]

        self._inks.append((tk.Label(outer, text="Boards", anchor="w",
                                    font=app.font_h2), "text"))
        self._inks[-1][0].pack(fill="x")
        hint = tk.Label(outer, anchor="w", font=app.font_small,
                        text="Each board gets a card in the Network links rail. "
                             "Names are labels only; addresses may be hostnames.")
        hint.pack(fill="x", pady=(UI.dp(2), UI.SM))
        self._inks.append((hint, "muted"))

        # Column captions, so the two fields are not guessed at by width.
        heads = tk.Frame(outer, bd=0, highlightthickness=0)
        heads.pack(fill="x")
        self._surfaces.append(heads)
        for text, width in (("Board name", self._NAME_W),
                            ("Address", self._ADDR_W)):
            lbl = tk.Label(heads, text=text, anchor="w", font=app.font_caption,
                           width=width)
            lbl.pack(side="left", padx=(0, UI.SM))
            self._inks.append((lbl, "text2"))

        self._rows_box = tk.Frame(outer, bd=0, highlightthickness=0)
        self._rows_box.pack(fill="x", pady=(UI.dp(2), 0))
        self._surfaces.append(self._rows_box)

        add_row = tk.Frame(outer, bd=0, highlightthickness=0)
        add_row.pack(fill="x", pady=(UI.SM, 0))
        self._surfaces.append(add_row)
        self._btn_add = Button(add_row, app, "Add board", self._add_blank_row,
                               variant="secondary", icon="+",
                               height=UI.dp(30), min_width=UI.dp(120))
        self._btn_add.pack(side="left")

        hairline(outer, app, pady=(UI.MD, UI.MD))

        # Sensor address and port: one row, both narrow.
        tail = tk.Frame(outer, bd=0, highlightthickness=0)
        tail.pack(fill="x")
        self._surfaces.append(tail)
        self.f_cn0575 = self._labelled(tail, "CN0575 address", CN0575_IP,
                                       self._ADDR_W, app.font_mono)
        self.f_port = self._labelled(tail, "TCP port", str(TCP_PORT),
                                     self._PORT_W, app.font_mono)

        # Reserves its line whether or not there is a message, so showing one
        # does not resize the dialog under the pointer.
        self._err = tk.Label(outer, text="", anchor="w", font=app.font_small,
                             wraplength=UI.dp(420), justify="left")
        self._err.pack(fill="x", pady=(UI.MD, 0))

        actions = tk.Frame(outer, bd=0, highlightthickness=0)
        actions.pack(fill="x", pady=(UI.SM, 0))
        self._surfaces.append(actions)
        Button(actions, app, "Save", self._save, height=UI.dp(32),
               min_width=UI.dp(104)).pack(side="right")
        Button(actions, app, "Cancel", self.close, variant="secondary",
               height=UI.dp(32), min_width=UI.dp(104)).pack(
                   side="right", padx=(0, UI.SM))

        for name, ip in BOARDS.items():
            self._add_row(name, ip)

        app.on_theme(self._apply_theme)
        # The app-wide sweep, not just this dialog's: Button registers render()
        # with the theme registry but never calls it, so a Button built after
        # the main window finished its own sweep stays an unpainted canvas.
        app.apply_theme()

        self.bind("<Escape>", lambda e: self.close())
        self.bind("<Return>", lambda e: self._save())
        self.protocol("WM_DELETE_WINDOW", self.close)

        self.update_idletasks()
        self._centre_on_parent()
        # grab_set after the window is mapped: an unmapped grab is refused by
        # some window managers and the dialog then is not modal at all.
        self.grab_set()
        if self._rows:
            self._rows[0][0].focus()

    _NAME_W = 22
    _ADDR_W = 20
    _PORT_W = 8

    # -- construction helpers ---------------------------------------------
    def _labelled(self, parent, caption, value, width, font):
        """A captioned field in a column of its own."""
        box = tk.Frame(parent, bd=0, highlightthickness=0)
        box.pack(side="left", padx=(0, UI.XL))
        self._surfaces.append(box)
        lbl = tk.Label(box, text=caption, anchor="w", font=self.app.font_caption)
        lbl.pack(fill="x")
        self._inks.append((lbl, "text2"))
        field = TextField(box, self.app, value, font=font, width=width)
        field.pack(anchor="w", pady=(UI.dp(2), 0))
        return field

    def _add_row(self, name, ip):
        row = tk.Frame(self._rows_box, bd=0, highlightthickness=0)
        row.pack(fill="x", pady=(0, UI.XS))
        self._surfaces.append(row)

        f_name = TextField(row, self.app, name, font=self.app.font_ui,
                           width=self._NAME_W)
        f_name.pack(side="left", padx=(0, UI.SM))
        f_addr = TextField(row, self.app, ip, font=self.app.font_mono,
                           width=self._ADDR_W)
        f_addr.pack(side="left", padx=(0, UI.SM))

        entry = [f_name, f_addr, row, None]
        btn = Button(row, self.app, "Remove", lambda: self._remove_row(entry),
                     variant="secondary", height=UI.dp(28), min_width=UI.dp(84))
        btn.pack(side="left")
        entry[3] = btn

        self._rows.append(entry)
        self.app.apply_theme()
        return entry

    def _add_blank_row(self):
        entry = self._add_row("", "")
        self.update_idletasks()
        self._centre_on_parent()
        entry[0].focus()

    def _remove_row(self, entry):
        if len(self._rows) == 1:
            self._fail("At least one board is required.")
            return
        self._rows.remove(entry)
        for widget in (entry[0], entry[1], entry[3]):
            widget.destroy()
        entry[2].destroy()
        for dead in (entry[2],):
            if dead in self._surfaces:
                self._surfaces.remove(dead)
        self._err.configure(text="")
        self.update_idletasks()
        self._centre_on_parent()

    def _centre_on_parent(self):
        self.geometry("+%d+%d" % (
            self.app.winfo_rootx()
            + max(0, (self.app.winfo_width() - self.winfo_reqwidth()) // 2),
            self.app.winfo_rooty()
            + max(0, (self.app.winfo_height() - self.winfo_reqheight()) // 3)))

    # -- validation --------------------------------------------------------
    def _fail(self, message, field=None):
        self._err.configure(text=message,
                            fg=status_text_color(self.app.theme_name, "error"))
        if field is not None:
            field.mark_invalid(True)
            field.focus()
        return None

    def collect(self):
        """Validate every field. Returns (boards, cn0575, port) or None.

        Reuses the CLI's own validators so a value typed here is held to the
        same standard as one passed on the command line.
        """
        for row in self._rows:
            row[0].mark_invalid(False)
            row[1].mark_invalid(False)
        self.f_cn0575.mark_invalid(False)
        self.f_port.mark_invalid(False)
        self._err.configure(text="")

        boards = {}
        for f_name, f_addr, _row, _btn in self._rows:
            name = f_name.get().strip()
            if not name:
                return self._fail("Every board needs a name.", f_name)
            # boards is a dict, so two rows with one name would collapse into
            # a single entry and a board would vanish on save.
            if name in boards:
                return self._fail(f"Two boards are both named {name!r}. "
                                  "Names must be unique.", f_name)
            try:
                boards[name] = _valid_ip(f_addr.get(), f"board {name!r}")
            except ConfigError as exc:
                return self._fail(str(exc), f_addr)

        if not boards:
            return self._fail("At least one board is required.")

        try:
            cn0575 = _valid_ip(self.f_cn0575.get(), "CN0575 address")
        except ConfigError as exc:
            return self._fail(str(exc), self.f_cn0575)
        try:
            port = _valid_port(self.f_port.get(), "TCP port")
        except ConfigError as exc:
            return self._fail(str(exc), self.f_port)
        return boards, cn0575, port

    def _save(self):
        result = self.collect()
        if result is None:
            return
        try:
            self._on_save(*result)
        except ConfigError as exc:
            self._fail(str(exc))
            return
        self.close()

    # -- teardown ----------------------------------------------------------
    def close(self):
        try:
            self.grab_release()
        except tk.TclError:
            pass
        # Unregister before destroying: the hook holds a bound method on this
        # window, and the app's registry outlives the dialog.
        self.app.off_theme(self._apply_theme)
        self.destroy()

    def _apply_theme(self):
        t = self.app.theme
        for w in self._surfaces:
            try:
                w.configure(bg=t["bg"] if w is self else t["card"])
            except tk.TclError:
                pass
        for w, token in self._inks:
            try:
                w.configure(bg=t["card"], fg=t[token])
            except tk.TclError:
                pass
        try:
            self.configure(bg=t["card"])
            self._err.configure(
                bg=t["card"], fg=status_text_color(self.app.theme_name, "error"))
        except tk.TclError:
            pass


# ---------------------------------------------------------------------------
# Main Application Window
# ---------------------------------------------------------------------------
class ControlPanel(tk.Tk, ThemeMixin):
    """Main application window: header, board panels, log and status bar."""

    def __init__(self):
        super().__init__()
        self.title("ADI DataX™ - 10BASE-T1L Industrial Reference Design")
        # Timers started before the window is torn down have to be cancelled
        # on the way out, or they fire into a destroyed widget.
        self._after_jobs = set()

        UI.configure(self, args.ui_scale)
        # Off before the window is sized: with no frame, the geometry we ask
        # for is the geometry we get, so nothing can overhang the screen edge.
        self._custom_chrome = args.custom_titlebar
        if self._custom_chrome:
            self.overrideredirect(True)
        self._fit_to_screen()

        # Theme registry must exist before any panel or Button is constructed.
        self.init_theme(args.theme)
        fam = pick_font("Segoe UI", "Inter", "DejaVu Sans", "Helvetica")
        mono = pick_font("Cascadia Mono", "DejaVu Sans Mono", "Courier")
        # One type scale. Sizes are points; UI.configure set Tk's scaling
        # factor, so these land at the right physical size on any density.
        self.font_ui        = tkfont.Font(family=fam, size=10)
        self.font_small     = tkfont.Font(family=fam, size=9)
        self.font_caption   = tkfont.Font(family=fam, size=8,  weight="bold")
        self.font_bold      = tkfont.Font(family=fam, size=10, weight="bold")
        self.font_badge     = tkfont.Font(family=fam, size=10, weight="bold")
        self.font_h1        = tkfont.Font(family=fam, size=17, weight="bold")
        self.font_h2        = tkfont.Font(family=fam, size=11, weight="bold")
        self.font_stat      = tkfont.Font(family=fam, size=13, weight="bold")
        # Hero figure: the same sans as everything else, proportional figures.
        self.font_display   = tkfont.Font(family=fam, size=30, weight="bold")
        self.font_mono      = tkfont.Font(family=mono, size=9)
        self.font_mono_bold = tkfont.Font(family=mono, size=9, weight="bold")

        self._build_titlebar()
        self._build_header()
        self._build_statusbar()
        self._build_content()
        self._bind_shortcuts()

        self.apply_theme()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._log_message("Session",
                          "Demo mode — synthetic data, no hardware attached"
                          if args.demo else
                          "Live hardware mode — press Test all to begin", "info")

    # ------------------------------------------------------------------ shell
    def _fit_to_screen(self):
        """Fill the screen on launch, whatever the window manager allows.

        Maximize is attempted first and then *verified*: some window managers
        (and headless/no-WM setups) accept the zoomed state without honouring
        it, and some honour an explicit full-screen geometry but offset the
        frame by its decoration, pushing the status bar off the bottom of the
        screen. So the explicit geometry is only a fallback, and
        _ensure_on_screen pulls the window back inside the work area either way.
        """
        self.update_idletasks()
        mx, my, screen_w, screen_h = monitor_geometry(self)
        self.minsize(min(UI.dp(1000), screen_w), min(UI.dp(650), screen_h))

        if self._custom_chrome:
            self.geometry(f"{screen_w}x{screen_h}+{mx}+{my}")
            return

        # Maximize only once the window is mapped. Asked for earlier, the
        # zoomed flag is accepted but some window managers (WSLg's Weston)
        # size the frame for the wrong output and ignore the taskbar, which
        # hid the bottom of the log and the status bar behind it.
        self.track_after(50, self._maximize)

    def _maximize(self):
        if not self.winfo_viewable():
            self.track_after(50, self._maximize)
            return
        for attempt in (lambda: self.attributes("-zoomed", True),
                        lambda: self.state("zoomed")):
            try:
                attempt()
            except tk.TclError:
                continue
            break
        # The window manager applies the zoom asynchronously, so measuring
        # straight away would always see the pre-zoom size. Verify once it
        # has had time to land, then pull the frame on screen.
        self.track_after(400, self._verify_zoomed)
        self.track_after(900, self._ensure_on_screen)

    def _verify_zoomed(self):
        """Fall back to an explicit geometry if maximize was not honoured."""
        self.update_idletasks()
        mx, my, screen_w, screen_h = monitor_geometry(self)
        if (self.winfo_width() < screen_w * 0.9
                or self.winfo_height() < screen_h * 0.9):
            self.geometry(f"{screen_w}x{screen_h}+{mx}+{my}")
        self._ensure_on_screen()

    def track_after(self, ms, fn, *fn_args):
        """after(), with the job id remembered so _on_close can cancel it."""
        job = self.after(ms, fn, *fn_args)
        self._after_jobs.add(job)
        return job

    def _cancel_tracked(self):
        for job in self._after_jobs:
            try:
                self.after_cancel(job)
            except (tk.TclError, ValueError):
                pass                # already fired, or interpreter tearing down
        self._after_jobs.clear()

    def _ensure_on_screen(self):
        """Shrink the window back inside the screen if its frame overhangs."""
        self.update_idletasks()
        x, y = self.winfo_rootx(), self.winfo_rooty()
        w, h = self.winfo_width(), self.winfo_height()
        mx, my, mw, mh = monitor_geometry(self)
        new_w, new_h = min(w, mx + mw - x), min(h, my + mh - y)
        if new_w < w or new_h < h:
            self.geometry(f"{max(new_w, 1)}x{max(new_h, 1)}+{x}+{y}")

    def _load_logo(self, height):
        """Load the header logo, scaled to `height` px, or None if unavailable.

        Reads assets/adi_logo.png (white artwork on transparency). Prefers the
        @2x asset when one exists, since the density factor can ask for more
        pixels than the 1x file has. With Pillow the image is resized smoothly;
        without it, Tk's integer-only subsample is used.
        """
        here = os.path.dirname(os.path.abspath(__file__))
        candidates = [os.path.join(here, "assets", "adi_logo@2x.png"),
                      os.path.join(here, "assets", "adi_logo.png")]
        path = next((p for p in candidates if os.path.exists(p)), None)
        if path is None:
            return None
        try:
            from PIL import Image, ImageTk
        except ImportError:
            try:
                img = tk.PhotoImage(file=path)
            except tk.TclError:
                return None
            if img.height() > height:        # integer downscale only
                img = img.subsample(max(1, round(img.height() / height)))
            return img
        try:
            im = Image.open(path).convert("RGBA")
            bbox = im.split()[3].getbbox()
            if bbox:
                im = im.crop(bbox)            # trim transparent padding
            w = max(1, round(im.width * height / im.height))
            return ImageTk.PhotoImage(im.resize((w, height), Image.LANCZOS))
        except Exception:
            return None

    def _build_titlebar(self):
        self.titlebar = None
        if not self._custom_chrome:
            return
        self.titlebar = TitleBar(
            self, self, "ADI DataX\u2122  \u2014  10BASE-T1L Industrial "
                        "Reference Design")
        self.titlebar.pack(side="top", fill="x")

    def _build_header(self):
        head = tk.Frame(self, height=UI.dp(76), bd=0, highlightthickness=0)
        head.pack(side="top", fill="x")
        head.pack_propagate(False)
        # A hairline of the mid-primary under the navy, so the header reads as
        # a deliberate band rather than the page simply starting dark.
        self._head_accent = tk.Frame(self, height=UI.dp(3), bd=0,
                                     highlightthickness=0)
        self._head_accent.pack(side="top", fill="x")

        left = tk.Frame(head, bd=0, highlightthickness=0)
        left.pack(side="left", padx=UI.XL)

        # Analog Devices logo. Falls back to a glyph if the asset is missing.
        self._logo_img = self._load_logo(UI.dp(LOGO_HEIGHT))
        if self._logo_img is not None:
            logo = tk.Label(left, image=self._logo_img, bd=0)
        else:
            logo = tk.Label(left, text="◆", font=self.font_h1)
        logo.pack(side="left", padx=(0, UI.MD))

        # A rule between the vendor mark and the product name, so the two read
        # as separate things rather than one run-on wordmark.
        self._head_rule = tk.Frame(left, width=UI.dp(1), bd=0,
                                   highlightthickness=0)
        self._head_rule.pack(side="left", fill="y", pady=UI.XS,
                             padx=(0, UI.MD))

        titles = tk.Frame(left, bd=0, highlightthickness=0)
        titles.pack(side="left")
        name = tk.Label(titles, text="DataX™ Industrial Reference Design",
                        font=self.font_h1, anchor="w")
        name.pack(fill="x")
        tagline = tk.Label(titles, text="10BASE-T1L single-pair Ethernet",
                           font=self.font_ui, anchor="w")
        tagline.pack(fill="x", pady=(UI.dp(3), 0))

        right = tk.Frame(head, bd=0, highlightthickness=0)
        right.pack(side="right", padx=UI.XL)

        # Fleet roll-up: the one number someone walking past the rack wants.
        summary = tk.Frame(right, bd=0, highlightthickness=0)
        summary.pack(side="left", padx=(0, UI.XL))
        self._online_dot = StatusDot(summary, self, "idle", size=UI.dp(12))
        self._online_dot.pack(side="left")
        self._online_lbl = tk.Label(summary, text="Not checked",
                                    font=self.font_badge, anchor="w")
        self._online_lbl.pack(side="left", padx=(UI.dp(6), 0))

        # Sized to its caption rather than the shared min_width: three buttons
        # at 116dp each crowd the fleet roll-up at the 1000dp minimum width.
        self._btn_config = HeaderButton(right, self, "Configure",
                                        self._open_config, height=UI.dp(34))
        self._btn_config.pack(side="left", padx=(0, UI.SM))

        self._btn_test_all = HeaderButton(right, self, "Test all",
                                          self._test_all, height=UI.dp(34),
                                          min_width=UI.dp(116))
        self._btn_test_all.pack(side="left")
        self._btn_theme = HeaderButton(right, self, "Dark", self._on_toggle_theme,
                                       icon="◐", height=UI.dp(34),
                                       min_width=UI.dp(116))
        self._btn_theme.pack(side="left", padx=(UI.SM, 0))

        self._header_frames = (head, left, right, titles, summary)
        self._header_labels = (logo, name)
        self._header_muted = (tagline,)

    def _build_statusbar(self):
        bar = tk.Frame(self, height=UI.dp(30), bd=0, highlightthickness=0)
        bar.pack(side="bottom", fill="x")
        bar.pack_propagate(False)
        self._status_rule = tk.Frame(self, height=UI.dp(1), bd=0,
                                     highlightthickness=0)
        self._status_rule.pack(side="bottom", fill="x")

        left = tk.Frame(bar, bd=0, highlightthickness=0)
        left.pack(side="left", padx=UI.XL)
        self._mode_dot = StatusDot(left, self, "info" if args.demo else "ok",
                                   size=UI.dp(10))
        self._mode_dot.pack(side="left", pady=UI.dp(2))
        self._mode_txt = tk.Label(
            left, text="Demo mode — synthetic data" if args.demo
            else "Live hardware", font=self.font_small)
        self._mode_txt.pack(side="left", padx=(UI.dp(6), 0))

        self._grip = None
        if self._custom_chrome:
            self._grip = ResizeGrip(bar, self)
            self._grip.pack(side="right", padx=(0, UI.SM), pady=UI.XS,
                            anchor="se")

        self._refresh_txt = tk.Label(bar, text="Idle", font=self.font_mono)
        self._refresh_txt.pack(side="right", padx=UI.XL)
        self._scale_txt = tk.Label(bar, font=self.font_mono,
                                   text=f"{len(BOARDS) + 1} boards")
        self._scale_txt.pack(side="right", padx=(0, UI.XL))
        self._statusbar = bar
        self._status_frames = (bar, left)

    def _section(self, parent, text, row, column, columnspan=1, top=0):
        lbl = tk.Label(parent, text=text.upper(), anchor="w",
                       font=self.font_caption)
        lbl.grid(row=row, column=column, columnspan=columnspan, sticky="w",
                 pady=(top, UI.SM))
        self._section_labels.append(lbl)
        return lbl

    def _build_content(self):
        """Two columns, then a full-width log.

        A full-width chart on a 16:10 screen can only ever be a flat band --
        around 7:1 -- and buying it height starves every other panel. Putting
        the telemetry card in its own column instead is what makes both
        proportions work: the chart lands near 2:1, and the space the board
        cards were spending on empty width becomes the height the log needed.

        Row weights, not fixed heights, divide the slack, so the proportions
        survive a different screen or a resized window.
        """
        content = tk.Frame(self, bd=0, highlightthickness=0)
        content.pack(side="top", fill="both", expand=True,
                     padx=UI.XL, pady=(UI.MD, UI.MD))
        self._content = content
        self._section_labels = []
        gap = UI.MD

        # grid hands each column its requested width first and only shares
        # the surplus by weight, so these weights tilt the leftover toward the
        # chart -- they do not set the ratio outright. The rail reports the
        # width one card needs; its height is whatever the row gives it,
        # because ScrollColumn scrolls rather than compressing.
        content.columnconfigure(0, weight=2)   # telemetry
        content.columnconfigure(1, weight=1)   # board rail
        # Spare height is split between the two rows, slightly favouring the
        # log. The chart is the only thing in the telemetry row that expands,
        # so giving that row all of the slack put all of it into the plot --
        # and giving it none made the log a mostly-empty box the height of a
        # third of the window. Neither row should be the sole beneficiary.
        content.rowconfigure(1, weight=2, minsize=UI.dp(380))
        content.rowconfigure(3, weight=3)

        self._section(content, "Sensor & telemetry", 0, 0)
        self._section(content, "Network links", 0, 1)

        self.panels = []
        self.cn0575 = SensorPanel(content, self, self._log_message)
        self.cn0575.grid(row=1, column=0, sticky="nsew", padx=(0, gap // 2))

        # The board cards share the rail's height evenly, so adding a fourth
        # board does not need any of these numbers changed.
        rail = ScrollColumn(content, self)
        rail.grid(row=1, column=1, sticky="nsew", padx=(gap // 2, 0))
        self._rail = rail
        for i, (board_name, ip) in enumerate(BOARDS.items()):
            p = BoardPanel(rail.body, self, board_name, ip, self._log_message)
            rail.add(p, pady=(0 if i == 0 else gap // 2, 0))
            self.panels.append(p)
        self.panels.append(self.cn0575)

        self._section(content, "Activity", 2, 0, columnspan=2, top=UI.MD)
        self.log_panel = LogPanel(content, self, self._clear_log)
        self.log_panel.grid(row=3, column=0, columnspan=2, sticky="nsew")

    def _bind_shortcuts(self):
        self.bind_all("<Control-r>", lambda e: self._test_all())
        self.bind_all("<F5>",        lambda e: self._test_all())
        self.bind_all("<Control-t>", lambda e: self._on_toggle_theme())
        self.bind_all("<Control-l>", lambda e: self._clear_log())

    # ----------------------------------------------------------------- theming
    def _on_toggle_theme(self):
        self.toggle_theme()
        self._log_message("UI", f"Switched to {self.theme_name} theme", "info")

    def apply_theme(self):
        t = self.theme
        self.configure(bg=t["bg"])
        self._btn_theme.label = "Dark" if self.theme_name == "light" else "Light"
        self._btn_theme.icon = ("◐" if self.theme_name == "light"
                                else "◑")

        for w in self._header_frames:
            w.configure(bg=t["header_bg"])
        self._head_rule.configure(bg=PRIMARY[700])
        self._head_accent.configure(bg=PRIMARY[500])
        for w in self._header_labels:
            # The logo label holds an image, so only its bg matters; fg would
            # be a no-op there but is what tints the wordmark text.
            w.configure(bg=t["header_bg"])
            if not w.cget("image"):
                w.configure(fg=t["header_fg"])
        for w in self._header_muted:
            w.configure(bg=t["header_bg"], fg=PRIMARY[300])
        self._online_lbl.configure(bg=t["header_bg"], fg=t["header_fg"])

        self._content.configure(bg=t["bg"])
        for w in self._section_labels:
            w.configure(bg=t["bg"], fg=t["muted"])

        for w in self._status_frames:
            w.configure(bg=t["surface"])
        self._status_rule.configure(bg=t["border"])
        self._mode_txt.configure(bg=t["surface"], fg=t["text2"])
        self._refresh_txt.configure(bg=t["surface"], fg=t["muted"])
        self._scale_txt.configure(bg=t["surface"], fg=t["muted"])

        # Run every panel/Button/Card hook, then flush so everything repaints
        # as one frame rather than cascading visibly.
        super().apply_theme()

        # Buttons paint their canvas with the *parent's* bg, and hooks run in
        # construction order -- re-render once the tree is fully themed.
        self._rerender_canvases(self)
        self.update_idletasks()

    def _rerender_canvases(self, widget):
        for child in widget.winfo_children():
            if isinstance(child, Button):
                child.render()
            elif isinstance(child, (StatusDot, Toggle, StatusBadge)):
                child._apply_theme()
            else:
                self._rerender_canvases(child)

    # -------------------------------------------------------------- log/status
    def _log_message(self, source, message, level="info"):
        self.log_panel.append(source, message, level)
        self._refresh_txt.configure(
            text=f"Updated {datetime.now().strftime('%H:%M:%S')}")

    def _clear_log(self):
        self.log_panel.clear()

    def refresh_summary(self):
        """Roll the per-board states up into the header badge."""
        panels = getattr(self, "panels", None)
        if not panels or not hasattr(self, "_online_lbl"):
            return
        online = sum(1 for p in panels if p.online)
        checked = [p for p in panels
                   if p._ping_state != "idle" or p._tcp_state != "idle"]
        if not checked:
            key, text = "idle", "Not checked"
        elif online == len(panels):
            key, text = "ok", f"All {online} boards online"
        elif online:
            key, text = "warn", f"{online} of {len(panels)} boards online"
        else:
            key, text = "error", "No boards online"
        self._online_dot.set(key)
        self._online_lbl.configure(text=text)

    def _test_all(self):
        """Run every check on every board.

        Guarded against re-entry: each panel starts two worker threads, so a
        held-down button or an impatient operator could otherwise stack dozens
        of concurrent pings against an unreachable subnet.
        """
        if any(p.busy for p in self.panels):
            self._log_message("Session", "Tests already running", "warn")
            return
        self._log_message("Session", f"Testing {len(self.panels)} boards", "info")
        self._btn_test_all.set_enabled(False)
        for p in self.panels:
            p.refresh()
        self._poll_test_all()

    # -- configuration -----------------------------------------------------
    def _open_config(self):
        """Open the board editor, unless checks are in flight."""
        if any(p.busy for p in self.panels):
            self._log_message("Session",
                              "Cannot reconfigure while checks are running",
                              "warn")
            return
        if getattr(self, "_config_dialog", None) is not None:
            try:
                self._config_dialog.lift()
                return
            except tk.TclError:
                pass
        self._config_dialog = ConfigDialog(self, self._on_config_saved)
        # Clear the handle however the dialog goes away, so a second click
        # after Cancel does not try to lift a destroyed window.
        self._config_dialog.bind("<Destroy>", self._on_config_closed, add="+")

    def _on_config_closed(self, event):
        if event.widget is getattr(self, "_config_dialog", None):
            self._config_dialog = None

    def _on_config_saved(self, boards, cn0575, port):
        """Apply the edited configuration, then persist it.

        Applied before it is written: if the save fails the user is looking at
        a window that already matches what they typed, and the dialog reports
        why it is not on disk. The reverse order would show them a stale UI
        and claim success.
        """
        self.apply_config(boards, cn0575, port)
        path = save_config_file(CONFIG_PATH, boards, cn0575, port)
        self._log_message("Session", f"Configuration saved to {path}", "ok")

    def apply_config(self, boards, cn0575, port):
        """Rebind the network configuration and reshape the panels to match.

        Live rather than on restart: board_name and ip are read per check, not
        cached at construction, so reassigning them is enough for every
        subsequent ping. The parts that *are* cached -- the subtitle, and the
        badge text describing the previous address -- are refreshed here.
        """
        global BOARDS, CN0575_IP, TCP_PORT
        port_changed = port != TCP_PORT
        BOARDS, CN0575_IP, TCP_PORT = dict(boards), cn0575, port

        # The CN0575 card's badges read self.ip, but send_cn0575_command reads
        # the module global. Setting only one would leave the temperature reads
        # and the reachability badges pointing at different hosts.
        if self.cn0575.ip != cn0575:
            self.cn0575.ip = cn0575
            self._reset_checks(self.cn0575)
        self.cn0575.refresh_subtitle()

        wanted = list(boards.items())
        old_order = [p for p in self.panels if p is not self.cn0575]

        # Decide which card becomes which board before touching any of them.
        # A card whose name survived the edit keeps its identity; the cards and
        # boards left unclaimed are then paired up in order, which is what
        # makes a rename a rename rather than a remove plus an add that would
        # throw the board's results away. Matching on position alone would
        # mean deleting the first row renamed every card below it and
        # destroyed the last one instead.
        unclaimed = {p.board_name: p for p in old_order}
        claimed = {}
        for index, (name, _ip) in enumerate(wanted):
            panel = unclaimed.pop(name, None)
            if panel is not None:
                claimed[index] = panel
        spare_cards = [p for p in old_order if p.board_name in unclaimed]
        spare_slots = [i for i in range(len(wanted)) if i not in claimed]
        for index, panel in zip(spare_slots, spare_cards):
            claimed[index] = panel
        taken = {id(p) for p in claimed.values()}

        cards = []
        for index, (name, ip) in enumerate(wanted):
            panel = claimed.get(index)
            if panel is None:
                panel = BoardPanel(self._rail.body, self, name, ip,
                                   self._log_message)
                self._log_message(name, f"Board added at {ip}", "info")
            else:
                if panel.board_name != name:
                    self._log_message(panel.board_name,
                                      f"Renamed to {name}", "info")
                    panel.board_name = name
                    panel.title_lbl.configure(text=name)
                if panel.ip != ip:
                    self._log_message(name, f"Address changed to {ip}", "info")
                    panel.ip = ip
                    self._reset_checks(panel)
            panel.refresh_subtitle()
            cards.append(panel)

        for panel in old_order:
            if id(panel) in taken:
                continue
            self._log_message(panel.board_name, "Board removed", "info")
            # cleanup() first: a ping started before this edit can still be in
            # its timeout, and post() drops the result once _closing is set.
            panel.cleanup()
            panel.destroy()

        # Rebuilt rather than patched, so the rail order always matches the
        # order the boards were listed in. The sensor card stays last, where
        # _build_content put it.
        self.panels = cards + [self.cn0575]

        if port_changed:
            for panel in self.panels:
                panel.refresh_subtitle()
            self._log_message("Session", f"TCP port is now {port}", "info")

        self._repack_rail()
        self.refresh_summary()

    def _repack_rail(self):
        """Re-pack the board cards so the first one keeps its flush top edge.

        add() gives every card but the first a top gap; after a removal the
        card that inherits first place would otherwise keep its own.
        """
        gap = UI.MD
        cards = [p for p in self.panels if p is not self.cn0575]
        for panel in cards:
            panel.pack_forget()
        for i, panel in enumerate(cards):
            self._rail.add(panel, pady=(0 if i == 0 else gap // 2, 0))

    @staticmethod
    def _reset_checks(panel):
        """Drop a panel's results back to unchecked.

        Called when a panel's address changes: `Reachable - 0.3 ms` describes
        the host it used to point at, and leaving it on screen would attribute
        the old board's health to the new one.
        """
        panel._ping_state = "idle"
        panel._tcp_state = "idle"
        panel.ping_badge.set("Not checked", "idle")
        panel.tcp_badge.set("Not checked", "idle")
        panel.checked_lbl.configure(text="never")
        panel._roll_up()

    def _poll_test_all(self):
        """Re-enable Test all once every panel has gone idle."""
        if any(p.busy for p in self.panels):
            self.track_after(200, self._poll_test_all)
        else:
            self._btn_test_all.set_enabled(True)

    def _on_close(self):
        # Panels first: cleanup() makes them drop any in-flight worker result,
        # so nothing lands on a widget that is about to stop existing.
        for p in self.panels:
            p.cleanup()
        self._cancel_tracked()
        self.destroy()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main(argv=None):
    """Parse arguments, report configuration problems, run the window."""
    parsed = build_parser().parse_args(argv)
    try:
        apply_args(parsed)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    app = ControlPanel()
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
