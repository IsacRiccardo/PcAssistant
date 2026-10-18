"""
Jarvis overlay: a floating animated orb that fades in above the taskbar when Jarvis wakes up
and fades away when it goes back to sleep. No window frame, no taskbar button, clicks pass
straight through it and it never takes keyboard focus.
Only used in voice mode. The voice loop runs on a worker thread and just sets attributes here;
the animation reads them on the Tk (main) thread, `fps` times a second.
Looks are set in config/ui.json; the values here are fixed by Windows or by how the drawing works.
"""
import ctypes
import ctypes.wintypes
import logging
import math
import random
import signal
import sys
import threading
import tkinter as tk

from .config import UiConfig
from .status import Status

KEY = "#010203"         # this exact colour is see-through, so only the orb and caption show
CAPTION_GAP = 17        # space between the outermost ring and the caption
BASE_FPS = 30           # frame rate the animation's internal motion constants were tuned at

# Win32 bits for the see-through, click-through overlay
GWL_EXSTYLE = -20
WS_EX_LAYERED, WS_EX_TRANSPARENT, WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x80000, 0x20, 0x80, 0x08000000
LWA_COLORKEY, LWA_ALPHA = 0x1, 0x2
SPI_GETWORKAREA = 0x30
user32 = ctypes.windll.user32

log = logging.getLogger("jarvis")


def _rgb(color: str) -> list[float]:
    return [int(color[i:i + 2], 16) for i in (1, 3, 5)]


def _hex(rgb: list[float]) -> str:
    return "#%02x%02x%02x" % tuple(round(v) for v in rgb)


def _mix(a: list[float], b: list[float], t: float) -> str:
    """Blend two rgb colours into a Tk colour string: t=0 gives a, t=1 gives b."""
    return _hex([x + (y - x) * t for x, y in zip(a, b)])


def _clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


def _rounded(x1, y1, x2, y2, r):
    """Points for a smoothed polygon that draws as a rounded rectangle."""
    return [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
            x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]


def _work_area() -> ctypes.wintypes.RECT:
    """The screen, not counting the taskbar."""
    r = ctypes.wintypes.RECT()
    user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(r), 0)
    return r


WHITE = [255, 255, 255]
KEY_COLORREF = sum(v << s for v, s in zip(_rgb(KEY), (0, 8, 16)))


class Hud(Status):
    def __init__(self, cfg: UiConfig):
        self.cfg = cfg
        self.state = "starting"
        self.you = self.reply = ""
        self.level = 0.0
        self.angle = 0.0
        self.alpha = 0.0
        self.frame = 0
        self.rgb = _rgb(cfg.colors["starting"])
        self.color = ""                      # orb colour last drawn, to skip recolouring when unchanged
        self.noise = [0.0] * cfg.bars
        self._shown = None
        self.closing = False

        previous = user32.GetForegroundWindow()   # Tk grabs focus when it first shows; give it back after
        root = self.root = tk.Tk()
        root.overrideredirect(True)          # no frame or title bar
        root.configure(bg=KEY)
        area = _work_area()                  # centred, just above the taskbar
        x = (area.left + area.right - cfg.width) // 2
        root.geometry(f"{cfg.width}x{cfg.height}+{x}+{area.bottom - cfg.height - cfg.margin}")
        root.attributes("-topmost", True)
        self.canvas = tk.Canvas(root, width=cfg.width, height=cfg.height, bg=KEY, highlightthickness=0)
        self.canvas.pack()
        self._build()

        root.update_idletasks()
        self.hwnd = user32.GetParent(root.winfo_id())
        style = user32.GetWindowLongW(self.hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(self.hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED | WS_EX_TRANSPARENT
                              | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
        self._set_alpha(0.0)
        if previous:
            user32.SetForegroundWindow(previous)

    # Called from the voice thread: plain attribute writes, picked up by the next frame
    def set_state(self, state: str) -> None:
        if self.state == "sleeping" and state == "listening":
            self.you = self.reply = ""       # fresh caption for a new conversation
        self.state = state

    def heard(self, text: str) -> None:
        self.you, self.reply = text, ""

    def replied(self, text: str) -> None:
        self.reply = text

    def close(self) -> None:
        self.closing = True                  # the next frame destroys the window on the Tk thread

    def run(self, work) -> None:
        """Run work() on a background thread and animate the overlay until Ctrl+C."""
        def guarded():
            try:
                work()
            except Exception as e:
                log.exception("Voice loop crashed")
                print(f"Error: {e}", file=sys.stderr)
                self.reply, self.state = str(e), "error"

        threading.Thread(target=guarded, daemon=True).start()
        signal.signal(signal.SIGINT, lambda *_: self.root.after(0, self.root.destroy))
        self.root.after(0, self._tick)
        self.root.mainloop()

    def _set_alpha(self, alpha: float) -> None:
        user32.SetLayeredWindowAttributes(self.hwnd, KEY_COLORREF, int(alpha * 255),
                                          LWA_COLORKEY | LWA_ALPHA)

    def _build(self):
        cfg, c, m, y = self.cfg, self.canvas, self.cfg.width / 2, self.cfg.orb_y
        self.panel = c.create_polygon(0, 0, 0, 0, smooth=True, fill=cfg.panel_color, outline="",
                                      state="hidden")
        self.you_text = c.create_text(m, 0, anchor="n", justify="center", width=cfg.width - 48,
                                      font=(cfg.font, cfg.heard_font_size), fill=cfg.heard_color)
        self.reply_text = c.create_text(m, 0, anchor="n", justify="center", width=cfg.width - 48,
                                        font=(cfg.font, cfg.reply_font_size))
        self.halo = c.create_oval(0, 0, 0, 0, width=1)
        self.bars = [c.create_line(0, 0, 0, 0, width=3, capstyle="round") for _ in range(cfg.bars)]
        self.rings = [(c.create_arc(m - r.radius, y - r.radius, m + r.radius, y + r.radius, style="arc",
                                    extent=r.length, width=r.width), r.start, r.speed) for r in cfg.rings]
        self.core = c.create_oval(0, 0, 0, 0, width=2)
        self.caption_y = y + max((r.radius for r in cfg.rings), default=95) + CAPTION_GAP

    def _ease(self, k: float) -> float:
        """Per-frame smoothing amount k, tuned at BASE_FPS, adjusted so motion keeps its speed at any fps."""
        return 1 - (1 - k) ** (BASE_FPS / self.cfg.fps)

    def _tick(self):
        if self.closing:
            self.root.destroy()
            return
        cfg = self.cfg
        self.root.after(1000 // cfg.fps, self._tick)
        state = self.state
        target = 0.0 if state == "sleeping" else cfg.opacity
        if self.alpha != target:
            fade = cfg.opacity / max(cfg.fade_seconds * cfg.fps, 1)   # opacity change per frame
            step = fade if target > self.alpha else -fade
            self.alpha = min(max(self.alpha + step, 0.0), cfg.opacity)
            if abs(self.alpha - target) < fade:
                self.alpha = target
            self._set_alpha(self.alpha)
        if self.alpha == 0.0:
            return                           # hidden, nothing to draw
        self._draw_orb(state)
        self._draw_caption(state)

    def _draw_orb(self, state: str):
        cfg = self.cfg
        self.frame += 1
        t = self.frame * BASE_FPS / cfg.fps     # animation time, in BASE_FPS frames
        self.rgb = [a + (b - a) * self._ease(0.12)                       # fade between states
                    for a, b in zip(self.rgb, _rgb(cfg.colors[state]))]
        color = _hex(self.rgb)
        recolor, self.color = color != self.color, color   # once a fade settles, colours stay put

        if state == "listening":
            target = min(1.0, self.mic_loudness / cfg.mic_full_scale)
        elif state == "speaking":
            target = self.voice_level
        elif state in ("thinking", "starting"):
            target = 0.3 + 0.1 * math.sin(t * 0.3)
        else:
            target = 0.1 + 0.08 * math.sin(t * 0.06)   # slow breathing
        self.level += (target - self.level) * self._ease(0.3)
        lv = self.level

        c, m, y = self.canvas, cfg.width / 2, cfg.orb_y
        core_r = 28 + 18 * lv
        r = core_r + 5
        c.coords(self.halo, m - r, y - r, m + r, y + r)
        if recolor:
            c.itemconfig(self.halo, outline=color)

        inner = core_r + 11
        noise = self._ease(0.35)
        for i, item in enumerate(self.bars):
            a = 2 * math.pi * i / cfg.bars + math.radians(self.angle) * 0.3
            if state in ("listening", "speaking"):
                self.noise[i] += (random.random() - self.noise[i]) * noise
                length = 3 + 32 * lv * self.noise[i]
            elif state in ("thinking", "starting"):
                length = 3 + 5 * (1 + math.sin(i * 6 * math.pi / cfg.bars - t * 0.25))
            else:
                length = 2
            cos, sin = math.cos(a), math.sin(a)
            c.coords(item, m + inner * cos, y + inner * sin,
                     m + (inner + length) * cos, y + (inner + length) * sin)
            if recolor:
                c.itemconfig(item, fill=color)

        self.angle += cfg.spin[state] / cfg.fps
        paint = {"outline": color} if recolor else {}
        for item, start, speed in self.rings:
            c.itemconfig(item, start=(start + self.angle * speed) % 360, **paint)

        c.coords(self.core, m - core_r, y - core_r, m + core_r, y + core_r)
        c.itemconfig(self.core, fill=_mix(self.rgb, WHITE, 0.25 + 0.5 * lv), outline=color)

    def _draw_caption(self, state: str):
        shown = (state, self.you, self.reply)
        if shown == self._shown:
            return
        self._shown = shown
        you, reply = self.you, self.reply
        if state == "starting":
            you, reply = "", "Starting up..."
        elif state == "error":
            you = "Something went wrong (see logs/jarvis.log)"
        elif state == "listening" and not you and not reply:
            you = "Listening..."

        cfg, c, y = self.cfg, self.canvas, self.caption_y
        c.itemconfig(self.you_text, text=_clip(you, 90))
        c.coords(self.you_text, cfg.width / 2, y)
        if you:
            y = c.bbox(self.you_text)[3] + 4
        c.itemconfig(self.reply_text, text=_clip(reply, 160),
                     fill=cfg.colors["error"] if state == "error" else cfg.reply_color)
        c.coords(self.reply_text, cfg.width / 2, y)

        boxes = [c.bbox(item) for item, text in ((self.you_text, you), (self.reply_text, reply)) if text]
        if not boxes:
            c.itemconfig(self.panel, state="hidden")
            return
        x1, y1 = min(b[0] for b in boxes) - 14, min(b[1] for b in boxes) - 10
        x2, y2 = max(b[2] for b in boxes) + 14, max(b[3] for b in boxes) + 10
        c.coords(self.panel, *_rounded(x1, y1, x2, y2, 14))
        c.itemconfig(self.panel, state="normal")
