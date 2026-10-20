"""
How the orb moves; ui.hud draws it.
The voice loop runs on a worker thread and just sets attributes here; the window calls step()
`fps` times a second on its own thread and draws what it leaves in the attributes.

Appearing and disappearing are staged, the way Apple animates Siri and the Dynamic Island: the orb
springs in from smaller with a slight overshoot while the glow blooms, the sound bars grow out of
the core and the rings sweep in last; going to sleep, the rings unwind and the orb sinks away faster.
The settings page preview (settings/page.html) mirrors this, so keep the two in step.
"""
import math
import random

from .config import UiConfig
from .status import Status

BASE_FPS = 30           # frame rate the motion constants were tuned at
CAPTION_GAP = 17        # space between the outermost ring and the caption
WHITE = [255, 255, 255]
START_SCALE = 0.6       # the orb springs in from this size...
END_SCALE = 0.85        # ...and sinks to this one as it goes
OUT_SPEED = 0.7         # disappearing takes this share of fade_seconds
# when each part comes and goes: (delay, duration), as shares of the appear or disappear time
APPEARING = {"alpha": (0, 0.55), "bars": (0.15, 0.6), "sweep": (0.3, 0.7)}
DISAPPEARING = {"sweep": (0, 0.6), "bars": (0.1, 0.6), "alpha": (0.2, 0.8), "scale": (0, 1)}


def rgb(color: str) -> list[float]:
    return [int(color[i:i + 2], 16) for i in (1, 3, 5)]


def to_hex(rgb: list[float]) -> str:
    return "#%02x%02x%02x" % tuple(round(v) for v in rgb)


def mix(a: list[float], b: list[float], t: float) -> str:
    """Blend two rgb colours into a colour string: t=0 gives a, t=1 gives b."""
    return to_hex([x + (y - x) * t for x, y in zip(a, b)])


def clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


def ease_out(x: float) -> float:
    """Fast start, gentle landing: things arriving."""
    return 1 - (1 - x) ** 3


def ease_in(x: float) -> float:
    """Gentle start, speeding up: things leaving."""
    return x ** 3


def progress(t: float, delay: float, span: float) -> float:
    """0..1: how far a part's own animation is, t being the share of the whole animation done."""
    return 1.0 if span <= 0 else min(max((t - delay) / span, 0.0), 1.0)


class Spring:
    """A value pulled toward a target like a weight on a spring: with bounce it overshoots a little
    and settles, which is what makes motion feel physical rather than timed."""

    def __init__(self, value: float):
        self.value, self.velocity = value, 0.0

    def step(self, target: float, dt: float, seconds: float, bounce: float) -> float:
        """Move toward target. seconds is roughly how long settling takes; bounce 0 never overshoots."""
        if seconds <= 0.02:
            self.value, self.velocity = target, 0.0
            return target
        omega, zeta = 9 / seconds, max(0.2, 1 - min(max(bounce, 0.0), 1.0))   # zeta 1: no overshoot
        for _ in range(4):   # small steps keep it stable at any frame rate
            h = dt / 4
            self.velocity += (-omega * omega * (self.value - target) - 2 * zeta * omega * self.velocity) * h
            self.value += self.velocity * h
        return self.value


class Bubble:
    """The caption bubble's motion: it grows out of a small pill under the orb and springs to fit its
    text, resizing smoothly when the text changes, and each new line rises into place as it fades in.
    Where the text goes is up to the window, which measures it in its own fonts."""
    LINE_DELAYS = (0.08, 0.14)   # seconds before what you said, and the reply, start to show
    LINE_SECONDS = 0.3
    RISE = 6                     # pixels a new line rises as it fades in

    def __init__(self):
        self.reset()

    def reset(self) -> None:
        self.edges = None        # springs for the bubble's left, top, right and bottom
        self.rect = None
        self.alpha = 0.0
        self.clock = 0.0
        self.texts = ["", ""]
        self.born = [None, None]   # when each line last appeared, by self.clock

    def step(self, target: tuple | None, pill: tuple, texts: tuple[str, str], dt: float,
             seconds: float, bounce: float) -> None:
        """target: (left, top, right, bottom) of the bubble around the text, None without a caption.
        pill: the small bubble it grows out of and shrinks back into."""
        self.clock += dt
        for i, (old, text) in enumerate(zip(self.texts, texts)):
            growing = i == 1 and old and text.startswith(old.rstrip("…")[:20])   # the reply, sentence by sentence
            if not text:
                self.born[i] = None
            elif text != old and not growing:
                self.born[i] = self.clock
        self.texts = list(texts)
        if self.edges is None or (target and self.alpha < 0.02):
            self.edges = [Spring(v) for v in pill]
        self.rect = [s.step(g, dt, seconds, bounce) for s, g in zip(self.edges, target or pill)]
        self.alpha += ((1.0 if target else 0.0) - self.alpha) * (1 - math.exp(-dt / 0.08))

    def line(self, i: int) -> tuple[float, float]:
        """(opacity, how far below its place) for caption line i: 0 what you said, 1 the reply."""
        if self.born[i] is None:
            return 0.0, 0.0
        a = ease_out(min(max((self.clock - self.born[i] - self.LINE_DELAYS[i]) / self.LINE_SECONDS, 0.0), 1.0))
        return a, self.RISE * (1 - a)


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
        # appearing and disappearing: each part's presence, 0 (gone) to 1 (fully there)
        self.shown = False
        self.since = 0.0                     # seconds since the orb last started appearing or disappearing
        self.parts = {"alpha": 0.0, "bars": 0.0, "sweep": 0.0}
        self.from_parts, self.from_scale = dict(self.parts), START_SCALE
        self.scale_spring = Spring(START_SCALE)
        self.scale = START_SCALE             # whole orb size, around its centre
        self.bloom = 0.0                     # 0..1, extra glow while it arrives

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
        """Where a ring starts; while the rings sweep in or out they also turn into or out of place."""
        turn = 90 * (1 - self.parts["sweep"]) * (-1 if self.shown else 1)
        return (start + self.angle * speed + turn) % 360

    def ring_length(self, length: float) -> float:
        return length * self.parts["sweep"]

    def _ease(self, k: float) -> float:
        """Per-frame smoothing amount k, tuned at BASE_FPS, adjusted so motion keeps its speed at any fps."""
        return 1 - (1 - k) ** (BASE_FPS / self.cfg.fps)

    def _appear(self, shown: bool) -> None:
        """Advance appearing or disappearing by one frame. Each part follows its own stage in APPEARING or
        DISAPPEARING; done is the share of the whole animation that has passed."""
        cfg, dt = self.cfg, 1 / self.cfg.fps
        if shown != self.shown:              # start from wherever it is, so waking mid-fade-out is smooth
            self.shown, self.since = shown, 0.0
            self.from_parts, self.from_scale = dict(self.parts), self.scale
            if shown and self.parts["alpha"] == 0:
                self.scale_spring = Spring(START_SCALE)
        else:
            self.since += dt
        if shown:
            done = self.since / cfg.fade_seconds if cfg.fade_seconds > 0 else 1.0
            for part, (delay, span) in APPEARING.items():
                start = self.from_parts[part]
                self.parts[part] = start + (1 - start) * ease_out(progress(done, delay, span))
            self.scale = self.scale_spring.step(1.0, dt, cfg.fade_seconds, cfg.bounce)   # springs, with overshoot
            self.bloom = math.sin(math.pi * min(done / 0.9, 1.0)) * (1 - self.from_parts["alpha"])
        else:
            done = self.since / (cfg.fade_seconds * OUT_SPEED) if cfg.fade_seconds > 0 else 1.0
            for part, (delay, span) in DISAPPEARING.items():
                if part != "scale":
                    self.parts[part] = self.from_parts[part] * (1 - ease_in(progress(done, delay, span)))
            self.scale = self.from_scale + (END_SCALE - self.from_scale) * ease_in(progress(done, *DISAPPEARING["scale"]))
            self.scale_spring.value, self.scale_spring.velocity = self.scale, 0.0   # a wake-up springs from here
            self.bloom = 0.0
        self.alpha = cfg.opacity * self.parts["alpha"]

    def step(self) -> str:
        """Advance one frame: appearing or disappearing, then (while visible) the orb's motion.
        Returns the state drawn."""
        cfg, state = self.cfg, self.state
        self._appear(state != "sleeping")
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

        noise, bars, grown = self._ease(0.35), [], self.parts["bars"]
        for i in range(cfg.bars):
            a = 2 * math.pi * i / cfg.bars + math.radians(self.angle) * 0.3
            if state in ("listening", "speaking"):
                self.noise[i] += (random.random() - self.noise[i]) * noise
                length = 3 + 32 * self.level * self.noise[i]
            elif state in ("thinking", "starting"):
                length = 3 + 5 * (1 + math.sin(i * 6 * math.pi / cfg.bars - t * 0.25))
            else:
                length = 2
            bars.append((a, length * grown))
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
