"""
How the orb moves; ui.hud draws it.
The voice loop runs on a worker thread and just sets attributes here; the window calls step()
`fps` times a second on its own thread and draws what it leaves in the attributes.
"""
import math
import random

from .config import UiConfig
from .status import Status

BASE_FPS = 30           # frame rate the motion constants were tuned at
CAPTION_GAP = 17        # space between the outermost ring and the caption
WHITE = [255, 255, 255]


def rgb(color: str) -> list[float]:
    return [int(color[i:i + 2], 16) for i in (1, 3, 5)]


def to_hex(rgb: list[float]) -> str:
    return "#%02x%02x%02x" % tuple(round(v) for v in rgb)


def mix(a: list[float], b: list[float], t: float) -> str:
    """Blend two rgb colours into a colour string: t=0 gives a, t=1 gives b."""
    return to_hex([x + (y - x) * t for x, y in zip(a, b)])


def clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


class Orb(Status):
    def __init__(self, cfg: UiConfig):
        self.cfg = cfg
        self.state = "starting"
        self.you = self.reply = ""
        self.closing = False
        self.alpha = 0.0                     # window opacity
        self.level = 0.0                     # 0..1, how big and lively the orb is
        self.angle = 0.0                     # ring rotation, degrees
        self.frame = 0
        self.rgb = rgb(cfg.colors["starting"])
        self.noise = [0.0] * cfg.bars
        self.bars: list[tuple[float, float]] = []   # (angle in radians, length) of each bar in the sound ring
        self.caption_y = cfg.orb_y + max((r.radius for r in cfg.rings), default=95) + CAPTION_GAP

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
        self.closing = True                  # the next frame closes the window on its own thread

    # Called by the window, once per frame
    @property
    def color(self) -> str:
        return to_hex(self.rgb)

    @property
    def core_color(self) -> str:
        return mix(self.rgb, WHITE, 0.25 + 0.5 * self.level)

    @property
    def core_radius(self) -> float:
        return 28 + 18 * self.level

    def ring_start(self, start: float, speed: float) -> float:
        return (start + self.angle * speed) % 360

    def _ease(self, k: float) -> float:
        """Per-frame smoothing amount k, tuned at BASE_FPS, adjusted so motion keeps its speed at any fps."""
        return 1 - (1 - k) ** (BASE_FPS / self.cfg.fps)

    def step(self) -> str:
        """Advance one frame: the fade, then (while visible) the orb's motion. Returns the state drawn."""
        cfg, state = self.cfg, self.state
        target = 0.0 if state == "sleeping" else cfg.opacity
        if self.alpha != target:
            fade = cfg.opacity / max(cfg.fade_seconds * cfg.fps, 1)   # opacity change per frame
            step = fade if target > self.alpha else -fade
            self.alpha = min(max(self.alpha + step, 0.0), cfg.opacity)
            if abs(self.alpha - target) < fade:
                self.alpha = target
        if self.alpha == 0.0:
            return state                     # hidden, nothing moves

        self.frame += 1
        t = self.frame * BASE_FPS / cfg.fps     # animation time, in BASE_FPS frames
        self.rgb = [a + (b - a) * self._ease(0.12)                       # fade between states
                    for a, b in zip(self.rgb, rgb(cfg.colors[state]))]
        if state == "listening":
            target = min(1.0, self.mic_loudness / cfg.mic_full_scale)
        elif state == "speaking":
            target = self.voice_level
        elif state in ("thinking", "starting"):
            target = 0.3 + 0.1 * math.sin(t * 0.3)
        else:
            target = 0.1 + 0.08 * math.sin(t * 0.06)   # slow breathing
        self.level += (target - self.level) * self._ease(0.3)

        noise, bars = self._ease(0.35), []
        for i in range(cfg.bars):
            a = 2 * math.pi * i / cfg.bars + math.radians(self.angle) * 0.3
            if state in ("listening", "speaking"):
                self.noise[i] += (random.random() - self.noise[i]) * noise
                length = 3 + 32 * self.level * self.noise[i]
            elif state in ("thinking", "starting"):
                length = 3 + 5 * (1 + math.sin(i * 6 * math.pi / cfg.bars - t * 0.25))
            else:
                length = 2
            bars.append((a, length))
        self.bars = bars
        self.angle += cfg.spin[state] / cfg.fps
        return state

    def caption(self, state: str) -> tuple[str, str]:
        """The two caption lines: what the user said, and the reply."""
        you, reply = self.you, self.reply
        if state == "starting":
            you, reply = "", "Starting up..."
        elif state == "error":
            you = "Something went wrong (see the terminal)"
        elif state == "listening" and not you and not reply:
            you = "Listening..."
        return clip(you, 90), clip(reply, 160)
