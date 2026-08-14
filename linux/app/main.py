#!/usr/bin/env python3
"""Claude Status Bar for Linux: a StatusNotifierItem tray app driven by Claude Code hooks.

A port of the macOS menu bar app (Sources/main.swift) over the same contract: hooks write
per-session JSON files to ~/.claude/statusbar/state.d/, this app polls them and renders one
aggregate icon: animating while any session works, an amber dot when one awaits permission,
resting on the Claude logo otherwise. It is launched by the hooks and quits itself when no
session is left, so there is nothing to manage.

Runs on any desktop with a StatusNotifierItem host (stock Ubuntu GNOME via the preinstalled
AppIndicator extension, KDE, XFCE); DBus-based, so X11 and Wayland both work.
"""

import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core

APP_ID = "claude-status-bar"
# Where the .deb releases live; the once-a-day update check reads this repo's latest tag.
RELEASE_REPO = "mill-master/claude-status-bar"
RELEASE_PAGE = f"https://github.com/{RELEASE_REPO}/releases/latest"
RELEASE_API = f"https://api.github.com/repos/{RELEASE_REPO}/releases/latest"

HOME = Path.home()
SB_DIR = HOME / ".claude" / "statusbar"
STATE_DIR = SB_DIR / "state.d"
QUIT_MARKER = SB_DIR / "quit-intent"
PID_FILE = SB_DIR / "app.pid"

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config")) / APP_ID
SETTINGS_PATH = CONFIG_DIR / "settings.json"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", HOME / ".cache")) / APP_ID

ORANGE = (217, 119, 87)    # #d97757, Anthropic's "Orange" accent
AMBER = (242, 186, 46)     # "awaiting permission" dot
COLORS = {"orange": ORANGE, "white": (255, 255, 255), "black": (0, 0, 0)}

LAUNCH_GRACE = 5     # settle time after launch before we may quit
IDLE_QUIT_DELAY = 3  # "not needed" must persist this long before quitting

SPARK_FPS = 9.0
CRAB_FPS = 12.5
CODE_SUB = 6         # sub-frames per glyph; 18 on macOS, fewer here since each frame is a DBus icon swap
CODE_CYCLE = 3.8     # seconds for the full 5-glyph loop
CODE_DIP = 0.14      # glyph shrinks to this at each swap

DEFAULT_SETTINGS = {
    "showTimer": False,
    "thinkingWords": True,
    "animStyle": "web",        # web | code | crab
    "iconColor": "orange",     # orange | white | black
    "soundThreshold": 0,       # 0 = off; else min turn length (seconds) that chimes on completion
    "hideIdleAfter": 900,      # hide a resting session's row after this long; 0 = never
    "installedVersion": "",
    "latestVersion": "",
    "lastUpdateCheck": 0,
}

SOUND_CHOICES = [(0, "Off"), (0.1, "Every turn"), (60, "1 min+"), (300, "5 min+"), (900, "15 min+")]
STYLE_CHOICES = [("web", "Claude Spark"), ("code", "Claude Code"), ("crab", "Crab Walking")]
COLOR_CHOICES = [("orange", "Orange"), ("white", "White"), ("black", "Black")]


def app_dir():
    return Path(__file__).resolve().parent


def find_assets():
    """The bundled asset dir: <share>/claude-status-bar/assets when installed,
    linux/build/assets (gen-assets.py output) in a checkout."""
    for base in (app_dir().parent / "build", app_dir().parent):
        p = base / "assets"
        if p.is_dir():
            return p
    return None


def app_version():
    """The version's home is build.sh's Info.plist block; packaging bakes it into _version.py,
    and a repo checkout reads build.sh directly."""
    try:
        from _version import VERSION  # written by linux/package.sh
        return VERSION
    except ImportError:
        pass
    for candidate in (app_dir().parent.parent / "build.sh", app_dir().parent.parent.parent / "build.sh"):
        try:
            m = re.search(r"CFBundleShortVersionString</key><string>([0-9.]+)", candidate.read_text())
            if m:
                return m.group(1)
        except OSError:
            continue
    return "0"


def load_settings():
    s = dict(DEFAULT_SETTINGS)
    try:
        s.update(json.loads(SETTINGS_PATH.read_text()))
    except (OSError, ValueError):
        pass
    return s


def save_settings(s):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS_PATH.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(s, indent=2) + "\n")
    tmp.rename(SETTINGS_PATH)


def locate_node():
    found = shutil.which("node")
    if found:
        return found
    candidates = ["/usr/bin/node", "/usr/local/bin/node",
                  str(HOME / ".volta" / "bin" / "node"), str(HOME / ".asdf" / "shims" / "node")]
    nvm = HOME / ".nvm" / "versions" / "node"
    if nvm.is_dir():
        for v in sorted(nvm.iterdir(), reverse=True):
            candidates.append(str(v / "bin" / "node"))
    for c in candidates:
        if os.access(c, os.X_OK):
            return c
    return None


def acquire_single_instance():
    """An exclusive flock on the pid file; a second instance exits quietly. The lock dies with
    the process, so a crash frees it and the hooks' self-heal relaunch works."""
    SB_DIR.mkdir(parents=True, exist_ok=True)
    # O_CLOEXEC: spawned children (node install.js, xdg-open) must never inherit the lock,
    # or a long-lived child would hold "running" past the app's own death.
    fd = os.open(PID_FILE, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    os.ftruncate(fd, 0)
    os.write(fd, str(os.getpid()).encode())
    return fd  # kept open for the process's life


# --- icon rendering (PIL -> PNG files the indicator refers to by name) ---

class IconSet:
    """Renders and caches the icon PNGs for one (style, color) into CACHE_DIR/<version>/.

    StatusNotifierItem icons are referred to by NAME out of an icon theme path, so every
    variant becomes a file, rendered once and reused across runs. Names are unique per
    style+color (the shell caches by name, so a name must never change content).
    """

    def __init__(self, version):
        self.assets = find_assets()
        if self.assets is None:
            raise SystemExit("claude-status-bar: asset dir not found — reinstall the package, "
                             "or in a checkout run: linux/gen-assets.py --repo . --out linux/build/assets")
        self.dir = CACHE_DIR / "icons" / version
        self.dir.mkdir(parents=True, exist_ok=True)
        self._done = set()

    def theme_path(self):
        return str(self.dir)

    def _mask_tint(self, src, rgb):
        from PIL import Image
        out = Image.new("RGBA", src.size, rgb + (255,))
        out.putalpha(src.getchannel("A"))
        return out

    def _load(self, name):
        from PIL import Image
        return Image.open(self.assets / name).convert("RGBA")

    def _crab_template(self, src, ink):
        """Port of adaptiveCrabFrame (Sources/CrabRender.swift): brightness -> opacity, so the
        body stays solid, legs go gray, and the darkest pixels (eyes) drop out as holes."""
        dark_cut, body_level, gamma = 0.30, 0.54, 1.3
        px = src.load()
        w, h = src.size
        for y in range(h):
            for x in range(w):
                r, g, b, a = px[x, y]
                if a == 0:
                    continue
                af = a / 255
                lum = (0.299 * r + 0.587 * g + 0.114 * b) / (255 * af)
                if lum < dark_cut:
                    px[x, y] = (0, 0, 0, 0)
                else:
                    t = min(1.0, (lum - dark_cut) / (body_level - dark_cut))
                    px[x, y] = ink + (int(max(0, min(255, a * (t ** gamma)))),)
        return src

    def _write(self, name, image):
        image.save(self.dir / f"{name}.png")

    def ensure(self, style, color):
        """Render every frame this (style, color) needs; returns the icon-name list in
        animation order. Idempotent per run; files persist across runs."""
        key = (style, color)
        names = self.frame_names(style, color)
        if key in self._done or all((self.dir / f"{n}.png").exists() for n in names):
            self._done.add(key)
            return names
        from PIL import Image
        rgb = COLORS[color]
        if style == "web":
            for i in range(8):
                self._write(f"csb-web-{color}-{i}", self._mask_tint(self._load(f"spark-{i}.png"), rgb))
        elif style == "crab":
            for i in range(20):
                src = self._load(f"crab-{i}.png")
                out = src if color == "orange" else self._crab_template(src, rgb)
                self._write(f"csb-crab-{color}-{i}", out)
        elif style == "code":
            for g in range(5):
                mask = self._load(f"glyph-{g}.png")
                for sub in range(CODE_SUB):
                    local = (sub + 0.5) / CODE_SUB
                    if local < 0.30:
                        u = local / 0.30
                        env = u * u * (3 - 2 * u)
                    elif local > 0.70:
                        u = (1 - local) / 0.30
                        env = u * u * (3 - 2 * u)
                    else:
                        env = 1.0
                    scale = CODE_DIP + (1 - CODE_DIP) * env
                    side = mask.size[0]
                    d = max(1, round(side * scale))
                    frame = Image.new("RGBA", mask.size, (0, 0, 0, 0))
                    frame.paste(mask.resize((d, d), Image.LANCZOS), ((side - d) // 2, (side - d) // 2))
                    self._write(f"csb-code-{color}-{g * CODE_SUB + sub}", self._mask_tint(frame, rgb))
        self._done.add(key)
        return names

    def frame_names(self, style, color):
        counts = {"web": 8, "crab": 20, "code": 5 * CODE_SUB}
        return [f"csb-{style}-{color}-{i}" for i in range(counts[style])]

    def resting(self, style, color):
        name = f"csb-crab-{color}-0" if style == "crab" else f"csb-logo-{color}"
        if not (self.dir / f"{name}.png").exists():
            if style == "crab":
                self.ensure("crab", color)
            else:
                self._write(name, self._mask_tint(self._load("logo.png"), COLORS[color]))
        return name

    def dot(self):
        name = "csb-dot"
        if not (self.dir / f"{name}.png").exists():
            from PIL import Image, ImageDraw
            big = Image.new("RGBA", (240, 240), (0, 0, 0, 0))
            ImageDraw.Draw(big).ellipse((60, 60, 180, 180), fill=AMBER + (255,))
            self._write(name, big.resize((60, 60), Image.LANCZOS))
        return name

    def fps(self, style):
        return {"web": SPARK_FPS, "crab": CRAB_FPS, "code": (5 * CODE_SUB) / CODE_CYCLE}[style]


# --- the app ---

class StatusApp:
    def __init__(self):
        self.version = app_version()
        self.settings = load_settings()
        self.icons = IconSet(self.version)
        self.words = self._load_words()

        self.sessions = {}        # id -> core.Session
        self.file_mtimes = {}     # "<id>.json" -> mtime (re-parse only on change)
        self.prev_state = {}      # id -> previous raw state
        self.session_word = {}    # id -> current thinking word
        self.turn_start = {}      # id -> active turn start (completion-sound gate)
        self.turn_line_cache = {} # transcript path -> (mtime, line)

        self.launched_at = time.time()
        self.not_needed_since = None
        self.anim_source = None
        self.frame_idx = 0
        self.frame_names = []
        self.current_icon = None
        self.current_label = None
        self.lead_started_at = 0.0
        self.menu_signature = None
        self.session_items = {}   # id -> Gtk.MenuItem
        self._syncing_menu = False
        self.player = None        # Gst playbin, if GStreamer is available

    # -- assets / settings --

    def _load_words(self):
        try:
            return json.loads((self.icons.assets / "words.json").read_text())
        except (OSError, ValueError):
            return ["Thinking"]

    def setting(self, key):
        return self.settings.get(key, DEFAULT_SETTINGS.get(key))

    def set_setting(self, key, value):
        self.settings[key] = value
        save_settings(self.settings)

    # -- state polling (mirrors tick/reloadSessions/evaluate in main.swift) --

    def state_file_names(self):
        try:
            return [f for f in os.listdir(STATE_DIR) if f.endswith(".json")]
        except OSError:
            return []

    def reload_sessions(self):
        files = self.state_file_names()
        present = set(files)
        for key in list(self.file_mtimes):
            if key not in present:
                del self.file_mtimes[key]
                self.sessions.pop(key[:-len(".json")], None)
        for f in files:
            full = STATE_DIR / f
            try:
                m = os.stat(full).st_mtime
            except OSError:
                continue
            if self.file_mtimes.get(f) == m:
                continue
            self.file_mtimes[f] = m
            sid = f[:-len(".json")]
            s = core.load_session_file(full, sid)
            if s is None:
                continue
            s.branch = core.branch_for_cwd(s.cwd)  # only on file change (a hook event)
            self.sessions[sid] = s

    def cached_turn_line(self, path):
        try:
            m = os.stat(path).st_mtime
        except OSError:
            m = None
        hit = self.turn_line_cache.get(path)
        if hit and hit[0] == m:
            return hit[1]
        line = core.last_turn_line(path)
        self.turn_line_cache[path] = (m, line)
        return line

    def evaluate(self):
        now = time.time()
        chime = False
        stale_age = float(self.setting("hideIdleAfter") or 0)
        for sid in list(self.sessions):
            s = self.sessions[sid]
            s.eff = core.effective_state(s, now, self.cached_turn_line)
            if core.should_reap(s, now, stale_age):
                try:
                    os.unlink(STATE_DIR / f"{sid}.json")
                except OSError:
                    pass
                for d in (self.sessions, self.prev_state, self.session_word, self.turn_start):
                    d.pop(sid, None)
                self.file_mtimes.pop(f"{sid}.json", None)
                continue
            self._update_thinking_word(s)
            if self._completion_edge(s, now):
                chime = True
            self.prev_state[sid] = s.state
        for sid in list(self.prev_state):
            if sid not in self.sessions:
                for d in (self.prev_state, self.session_word, self.turn_start):
                    d.pop(sid, None)
        if chime:
            self._play_sound()

        core.assign_display_names(list(self.sessions.values()))
        lead = core.pick_lead(self.sessions.values())
        if lead is None:
            self._render(icon=self.icons.resting(self.style, self.color), label="", started_at=0)
        elif lead.eff == "permission":
            self._render(icon=self.icons.dot(), label="Awaiting permission", started_at=0)
        elif lead.eff in core.WORKING_STATES:
            self._render(animate=True,
                         label=core.status_text(lead, lead.eff, self.setting("thinkingWords"),
                                                self.session_word.get(lead.id)),
                         started_at=lead.started_at)
        else:
            self._render(icon=self.icons.resting(self.style, self.color), label="", started_at=0)
        self._sync_menu()

    def _update_thinking_word(self, s):
        if s.state == "thinking" and self.prev_state.get(s.id, "") != "thinking":
            self.session_word[s.id] = core.pick_thinking_word(self.words, self.session_word.get(s.id))

    def _completion_edge(self, s, now):
        if s.state in core.WORKING_STATES and s.started_at > 0:
            self.turn_start[s.id] = s.started_at
        threshold = float(self.setting("soundThreshold") or 0)
        prev = self.prev_state.get(s.id, "")
        edge = False
        if threshold > 0 and s.state == "done" and prev != "done":
            st = self.turn_start.get(s.id, 0)
            if st > 0 and now - st >= threshold:
                edge = True
        if s.state == "done":
            self.turn_start[s.id] = 0
        return edge

    # -- self-quit lifecycle --

    def check_lifecycle(self):
        now = time.time()
        if now - self.launched_at < LAUNCH_GRACE:
            return
        if self.state_file_names():
            self.not_needed_since = None
            return
        if self.not_needed_since is None:
            self.not_needed_since = now
        elif now - self.not_needed_since >= IDLE_QUIT_DELAY:
            self.Gtk.main_quit()

    # -- rendering --

    @property
    def style(self):
        s = self.setting("animStyle")
        return s if s in ("web", "code", "crab") else "web"

    @property
    def color(self):
        c = self.setting("iconColor")
        return c if c in COLORS else "orange"

    def _render(self, icon=None, animate=False, label="", started_at=0.0):
        self.lead_started_at = started_at
        if animate:
            if self.anim_source is None:
                self.frame_names = self.icons.ensure(self.style, self.color)
                self.frame_idx = 0
                interval = int(1000 / self.icons.fps(self.style))
                self.anim_source = self.GLib.timeout_add(interval, self._anim_step)
                self._set_icon(self.frame_names[0])
        else:
            if self.anim_source is not None:
                self.GLib.source_remove(self.anim_source)
                self.anim_source = None
            self._set_icon(icon)
        self._apply_label(label)

    def _anim_step(self):
        self.frame_idx = (self.frame_idx + 1) % len(self.frame_names)
        self._set_icon(self.frame_names[self.frame_idx])
        return True

    def _set_icon(self, name):
        if name and name != self.current_icon:
            self.current_icon = name
            self.indicator.set_icon_full(name, "Claude Status Bar")

    def _apply_label(self, base):
        text = base
        if self.setting("showTimer") and self.lead_started_at > 0:
            text += "  " + core.elapsed(time.time() - self.lead_started_at)
        if text:
            text = " " + text
        if text != self.current_label:
            self.current_label = text
            self.indicator.set_label(text, " Metamorphosing…  88m 88s")

    def restyle(self):
        """Re-render the current state after a style/color change."""
        if self.anim_source is not None:
            self.GLib.source_remove(self.anim_source)
            self.anim_source = None
        self.current_icon = None
        self.evaluate()

    # -- menu --

    def _visible_sessions(self, now):
        ordered = sorted(self.sessions.values(), key=lambda s: s.ts, reverse=True)
        # Gate ONLY desktop-app sessions on real activity (merely-opened conversations); every
        # other surface is launched deliberately, so it shows the moment it starts.
        ordered = [s for s in ordered
                   if s.entrypoint != "claude-desktop" or s.started
                   or s.eff in ("permission", "thinking", "tool")]
        stale_age = float(self.setting("hideIdleAfter") or 0)
        visible = [s for s in ordered
                   if s.eff in ("permission", "thinking", "tool")
                   or not (stale_age > 0 and now - s.ts > stale_age)]
        if not visible and ordered:
            visible = [ordered[0]]  # floor: never empty while a session is alive
        return visible

    def _row_text(self, s, now):
        glyph = {"permission": "⚠", "thinking": "✳", "tool": "✳"}.get(s.eff, "❯")
        line = f"{glyph}  {core.truncated(core.session_name(s), 30, 28)}"
        if s.branch:
            line += " · " + core.truncated(s.branch, 22, 20)
        if s.eff in core.WORKING_STATES and s.started_at > 0:
            line += "  " + core.elapsed(now - s.started_at)
        return line

    def _sync_menu(self):
        """Rebuild the menu when its structure changes; refresh row texts in place otherwise
        (GTK menus, unlike NSMenu, tolerate live label updates)."""
        now = time.time()
        visible = self._visible_sessions(now)
        signature = (
            tuple(s.id for s in visible),
            self.style, self.color, bool(self.setting("showTimer")),
            bool(self.setting("thinkingWords")), float(self.setting("soundThreshold") or 0),
            self.setting("latestVersion"),
        )
        if signature != self.menu_signature:
            self.menu_signature = signature
            self._build_menu(visible, now)
        else:
            for s in visible:
                item = self.session_items.get(s.id)
                if item is not None:
                    text = self._row_text(s, now)
                    if item.get_label() != text:
                        item.set_label(text)

    def _header(self, menu, title):
        it = self.Gtk.MenuItem(label=title)
        it.set_sensitive(False)
        menu.append(it)

    def _check_item(self, menu, title, key):
        it = self.Gtk.CheckMenuItem(label=title)
        it.set_active(bool(self.setting(key)))
        def on_toggle(item):
            if self._syncing_menu:
                return
            self.set_setting(key, bool(item.get_active()))
            self.restyle()
        it.connect("toggled", on_toggle)
        menu.append(it)

    def _radio_submenu(self, menu, title, choices, key, coerce):
        parent = self.Gtk.MenuItem(label=title)
        sub = self.Gtk.Menu()
        group = None
        current = self.setting(key)
        for value, name in choices:
            it = self.Gtk.RadioMenuItem(label=name, group=group)
            group = group or it
            it.set_active(coerce(current) == coerce(value))
            def on_activate(item, value=value):
                if self._syncing_menu or not item.get_active():
                    return
                self.set_setting(key, value)
                self.restyle()
            it.connect("activate", on_activate)
            sub.append(it)
        parent.set_submenu(sub)
        menu.append(parent)

    def _build_menu(self, visible, now):
        self._syncing_menu = True
        Gtk = self.Gtk
        menu = Gtk.Menu()
        self.session_items = {}

        if visible:
            self._header(menu, "Sessions")
            for s in visible:
                it = Gtk.MenuItem(label=self._row_text(s, now))
                # Row clicks are inert for now: raising the right terminal window is not
                # portable across Wayland compositors.
                self.session_items[s.id] = it
                menu.append(it)
            menu.append(Gtk.SeparatorMenuItem())

        self._header(menu, "Options")
        self._check_item(menu, "Show timer", "showTimer")
        self._check_item(menu, "Thinking words", "thinkingWords")
        self._radio_submenu(menu, "Animation", STYLE_CHOICES, "animStyle", str)
        self._radio_submenu(menu, "Color", COLOR_CHOICES, "iconColor", str)
        if self.player is not None:
            self._radio_submenu(menu, "Completion Sound", SOUND_CHOICES, "soundThreshold", float)

        menu.append(Gtk.SeparatorMenuItem())
        version_item = Gtk.MenuItem(label=f"Version {self.version}")
        version_item.set_sensitive(False)
        menu.append(version_item)
        latest = self.setting("latestVersion")
        if latest and core.version_newer(latest, self.version):
            up = Gtk.MenuItem(label=f"Update to {latest}")
            up.connect("activate", lambda *_: subprocess.Popen(
                ["xdg-open", RELEASE_PAGE], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            menu.append(up)

        quit_item = Gtk.MenuItem(label="Quit")
        quit_item.connect("activate", self._quit_clicked)
        menu.append(quit_item)

        menu.show_all()
        self.indicator.set_menu(menu)
        self._syncing_menu = False

    def _quit_clicked(self, *_):
        # The marker keeps update.js's self-relaunch from undoing an explicit Quit; cleared on
        # the next SessionStart (lifecycle.js) or the next manual launch.
        try:
            QUIT_MARKER.touch()
        except OSError:
            pass
        self.Gtk.main_quit()

    # -- completion sound --

    def _init_sound(self):
        try:
            import gi
            gi.require_version("Gst", "1.0")
            from gi.repository import Gst
            Gst.init(None)
            mp3 = self.icons.assets / "completion.mp3"
            if not mp3.exists():
                return
            self.player = Gst.ElementFactory.make("playbin", "csb-chime")
            if self.player is None:
                return
            self.player.set_property("uri", "file://" + str(mp3))
            self.player.set_property("volume", 0.7)
            self.Gst = Gst
        except Exception:
            self.player = None  # GStreamer absent: the sound menu is simply not offered

    def _play_sound(self):
        if self.player is None:
            return
        try:
            self.player.set_state(self.Gst.State.NULL)
            self.player.set_state(self.Gst.State.PLAYING)
        except Exception:
            pass

    # -- hooks install & update check --

    def ensure_hooks_installed(self):
        """Re-runs on first install AND on every version change, so upgrades pick up hook
        changes (same contract as the macOS app's first-launch install)."""
        if self.setting("installedVersion") == self.version:
            return
        hooks = None
        for base in (app_dir().parent, app_dir().parent.parent):
            if (base / "hooks" / "install.js").is_file():
                hooks = base / "hooks" / "install.js"
                break
        if hooks is None:
            return
        def run():
            node = locate_node()
            if node is None:
                print("claude-status-bar: could not find node; hooks not installed "
                      "(will retry next launch)", file=sys.stderr)
                return
            rc = subprocess.run([node, str(hooks)], stdout=subprocess.DEVNULL).returncode
            if rc == 0:
                self.GLib.idle_add(self.set_setting, "installedVersion", self.version)
        threading.Thread(target=run, daemon=True).start()

    def check_for_update(self):
        """Once/day: cache the latest release tag. Nothing is sent anywhere (see PRIVACY.md)."""
        if time.time() - float(self.setting("lastUpdateCheck") or 0) < 86400:
            return
        def run():
            try:
                req = urllib.request.Request(RELEASE_API, headers={"User-Agent": APP_ID})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    tag = json.load(resp).get("tag_name", "")
            except Exception:
                return
            ver = tag[1:] if tag.startswith("v") else tag
            if ver:
                def store():
                    self.settings["latestVersion"] = ver
                    self.settings["lastUpdateCheck"] = int(time.time())
                    save_settings(self.settings)
                self.GLib.idle_add(store)
        threading.Thread(target=run, daemon=True).start()

    # -- main --

    def tick(self):
        self.check_lifecycle()
        self.reload_sessions()
        self.evaluate()
        return True

    def run(self):
        import gi
        gi.require_version("Gtk", "3.0")
        gi.require_version("AyatanaAppIndicator3", "0.1")
        from gi.repository import Gtk, GLib, AyatanaAppIndicator3 as AppIndicator
        self.Gtk, self.GLib = Gtk, GLib

        try:
            QUIT_MARKER.unlink()
        except OSError:
            pass
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self._init_sound()

        self.indicator = AppIndicator.Indicator.new(
            APP_ID, self.icons.resting(self.style, self.color),
            AppIndicator.IndicatorCategory.APPLICATION_STATUS)
        self.indicator.set_icon_theme_path(self.icons.theme_path())
        self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        self.indicator.set_title("Claude Status Bar")
        self._build_menu([], time.time())

        self.ensure_hooks_installed()
        self.check_for_update()
        self.tick()
        GLib.timeout_add(400, self.tick)
        for sig in (signal.SIGINT, signal.SIGTERM):
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, Gtk.main_quit)
        Gtk.main()


def dump_state():
    """--dump: print the aggregate once (no GUI), for troubleshooting."""
    now = time.time()
    sessions = []
    for f in sorted(os.listdir(STATE_DIR)) if STATE_DIR.is_dir() else []:
        if not f.endswith(".json"):
            continue
        s = core.load_session_file(STATE_DIR / f, f[:-len(".json")])
        if s:
            s.eff = core.effective_state(s, now)
            s.branch = core.branch_for_cwd(s.cwd)
            sessions.append(s)
    core.assign_display_names(sessions)
    lead = core.pick_lead(sessions)
    print(json.dumps({
        "version": app_version(),
        "sessions": [{"id": s.id, "project": core.session_name(s), "branch": s.branch,
                      "state": s.state, "effective": s.eff, "pid": s.pid,
                      "pidAlive": core.pid_alive(s.pid), "ts": s.ts} for s in sessions],
        "lead": lead.id if lead else None,
    }, indent=2))


def main():
    if "--version" in sys.argv:
        print(app_version())
        return
    if "--dump" in sys.argv:
        dump_state()
        return
    lock = acquire_single_instance()
    if lock is None:
        return  # another instance is already showing the icon
    StatusApp().run()


if __name__ == "__main__":
    main()
