"""The PC itself: speaker volume, screen brightness, and power (shut down, restart, sleep, lock, sign out)."""
import ctypes
import logging
import re
import subprocess
import threading
import time

from ..phrases import about_the_computer, words

log = logging.getLogger("jarvis")

STEP = 10                # percent that 'up', 'down', 'louder' or 'dimmer' change things by
SHUTDOWN_SECONDS = 30    # warning before shutting down or restarting, long enough to say 'cancel'
POWER_DELAY = 4          # seconds before sleeping, locking or signing out, so Jarvis finishes saying so

UP = {"up", "louder", "higher", "brighter", "increase", "more", "raise", "boost"}
DOWN = {"down", "quieter", "lower", "dimmer", "darker", "decrease", "less", "reduce", "dim"}
CHECK = {"check", "current", "status", "now", "what", ""}   # what set_volume and set_brightness take for 'just say it'

# the power actions, and the names the model may give them
POWER_ACTIONS = {
    "shutdown": {"shutdown", "shut down", "turn off", "power off", "switch off"},
    "restart": {"restart", "reboot"},
    "sleep": {"sleep", "suspend", "standby"},
    "lock": {"lock"},
    "sign out": {"sign out", "signout", "log out", "logout", "log off", "logoff"},
    "cancel": {"cancel", "abort", "stop"},
}
# The words the user must have said for each action, so the model can't power the PC off on its own
# initiative. 'turn off' only counts with the computer named: 'turn off the music' isn't a shutdown.
ASKED_FOR = {
    "shutdown": {"shut", "shutdown"},
    "restart": {"restart", "reboot"},
    "sleep": {"sleep", "suspend", "standby"},
    "lock": {"lock"},
    "sign out": {"sign out", "log out", "logout", "log off", "logoff"},
    "cancel": {"cancel", "abort", "stop", "don't", "dont"},
}
SWITCH_OFF = {"turn off", "power off", "switch off"}   # a shutdown, when said about the computer


def _asked_for(action: str, request: str) -> bool:
    said = f" {words(request)} "
    if any(f" {w} " in said for w in ASKED_FOR[action]):
        return True
    return action == "shutdown" and about_the_computer(request) and any(f" {w} " in said for w in SWITCH_OFF)


def _parse_change(change: str) -> tuple[str, int] | None:
    """What a change asks for: ('to', 50) for '50', '50%', 'max' or 'min'; ('by', 10) for '+10' or
    'up', ('by', -10) for '-10' or 'down'. None if it isn't one of those."""
    change = change.lower().strip().rstrip("%").strip()
    if change in UP:
        return "by", STEP
    if change in DOWN:
        return "by", -STEP
    if change in ("max", "maximum", "full", "highest"):
        return "to", 100
    if change in ("min", "minimum", "lowest"):
        return "to", 0
    number = re.fullmatch(r"([+-]?)\s*(\d+(?:\.\d+)?)", change)
    if not number:
        return None
    sign, amount = number.group(1), round(float(number.group(2)))
    return ("to", amount) if not sign else ("by", amount if sign == "+" else -amount)


def _clamp(level: int) -> int:
    return min(max(level, 0), 100)


def level_asked(request: str, can_mute: bool) -> str | None:
    """The volume or brightness change the user's own words ask for, in the form set_volume and
    set_brightness take: 'set the volume to 30' is '30', 'turn it up by 20' '+20', 'a bit more'
    'up', 'mute' 'mute'. None if the words don't say plainly. Small models pick the wrong level
    or claim a change they never made, so a plain request is carried out without them."""
    said = words(request)
    asked = set(said.split())
    if can_mute and "unmute" in asked:
        return "unmute"
    if can_mute and asked & {"mute", "silence"}:
        return "mute"
    if asked & {"max", "maximum", "full"}:
        return "max"
    up, down = asked & UP, asked & DOWN
    number = re.search(r"\b(\d{1,3})\b", said)
    if number:
        if "by" in asked and bool(up) != bool(down):   # 'up by 20': a change, not a level
            return f"+{number.group(1)}" if up else f"-{number.group(1)}"
        return number.group(1)
    if bool(up) != bool(down):
        return "up" if up else "down"
    return None


# ---------------------------------------------------------------- volume

def _speakers():
    """The default speakers' volume control. COM has to be set up on each thread that uses it,
    and the tools run on the voice thread."""
    import comtypes
    from pycaw.pycaw import AudioUtilities

    comtypes.CoInitialize()
    return AudioUtilities.GetSpeakers().EndpointVolume


def set_volume(level: str) -> str:
    """Change the speaker volume, mute or unmute it, or find out what it is now.

    Args:
      level: 'up' or 'down' for a step, a change like '+20' or '-20', a percentage like '50', 'mute', 'unmute', or 'check' to only find out the volume
    """
    try:
        speakers = _speakers()
    except ImportError:
        return "Failed: volume control isn't installed (pip install pycaw)."
    except Exception as e:
        return f"Failed: couldn't reach the speakers ({e})."
    asked = (level or "").lower().strip()
    if asked in ("mute", "off", "silent"):
        speakers.SetMute(1, None)
        return "Muted the sound."
    current = round(speakers.GetMasterVolumeLevelScalar() * 100)   # read fresh: it may have been changed by hand
    if asked in CHECK:
        muted = " and muted" if speakers.GetMute() else ", not muted"
        return f"The volume is {current}%{muted}."
    if asked in ("unmute", "on"):
        speakers.SetMute(0, None)
        return f"Unmuted the sound. The volume is {current}%."
    change = _parse_change(asked)
    if change is None:
        return f"Failed: '{level}' isn't a volume. Use up, down, a change like +20, a percentage, mute or unmute."
    kind, amount = change
    new = _clamp(current + amount if kind == "by" else amount)
    speakers.SetMasterVolumeLevelScalar(new / 100, None)
    speakers.SetMute(0, None)   # asking for a volume means wanting to hear it
    return f"The volume is now {new}% (it was {current}%)."


# ---------------------------------------------------------------- brightness

# WMI's brightness control, through PowerShell: reads the level and sets the new one in one go
BRIGHTNESS_SCRIPT = (
    "$screen = Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightness -ErrorAction Stop | Select-Object -First 1; "
    "$current = $screen.CurrentBrightness; $new = {new}; "
    "Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightnessMethods -ErrorAction Stop | Select-Object -First 1 | "
    "Invoke-CimMethod -MethodName WmiSetBrightness -Arguments @{{Timeout = 0; Brightness = [byte]$new}} | Out-Null; "
    "\"$current $new\""
)


def powershell(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                          capture_output=True, text=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)


def set_brightness(level: str) -> str:
    """Change the screen brightness, or find out what it is now.

    Args:
      level: 'up' or 'down' for a step, a change like '+20' or '-20', a percentage like '50', or 'check' to only find out the brightness
    """
    asked = (level or "").lower().strip()
    if asked in CHECK:
        new = "$current"   # set to what it is: the same script then just reads it
    else:
        change = _parse_change(asked)
        if change is None:
            return f"Failed: '{level}' isn't a brightness. Use up, down, a change like +20, a percentage, or check."
        kind, amount = change   # whole numbers only, so nothing the model wrote reaches the script
        new = f"[Math]::Min(100, [Math]::Max(0, $current + {amount}))" if kind == "by" else str(_clamp(amount))
    result = powershell(BRIGHTNESS_SCRIPT.format(new=new))
    if result.returncode != 0 or not result.stdout.strip():
        log.info("[brightness] %s", result.stderr.strip()[:300])
        return ("Failed: Windows can't change this screen's brightness. That works on laptop screens; "
                "an external monitor needs its own buttons.")
    current, new_level = result.stdout.split()
    if asked in CHECK:
        return f"The brightness is {current}%."
    return f"The brightness is now {new_level}% (it was {current}%)."


# ---------------------------------------------------------------- power

class Power:
    def __init__(self):
        self.request = ""   # what the user just said, set by Tools.begin
        self.shutdown_at = 0.0   # time.monotonic() a shutdown or restart Jarvis asked for will happen

    def cancel_asked(self, request: str) -> bool:
        """Whether this asks to stop a shutdown or restart that is still counting down."""
        return time.monotonic() < self.shutdown_at and _asked_for("cancel", request)

    def computer_power(self, action: str) -> str:
        """Shut down, restart, sleep, lock or sign out of this PC, or cancel a shutdown or restart.
        Only when the user asks for it.

        Args:
          action: 'shutdown', 'restart', 'sleep', 'lock', 'sign out', or 'cancel' to stop a shutdown or restart
        """
        asked = words(action or "")
        name = next((n for n, names in POWER_ACTIONS.items() if asked == n or asked in names), None)
        if name is None:
            return f"Failed: '{action}' isn't a power action. Use shutdown, restart, sleep, lock, sign out or cancel."
        if not _asked_for(name, self.request):
            log.info("[tool] refusing %r: the user didn't ask for it", name)
            verb = {"shutdown": "shut down", "cancel": "cancel a shutdown"}.get(name, name)
            return f"Failed: the user didn't ask to {verb}, so I didn't."

        if name in ("shutdown", "restart"):
            flag = "/s" if name == "shutdown" else "/r"
            result = subprocess.run(["shutdown", flag, "/t", str(SHUTDOWN_SECONDS)], capture_output=True, text=True,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            if result.returncode != 0:
                return f"Failed: Windows didn't {name} ({result.stderr.strip() or result.returncode})."
            self.shutdown_at = time.monotonic() + SHUTDOWN_SECONDS
            verb = "shut down" if name == "shutdown" else "restart"
            return f"The PC will {verb} in {SHUTDOWN_SECONDS} seconds. Saying 'cancel' stops it."
        if name == "cancel":
            result = subprocess.run(["shutdown", "/a"], capture_output=True, text=True,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            self.shutdown_at = 0.0
            return "Cancelled the shutdown." if result.returncode == 0 else "There was no shutdown or restart to cancel."

        # sleep, lock and sign out: after a moment, so Jarvis gets to say it first
        threading.Timer(POWER_DELAY, {"sleep": _sleep, "lock": _lock, "sign out": _sign_out}[name]).start()
        return {"sleep": "Putting the PC to sleep.", "lock": "Locking the PC.",
                "sign out": "Signing out of Windows. Unsaved work in open apps may be lost."}[name]


def _modern_standby() -> bool:
    """Whether this PC sleeps the way phones do (S0 low power idle, 'Modern Standby') rather than
    the classic way (S3). Most new laptops do; they have no classic sleep to ask for."""
    caps = ctypes.create_string_buffer(128)   # SYSTEM_POWER_CAPABILITIES; byte 5 is SystemS3, byte 20 AoAc
    if not ctypes.WinDLL("powrprof").GetPwrCapabilities(ctypes.byref(caps)):
        return False
    return bool(caps.raw[20]) and not caps.raw[5]


def _sleep() -> None:
    if _modern_standby():
        # turning the screen off is how Modern Standby PCs go to sleep, like pressing the power button
        HWND_BROADCAST, WM_SYSCOMMAND, SC_MONITORPOWER, OFF = 0xFFFF, 0x0112, 0xF170, 2
        ctypes.WinDLL("user32").PostMessageW(HWND_BROADCAST, WM_SYSCOMMAND, SC_MONITORPOWER, OFF)
    else:
        ctypes.WinDLL("powrprof").SetSuspendState(False, False, False)   # sleep, not hibernate


def _lock() -> None:
    ctypes.WinDLL("user32").LockWorkStation()


def _sign_out() -> None:
    subprocess.run(["shutdown", "/l"], creationflags=subprocess.CREATE_NO_WINDOW)
