"""
Steam Unblock — gets past the ISP block on Steam without a VPN.

Only connections to Steam domains pass through sing-box (fake-IP range routed into a TUN); game traffic
never touches it, so in-game ping is unchanged. sing-box is bound to a kill-on-close Job Object: quitting or
crashing this app removes the bypass and the TUN with it. Closing or minimizing the window only hides it to
the system tray; "Quit" in the tray menu quits. Optionally starts with Windows (logon task), either hidden in
the tray or with the window shown. UI in Vietnamese or English.
"""

from __future__ import annotations

import enum
import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
import winreg
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk

import pystray
from PIL import Image

import autostart
import i18n
import setup_check
from tunnel import SingBox, build_steam_unblock_config, host_has_ipv6, probe_steam
from winapi import (
    SHIELD_TUNNEL_MUTEX, STEAM_APP_MUTEX, STEAM_SHOW_EVENT, STEAM_TUNNEL_MUTEX, KillOnCloseJob, NamedEvent,
    NamedMutex, enable_dpi_awareness, find_processes, flush_dns_cache, is_admin, mutex_exists, relaunch_as_admin,
    signal_event,
)

APP_TITLE = "Steam Unblock"
FROZEN = getattr(sys, "frozen", False)
BASE_DIR = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
CORE_DIR = BASE_DIR / "core"
SETTINGS_PATH = BASE_DIR / "steam-settings.json"
AUTOSTART_TASK = "SteamUnblock"
RELEASES_URL = "https://github.com/Edvat07/SteamUnblock/releases/latest"
STEAM_DOWNLOAD_URL = "https://store.steampowered.com/about/"
STARTUP_TRAY, STARTUP_WINDOW = "tray", "window"

SINGBOX_NAMES = frozenset({"sing-box.exe"})
UI_TICK_MS = 250
WATCHDOG_EVERY_TICKS = 4            # 1 s
CONNECT_TIMEOUT_S = 20
LOG_MAX_LINES = 300

COLOR_OK, COLOR_WARN, COLOR_ERR, COLOR_IDLE = "#1a7f37", "#9a6700", "#cf222e", "#57606a"

tr = i18n.Strings({
    "intro": {
        "vi": "Chỉ kết nối tới Steam được xử lý. Game và mọi app khác đi mạng bình thường, không qua VPN nên "
              "không tăng ping.\nĐóng/thu nhỏ cửa sổ thì app vẫn chạy ngầm ở khay hệ thống; chuột phải biểu "
              "tượng → Thoát để tắt hẳn.",
        "en": "Only connections to Steam are handled. Games and every other app use your normal connection, "
              "no VPN, so ping does not change.\nClosing or minimizing the window keeps the app running in the "
              "system tray; right-click the icon → Quit to exit.",
    },
    "btn_start": {"vi": "Bật", "en": "Turn on"},
    "btn_steam": {"vi": "Mở Steam", "en": "Open Steam"},
    "btn_stop": {"vi": "Tắt", "en": "Turn off"},
    "options": {"vi": "Tuỳ chọn", "en": "Options"},
    "autostart": {"vi": "Khởi động cùng Windows", "en": "Start with Windows"},
    "startup_tray": {"vi": "Chạy ngầm (chỉ hiện ở khay hệ thống)", "en": "In the background (tray icon only)"},
    "startup_window": {"vi": "Hiện cửa sổ", "en": "Show the window"},
    "language": {"vi": "Ngôn ngữ:", "en": "Language:"},
    "log": {"vi": "Nhật ký", "en": "Log"},
    "tray_show": {"vi": "Mở cửa sổ", "en": "Open window"},
    "tray_quit": {"vi": "Thoát", "en": "Quit"},
    "tray_hint": {
        "vi": "Vẫn đang chạy ngầm. Bấm vào biểu tượng ở khay hệ thống để mở lại, chuột phải → Thoát để tắt hẳn.",
        "en": "Still running in the background. Click the tray icon to reopen it, right-click → Quit to exit.",
    },
    "state_off": {"vi": "Đang tắt", "en": "Off"},
    "state_starting": {"vi": "Đang bật…", "en": "Turning on…"},
    "state_on": {"vi": "Đã mở khoá Steam ✓", "en": "Steam unblocked ✓"},
    "state_stopping": {"vi": "Đang tắt…", "en": "Turning off…"},
    "state_stopped": {"vi": "Đã tắt.", "en": "Turned off."},
    "state_not_on": {"vi": "Chưa bật", "en": "Not on"},
    "state_paused": {"vi": "Tạm nghỉ: đang có tunnel khác (Roblox Shield)",
                     "en": "Paused: another tunnel is running (Roblox Shield)"},
    "state_crashed": {"vi": "Bị tắt do lỗi — bấm Bật để thử lại.", "en": "Stopped by an error — press Turn on to retry."},
    "autostart_refresh_failed": {"vi": "Không cập nhật được tác vụ khởi động: {error}",
                                 "en": "Could not update the startup task: {error}"},
    "autostart_on_failed": {"vi": "Không bật được khởi động cùng Windows: {error}",
                            "en": "Could not turn on start with Windows: {error}"},
    "autostart_off_failed": {"vi": "Không tắt được khởi động cùng Windows: {error}",
                             "en": "Could not turn off start with Windows: {error}"},
    "autostart_on": {"vi": "Đã bật khởi động cùng Windows.", "en": "Start with Windows is on."},
    "autostart_off": {"vi": "Đã tắt khởi động cùng Windows.", "en": "Start with Windows is off."},
    "startup_mode_tray": {"vi": "Khi khởi động cùng Windows: chạy ngầm ở khay hệ thống.",
                          "en": "At Windows startup: run in the background in the tray."},
    "startup_mode_window": {"vi": "Khi khởi động cùng Windows: hiện cửa sổ.",
                            "en": "At Windows startup: show the window."},
    "warn_job": {"vi": "Cảnh báo: không gắn được sing-box vào Job Object.",
                 "en": "Warning: could not bind sing-box to the Job Object."},
    "singbox_start_failed": {"vi": "Không khởi động được sing-box: {error}", "en": "Could not start sing-box: {error}"},
    "no_response": {"vi": "chưa có phản hồi", "en": "no response yet"},
    "singbox_exited": {"vi": "sing-box đã thoát.", "en": "sing-box exited."},
    "cancelled": {"vi": "Đã hủy.", "en": "Cancelled."},
    "steam_unreachable": {"vi": "Không vào được Steam: {error}", "en": "Could not reach Steam: {error}"},
    "stopped_no_resume": {"vi": "Đã tắt. Sẽ không tự bật lại khi Roblox Shield tắt.",
                          "en": "Turned off. It will not turn back on when Roblox Shield stops."},
    "paused": {
        "vi": "Roblox Shield (hoặc một sing-box khác) đang chạy và tự lo Steam, app này tạm nghỉ để tránh xung đột. "
              "Khi nó tắt, app sẽ tự bật lại.",
        "en": "Roblox Shield (or another sing-box) is running and handles Steam itself, so this app pauses to "
              "avoid a conflict. It turns back on when that stops.",
    },
    "opening_steam": {
        "vi": "Đang mở Steam… Nếu Steam đã mở sẵn mà vẫn báo lỗi mạng, hãy thoát hẳn Steam rồi mở lại.",
        "en": "Opening Steam… If Steam was already open and still shows a network error, quit Steam fully "
              "and open it again.",
    },
    "steam_not_installed": {
        "vi": "Máy chưa cài Steam — đã mở trang tải Steam. Cài xong thì bấm Mở Steam.",
        "en": "Steam is not installed — opened the Steam download page. After installing it, press Open Steam.",
    },
    "resuming": {"vi": "Roblox Shield đã tắt, bật lại mở khoá Steam.",
                 "en": "Roblox Shield stopped, turning the Steam unblock back on."},
    "singbox_died": {"vi": "sing-box dừng đột ngột.", "en": "sing-box stopped unexpectedly."},
    "crash_notify": {"vi": "Mở khoá Steam bị tắt do lỗi. Mở cửa sổ để bật lại.",
                     "en": "The Steam unblock stopped because of an error. Open the window to turn it back on."},
    "started": {"vi": "Steam phản hồi (HTTP {status}). Cứ để app chạy trong lúc dùng Steam.",
                "en": "Steam responded (HTTP {status}). Keep the app running while you use Steam."},
    "restart_question": {"vi": "Mở lại app ngay để đổi ngôn ngữ?\n(Mở khoá Steam sẽ tắt vài giây.)",
                         "en": "Restart the app now to change the language?\n(The Steam unblock pauses for a few seconds.)"},
    "language_later": {"vi": "Ngôn ngữ mới sẽ dùng từ lần mở app sau.",
                       "en": "The new language applies the next time the app starts."},
    "need_admin": {"vi": "Cần quyền Administrator để tạo card mạng ảo (TUN).",
                   "en": "Administrator rights are needed to create the virtual network adapter (TUN)."},
})


class State(enum.Enum):
    IDLE = "idle"
    STARTING = "starting"
    ON = "on"
    STOPPING = "stopping"
    PAUSED = "paused"       # Roblox Shield's tunnel is up and unblocks Steam itself; resume when it goes down


def find_steam_exe() -> Path | None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
            path = Path(winreg.QueryValueEx(key, "SteamExe")[0])
        return path if path.is_file() else None
    except OSError:
        return None


def steam_url_registered() -> bool:
    try:
        winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"steam\shell\open\command").Close()
        return True
    except OSError:
        return False


def load_settings() -> dict:
    try:
        return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_settings(data: dict) -> None:
    tmp = SETTINGS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, SETTINGS_PATH)


class App:
    def __init__(self, root: tk.Tk, settings: dict) -> None:
        self.root = root
        self.show_event = NamedEvent(STEAM_SHOW_EVENT)
        self.settings = settings
        self.job = KillOnCloseJob()
        self.singbox = SingBox(CORE_DIR, name="steam-runtime", log_name="steam-unblock.log")
        self.events: queue.SimpleQueue[tuple] = queue.SimpleQueue()
        self.state = State.IDLE
        self.cancel_start = threading.Event()
        self.tunnel_flag = NamedMutex(STEAM_TUNNEL_MUTEX)
        self.yielding = False       # stopping because Roblox Shield wants the network, not because the user asked
        self.restart_requested = False
        self.tick_count = 0
        self.tray_hint_shown = False

        self._build_ui()
        self.tray = self._create_tray()
        self._render_state(tr("state_off"), COLOR_IDLE)
        self._sync_autostart()
        root.protocol("WM_DELETE_WINDOW", self.hide_to_tray)
        root.bind("<Unmap>", self._on_unmap)
        root.after(UI_TICK_MS, self._tick)
        self.start()

    # ── UI ────────────────────────────────────────────────────────────────────
    def _build_ui(self) -> None:
        self.root.title(APP_TITLE)
        self.root.minsize(460, 340)
        icon = BASE_DIR / "app_icon.ico"
        if icon.is_file():
            self.root.iconbitmap(default=str(icon))

        pad = {"padx": 12, "pady": 6}
        main = ttk.Frame(self.root, padding=8)
        main.pack(fill="both", expand=True)

        self.lbl_state = tk.Label(main, font=("Segoe UI", 15, "bold"), anchor="w")
        self.lbl_state.pack(fill="x", **pad)
        ttk.Label(main, foreground=COLOR_IDLE, wraplength=420, justify="left", text=tr("intro")).pack(fill="x", padx=12)

        buttons = ttk.Frame(main)
        buttons.pack(fill="x", **pad)
        self.btn_start = ttk.Button(buttons, text=tr("btn_start"), command=self.start)
        self.btn_start.pack(side="left", fill="x", expand=True)
        self.btn_steam = ttk.Button(buttons, text=tr("btn_steam"), command=self.launch_steam)
        self.btn_steam.pack(side="left", fill="x", expand=True, padx=8)
        self.btn_stop = ttk.Button(buttons, text=tr("btn_stop"), command=self.stop)
        self.btn_stop.pack(side="left", fill="x", expand=True)

        options = ttk.LabelFrame(main, text=tr("options"))
        options.pack(fill="x", **pad)
        self.var_autostart = tk.BooleanVar(value=False)
        ttk.Checkbutton(options, text=tr("autostart"), variable=self.var_autostart,
                        command=self._on_autostart_toggled).pack(anchor="w", padx=8, pady=(4, 0))
        modes = ttk.Frame(options)
        modes.pack(fill="x", padx=28)
        self.var_startup_mode = tk.StringVar(value=self.settings.get("startup_mode", STARTUP_TRAY))
        self.radio_modes = [
            ttk.Radiobutton(modes, text=tr("startup_tray"), value=STARTUP_TRAY,
                            variable=self.var_startup_mode, command=self._on_startup_mode_changed),
            ttk.Radiobutton(modes, text=tr("startup_window"), value=STARTUP_WINDOW,
                            variable=self.var_startup_mode, command=self._on_startup_mode_changed),
        ]
        for radio in self.radio_modes:
            radio.pack(side="left", padx=(0, 16))

        language = ttk.Frame(options)
        language.pack(fill="x", padx=8, pady=(4, 6))
        ttk.Label(language, text=tr("language")).pack(side="left")
        self.var_language = tk.StringVar(value=i18n.LANGUAGES[self.settings.get("language", "auto")])
        combo = ttk.Combobox(language, textvariable=self.var_language, values=list(i18n.LANGUAGES.values()),
                             state="readonly", width=18)
        combo.pack(side="left", padx=8)
        combo.bind("<<ComboboxSelected>>", self._on_language_changed)

        log_frame = ttk.LabelFrame(main, text=tr("log"))
        log_frame.pack(fill="both", expand=True, **pad)
        self.txt_log = tk.Text(log_frame, height=8, state="disabled", wrap="word", font=("Consolas", 9), relief="flat")
        scroll = ttk.Scrollbar(log_frame, command=self.txt_log.yview)
        self.txt_log.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.txt_log.pack(side="left", fill="both", expand=True)

    def _create_tray(self) -> pystray.Icon:
        """Tray callbacks run on pystray's thread, so they only post events for the Tk loop."""
        icon_path = BASE_DIR / "app_icon.ico"
        image = Image.open(icon_path) if icon_path.is_file() else Image.new("RGB", (64, 64), COLOR_OK)
        post = lambda action: lambda *_: self.events.put(("tray", action))
        menu = pystray.Menu(
            pystray.MenuItem(tr("tray_show"), post("show"), default=True),
            pystray.MenuItem(tr("btn_steam"), post("steam")),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(tr("tray_quit"), post("quit")),
        )
        tray = pystray.Icon("SteamUnblock", image, APP_TITLE, menu)
        tray.run_detached()
        return tray

    def _on_language_changed(self, _event=None) -> None:
        choice = next(code for code, name in i18n.LANGUAGES.items() if name == self.var_language.get())
        if choice == self.settings.get("language", "auto"):
            return
        self.settings["language"] = choice
        save_settings(self.settings)
        # The question is asked in the language the app will switch to, so the user can read it.
        previous = i18n.current()
        i18n.set_language(choice)
        if messagebox.askyesno(APP_TITLE, tr("restart_question")):
            self.restart_requested = True
            self._quit()
        else:
            i18n.set_language(previous)   # this window stays in its language until the next start
            self.log(tr("language_later"))

    # ── Start with Windows ────────────────────────────────────────────────────
    def _sync_autostart(self) -> None:
        """The scheduled task is the source of truth; refresh it so a moved app folder keeps working."""
        enabled = autostart.is_enabled(AUTOSTART_TASK)
        if enabled:
            try:
                autostart.enable(AUTOSTART_TASK, APP_TITLE)
            except OSError as exc:
                self.log(tr("autostart_refresh_failed", error=exc))
        self.var_autostart.set(enabled)
        self._render_startup_modes()

    def _render_startup_modes(self) -> None:
        for radio in self.radio_modes:
            radio.state(["!disabled"] if self.var_autostart.get() else ["disabled"])

    def _on_autostart_toggled(self) -> None:
        want = self.var_autostart.get()
        try:
            if want:
                autostart.enable(AUTOSTART_TASK, APP_TITLE)
            else:
                autostart.disable(AUTOSTART_TASK)
        except OSError as exc:
            self.var_autostart.set(not want)
            self.log(tr("autostart_on_failed" if want else "autostart_off_failed", error=exc))
        else:
            self.log(tr("autostart_on" if want else "autostart_off"))
        self._render_startup_modes()

    def _on_startup_mode_changed(self) -> None:
        self.settings["startup_mode"] = self.var_startup_mode.get()
        save_settings(self.settings)
        self.log(tr("startup_mode_tray" if self.starts_hidden() else "startup_mode_window"))

    def starts_hidden(self) -> bool:
        return self.settings.get("startup_mode", STARTUP_TRAY) == STARTUP_TRAY

    def hide_to_tray(self) -> None:
        self.root.withdraw()
        if not self.tray_hint_shown:
            self.tray_hint_shown = True
            self.tray.notify(tr("tray_hint"), APP_TITLE)

    def show_window(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _on_unmap(self, event: tk.Event) -> None:
        # Minimize button: go to the tray instead of the taskbar.
        if event.widget is self.root and self.root.state() == "iconic":
            self.hide_to_tray()

    def _render_state(self, text: str, color: str) -> None:
        self.tray.title = f"{APP_TITLE}: {text}"
        self.lbl_state.configure(text=text, fg=color)
        self.btn_start.state(["!disabled"] if self.state is State.IDLE else ["disabled"])
        self.btn_steam.state(["!disabled"] if self.state is State.ON else ["disabled"])
        self.btn_stop.state(["!disabled"] if self.state in (State.STARTING, State.ON, State.PAUSED) else ["disabled"])

    def log(self, message: str) -> None:
        self.txt_log.configure(state="normal")
        self.txt_log.insert("end", f"[{datetime.now():%H:%M:%S}] {message}\n")
        overflow = int(self.txt_log.index("end-1c").split(".")[0]) - LOG_MAX_LINES
        if overflow > 0:
            self.txt_log.delete("1.0", f"{overflow + 1}.0")
        self.txt_log.see("end")
        self.txt_log.configure(state="disabled")

    # ── Actions ───────────────────────────────────────────────────────────────
    def start(self) -> None:
        if self.state is not State.IDLE:
            return
        # Flag first, then look: Roblox Shield does the same in reverse, so at least one side always sees
        # the other and the two TUNs are never up together.
        self.tunnel_flag.acquire()
        if self._other_tunnel_up():
            self.tunnel_flag.release()
            self._pause()
            return
        self.state = State.STARTING
        self.cancel_start.clear()
        self._render_state(tr("state_starting"), COLOR_WARN)
        threading.Thread(target=self._start_worker, daemon=True).start()

    def _start_worker(self) -> None:
        try:
            config = build_steam_unblock_config(self.singbox.log_path, ipv6=host_has_ipv6())
            pid = self.singbox.start(config)
            if not self.job.assign(pid):
                self.events.put(("log", tr("warn_job")))
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            self.singbox.stop()
            self.events.put(("start_failed", tr("singbox_start_failed", error=exc)))
            return

        deadline = time.monotonic() + CONNECT_TIMEOUT_S
        last_issue = tr("no_response")
        while time.monotonic() < deadline and not self.cancel_start.is_set():
            if not self.singbox.is_alive():
                last_issue = f"{tr('singbox_exited')}\n{self.singbox.log_tail()}"
                break
            flush_dns_cache()
            try:
                self.events.put(("started", probe_steam()))
                return
            except OSError as exc:
                last_issue = str(exc)
            self.cancel_start.wait(1.0)

        self.singbox.stop()
        flush_dns_cache()
        reason = tr("cancelled") if self.cancel_start.is_set() else tr("steam_unreachable", error=last_issue)
        self.events.put(("start_failed", reason))

    def stop(self) -> None:
        self.yielding = False
        if self.state is State.STARTING:
            self.cancel_start.set()
        elif self.state is State.ON:
            self._shutdown(tr("state_stopped"), COLOR_IDLE)
        elif self.state is State.PAUSED:
            self.state = State.IDLE
            self._render_state(tr("state_stopped"), COLOR_IDLE)
            self.log(tr("stopped_no_resume"))

    def _other_tunnel_up(self) -> bool:
        """
        Roblox Shield's flag, or any sing-box that is not ours. The flag alone is not enough: a sing-box that
        outlives its owner (crash before it was bound to a job) keeps its strict-route TUN up, and starting
        ours next to it cuts the whole machine off the network.
        """
        if mutex_exists(SHIELD_TUNNEL_MUTEX):
            return True
        return bool(set(find_processes(SINGBOX_NAMES)) - {self.singbox.pid})

    def _pause(self) -> None:
        if self.state is State.PAUSED:
            return
        self.yielding = False
        self.state = State.PAUSED
        self._render_state(tr("state_paused"), COLOR_IDLE)
        self.log(tr("paused"))

    def _yield_to_shield(self) -> None:
        """Roblox Shield is connecting: take our TUN down; it waits for our flag to disappear."""
        self.yielding = True
        if self.state is State.STARTING:
            self.cancel_start.set()
        elif self.state is State.ON:
            self._shutdown("", COLOR_IDLE)

    def _shutdown(self, reason: str, color: str) -> None:
        self.state = State.STOPPING
        self._render_state(tr("state_stopping"), COLOR_WARN)

        def worker() -> None:
            self.singbox.stop()
            flush_dns_cache()  # fake IPs must not outlive the TUN
            self.events.put(("stopped", reason, color))

        threading.Thread(target=worker, daemon=True).start()

    def launch_steam(self) -> None:
        exe = find_steam_exe()
        if exe:
            # explorer.exe starts Steam with the normal (non-admin) user token.
            subprocess.Popen(["explorer.exe", str(exe)], close_fds=True)
        elif steam_url_registered():
            os.startfile("steam://open/main")
        else:
            # Steam is not installed yet: its download page is reachable now that Steam is unblocked.
            webbrowser.open(STEAM_DOWNLOAD_URL)
            self.log(tr("steam_not_installed"))
            return
        self.log(tr("opening_steam"))

    # ── Event loop / watchdog ─────────────────────────────────────────────────
    def _tick(self) -> None:
        try:
            while True:
                self._handle_event(self.events.get_nowait())
        except queue.Empty:
            pass

        self.tick_count += 1
        if self.show_event.is_set():
            self.show_window()
        if self.tick_count % WATCHDOG_EVERY_TICKS == 0:
            self._watchdog()
        self.root.after(UI_TICK_MS, self._tick)

    def _watchdog(self) -> None:
        other_up = self._other_tunnel_up()
        if other_up and self.state in (State.STARTING, State.ON) and not self.yielding:
            self._yield_to_shield()
        elif not other_up and self.state is State.PAUSED:
            self.state = State.IDLE
            self.log(tr("resuming"))
            self.start()
        elif self.state is State.ON and not self.singbox.is_alive():
            tail = self.singbox.log_tail()
            self.log(f"{tr('singbox_died')}\n{tail}" if tail else tr("singbox_died"))
            self._shutdown(tr("state_crashed"), COLOR_ERR)
            self.tray.notify(tr("crash_notify"), APP_TITLE)

    def _on_tunnel_down(self, reason: str, color: str) -> None:
        """sing-box is stopped and DNS flushed: drop our flag so Roblox Shield may start its TUN."""
        self.tunnel_flag.release()
        if self.yielding:
            self._pause()
            return
        self.state = State.IDLE
        self._render_state(reason, color)
        self.log(reason)

    def _handle_event(self, event: tuple) -> None:
        kind = event[0]
        if kind == "log":
            self.log(event[1])
        elif kind == "tray":
            {"show": self.show_window, "steam": self.launch_steam, "quit": self._quit}[event[1]]()
        elif kind == "started":
            self.state = State.ON
            self._render_state(tr("state_on"), COLOR_OK)
            self.log(tr("started", status=event[1]))
        elif kind == "start_failed":
            if not self.yielding:
                self.log(event[1])
            self._on_tunnel_down(tr("state_not_on"), COLOR_ERR)
        elif kind == "stopped":
            self._on_tunnel_down(event[1], event[2])

    def _quit(self) -> None:
        self.cancel_start.set()
        # Hide first: stopping sing-box can take a few seconds and the window must not look frozen.
        self.root.withdraw()
        self.tray.stop()
        self.root.update_idletasks()
        self.singbox.stop()
        flush_dns_cache()
        self.tunnel_flag.release()
        self.root.destroy()


def restart_command() -> list[str]:
    # The new copy inherits our elevation, so there is no UAC prompt. --autostart is dropped on purpose:
    # the user is looking at the window, so it must reopen.
    if FROZEN:
        return [sys.executable]
    return [sys.executable, str(Path(__file__).resolve())]


def main() -> None:
    enable_dpi_awareness()
    settings = load_settings()
    i18n.set_language(settings.get("language"))
    if not is_admin():
        if not relaunch_as_admin():
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror(APP_TITLE, tr("need_admin"))
        return
    instance = NamedMutex(STEAM_APP_MUTEX)
    if not instance.acquire():
        signal_event(STEAM_SHOW_EVENT)
        return
    root = tk.Tk()
    root.withdraw()   # hidden until the first-run check passes, so its dialogs have a parent
    if not setup_check.ensure_ready(BASE_DIR, CORE_DIR, APP_TITLE, "SteamUnblock.exe", RELEASES_URL):
        root.destroy()
        return
    app = App(root, settings)
    if not (autostart.AUTOSTART_ARG in sys.argv and app.starts_hidden()):
        root.deiconify()
    root.mainloop()
    if app.restart_requested:
        instance.release()   # or the new copy would see us and just exit
        subprocess.Popen(restart_command(), cwd=BASE_DIR, close_fds=True)


if __name__ == "__main__":
    main()
