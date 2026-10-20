"""Opening things on screen: apps, websites, new browser tabs and Google searches. Closing apps."""
import ctypes
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import webbrowser
import winreg
from ctypes import wintypes
from urllib.parse import quote_plus

import psutil

from ..config import Config

log = logging.getLogger("jarvis")

_user32 = ctypes.WinDLL("user32")
_dwmapi = ctypes.WinDLL("dwmapi")
_kernel32 = ctypes.WinDLL("kernel32")
_kernel32.GetConsoleWindow.restype = wintypes.HWND
_EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
WM_CLOSE = 0x0010           # the message clicking a window's X sends
GW_OWNER = 4                # a window's owner: dialogs and popups have one, app windows don't
GWL_EXSTYLE = -20           # a window's extended style flags
WS_EX_TOOLWINDOW = 0x80     # floating tool windows, which aren't on the taskbar
DWMWA_CLOAKED = 14          # whether a window is kept alive but hidden (like suspended Store apps)
# the desktop and the taskbar: never close these
_SHELL = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}


def _open_windows() -> list[tuple[wintypes.HWND, str, str]]:
    """The app windows on the taskbar: (handle, lowercase title, program like 'excel')."""
    found, my_pid, my_console = [], os.getpid(), _kernel32.GetConsoleWindow()

    def collect(handle, _):   # called by Windows for every top-level window; True: keep going
        hwnd = wintypes.HWND(handle)
        if (handle == my_console or not _user32.IsWindowVisible(hwnd) or _user32.GetWindow(hwnd, GW_OWNER)
                or _user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW):
            return True
        cloaked, pid = wintypes.DWORD(), wintypes.DWORD()
        _dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        class_name, title = ctypes.create_unicode_buffer(256), ctypes.create_unicode_buffer(512)
        _user32.GetClassNameW(hwnd, class_name, 256)
        _user32.GetWindowTextW(hwnd, title, 512)
        if cloaked.value or pid.value == my_pid or class_name.value in _SHELL or not title.value:
            return True
        try:
            program = psutil.Process(pid.value).name().lower().removesuffix(".exe")
        except psutil.Error:
            program = ""
        # Edge puts an invisible space in 'Microsoft Edge'
        found.append((hwnd, title.value.replace("​", " ").lower().strip(), program))
        return True

    _user32.EnumWindows(_EnumWindowsProc(collect), 0)
    return found


def _program_in(target: str) -> str:
    """The program a Start menu entry runs, when its ID names it: 'winword' for Word."""
    m = re.search(r"([^\\.]+)\.exe", target, re.IGNORECASE)
    return m.group(1).lower() if m else ""

# Start menu entries that aren't programs: uninstallers, help pages and web links
_NOT_APPS = re.compile(r"^uninstall\b|://|\.(url|html?|chm|txt|pdf)$", re.IGNORECASE)


def _start_menu_apps() -> dict[str, str]:
    """Every app in this PC's Start menu, desktop and Store alike: lowercase name -> what opens it."""
    script = "[Console]::OutputEncoding = [Text.Encoding]::UTF8; Get-StartApps | ConvertTo-Json -Compress"
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                             capture_output=True, text=True, encoding="utf-8", timeout=30,
                             creationflags=subprocess.CREATE_NO_WINDOW).stdout
        found = json.loads(out or "[]")
    except Exception as e:
        log.warning("Couldn't list the Start menu apps: %s", e)
        return {}
    if isinstance(found, dict):   # a single app comes back as an object, not a list
        found = [found]
    apps = {}
    for app in found:
        name, app_id = app["Name"].lower().strip(), app["AppID"]
        if not (_NOT_APPS.search(name) or _NOT_APPS.search(app_id)):
            apps.setdefault(name, rf"shell:AppsFolder\{app_id}")
    log.info("Found %d apps in the Start menu", len(apps))
    return apps


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def _spells(short: str, words: list[str]) -> bool:
    """Whether short is made of the starts of some of these words, in order:
    'chrome' fits 'google chrome', 'vscode' fits 'visual studio code' (v-s-code)."""
    if not short:
        return True
    for i, word in enumerate(words):
        for n in range(min(len(word), len(short)), 0, -1):
            if short[:n] == word[:n] and _spells(short[n:], words[i + 1:]):
                return True
    return False


def _installed(name: str) -> bool:
    """Whether 'start <name>' can find this program: on PATH, or registered under App Paths (like winword)."""
    if shutil.which(name):
        return True
    exe = name if name.endswith(".exe") else name + ".exe"
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            winreg.CloseKey(winreg.OpenKey(hive, rf"Software\Microsoft\Windows\CurrentVersion\App Paths\{exe}"))
            return True
        except OSError:
            pass
    return False


def _start(*args: str) -> None:
    subprocess.Popen(["cmd", "/c", "start", "", *args], shell=False)


class Launcher:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.request = ""   # what the user just said, set by Tools.begin
        # this PC's apps (Start menu name -> what opens it), looked up fresh each launch since every
        # PC has different ones; that takes a few seconds, so it runs in the background
        self.apps: dict[str, str] = {}
        self._scanning = threading.Thread(target=self._scan_start_menu, daemon=True)
        self._scanning.start()

    def _scan_start_menu(self) -> None:
        self.apps = _start_menu_apps()

    def _match(self, key: str) -> str | None:
        """This app's Start menu name: the exact name, or the one it's short for."""
        if key in self.apps:
            return key
        short = "".join(_words(key))
        matches = [n for n in self.apps if short and _spells(short, _words(n))]
        return min(matches, key=len) if matches else None   # shortest is the plainest

    def _find(self, key: str) -> str | None:
        """What opens this app (a Start menu entry or a command), or None if it isn't on this PC."""
        if key in self.apps:
            return self.apps[key]
        if _installed(key):   # a command like 'cmd', or registered under App Paths like 'winword'
            return key
        app = self._match(key)
        return self.apps[app] if app else None

    def open_app(self, name: str) -> str:
        """Open an application on this Windows PC.

        Args:
          name: The app to open, for example 'spotify', 'notepad', 'calculator', 'chrome', 'settings'
        """
        key = name.lower().strip()
        if key in self.cfg.sites:   # the model often treats sites like 'gmail' as apps
            return self.open_website(key)
        # 'start' returns straight away even for a missing app (Windows shows an error box),
        # so check first or the model would be told it worked
        self._scanning.join()
        target = self._find(key)
        if target is None:   # maybe installed since Jarvis started
            self._scan_start_menu()
            target = self._find(key)
        if target is None:
            return f"Failed: {name} isn't installed on this PC."
        try:
            if target.startswith("shell:"):
                os.startfile(target)   # straight to Windows, so no cmd quoting to trip over
            else:
                _start(target)
            return f"Opened {name}."
        except Exception as e:
            return f"Failed to open {name}: {e}"

    def close_app(self, name: str) -> str:
        """Close an application that is open on this Windows PC, like clicking its X.

        Args:
          name: The app to close, for example 'excel', 'chrome', 'notepad', 'calculator'
        """
        key = name.lower().strip()
        if key in self.cfg.sites:
            return f"Failed: {name} is a website. I can only close whole apps, not browser tabs."
        windows = self._windows_of(key)
        if not windows:
            return f"Failed: {name} isn't open."

        # a polite request, the same as clicking X, so apps with unsaved work still ask whether to save
        refused = [hwnd for hwnd in windows if not _user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)]
        if len(refused) == len(windows):
            return f"Failed: Windows didn't let me close {name}. It may be running as administrator."
        deadline = time.monotonic() + 2
        while any(_user32.IsWindowVisible(hwnd) for hwnd in windows) and time.monotonic() < deadline:
            time.sleep(0.1)
        if any(_user32.IsWindowVisible(hwnd) for hwnd in windows):
            return f"Asked {name} to close, but it's still open. It's probably asking whether to save changes."
        return f"Closed {name}."

    def _windows_of(self, key: str) -> list[wintypes.HWND]:
        """The open windows of an app, found by the program behind them or by their title."""
        self._scanning.join()
        app = self._match(key)   # its Start menu name, e.g. 'google chrome' for 'chrome'
        titles = {key, app} - {None}   # app windows are titled like 'Book1 - Excel'
        words = _words(key) or [key]
        # the program behind it: 'spotify', 'explorer' for 'file explorer', 'winword' for 'word'
        programs = {"".join(words), words[-1], _program_in(self.apps[app]) if app else ""} - {""}
        return [hwnd for hwnd, title, program in _open_windows()
                if program in programs or any(re.search(rf"(^|\W){re.escape(t)}$", title) for t in titles)]

    def open_website(self, url: str = "", browser: str = "") -> str:
        """Open a website, or a blank new tab, in a browser.

        Args:
          url: The website address like 'youtube.com', or a site name like 'gmail' or 'gmail compose'. Leave empty for a blank new tab.
          browser: Only if the user names a browser, for example 'firefox' or 'chrome'. Leave empty for the default browser.
        """
        url = (url or "").strip()            # the model sometimes passes None
        asked = (browser or "").lower().strip()
        if not url and asked in self.cfg.sites:   # site put in the wrong argument, e.g. browser='gmail'
            url, asked = asked, ""
        url = self._address(url)

        # substring match so 'microsoft edge' finds 'edge' and 'google chrome' finds 'chrome'
        name = next((n for n in self.cfg.browsers if n in asked), None) if asked else None
        request = self.request.lower()
        if asked and asked not in request and not (name and name in request):
            log.info("[tool] ignoring browser %r, the user didn't ask for it", asked)
            asked, name = "", None
        known = ", ".join(self.cfg.browsers)
        if asked and name is None:
            return f"Failed: I don't know the browser {browser}. I can use {known}."

        if name is None:   # the default browser
            if not url:
                return f"Say which browser to open a new tab in: {known}."
            webbrowser.open(url)
            return f"Opened {url} in the default browser."
        chosen = self.cfg.browsers[name]
        try:
            _start(chosen.command, *chosen.args, url or chosen.new_tab)
            return f"Opened {url or 'a new tab'} in {name}."
        except Exception as e:
            return f"Failed to open {name}: {e}"

    def _address(self, url: str) -> str:
        """The full address for a site name ('gmail'), a bare domain ('youtube.com') or an address;
        empty for a blank new tab."""
        url = self.cfg.sites.get(url.lower(), url)
        if url.lower() in ("new tab", "newtab", "blank"):
            return ""
        if url and ":" not in url.split("/")[0]:  # bare domain like 'youtube.com'
            return "https://" + url
        return url

    @staticmethod
    def search_web(query: str) -> str:
        """Open Google search results in the browser, ONLY when the user wants to see the results on screen.

        Args:
          query: What to search for
        """
        webbrowser.open("https://www.google.com/search?q=" + quote_plus(query))
        return f"Opened a Google search for {query}."
