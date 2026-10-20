"""config/ui.json loaded into typed objects."""
from dataclasses import dataclass, fields
from pathlib import Path

from ..config import read_settings
from .status import STATES


@dataclass(frozen=True)
class Ring:
    radius: float
    start: float     # starting angle, degrees
    length: float    # arc length, degrees
    width: float     # line width
    speed: float     # multiplier on the state's spin; negative spins backwards


@dataclass(frozen=True)
class UiConfig:
    fps: int
    width: int
    height: int
    orb_y: int             # orb centre, from the top of the overlay
    margin: int            # gap to the taskbar
    opacity: float         # overall opacity while visible
    fade_seconds: float    # how long appearing takes; disappearing is a little quicker
    bounce: float          # 0..1, how much the orb and caption overshoot as they spring into place
    mic_full_scale: float  # mic loudness (mean int16) that fills the sound ring
    bars: int              # bars in the sound ring around the core
    rings: tuple[Ring, ...]
    colors: dict[str, str]   # orb colour per state
    spin: dict[str, float]   # ring speed per state, degrees per second
    font: str
    heard_font_size: int
    reply_font_size: int
    heard_color: str       # caption line with what you said
    reply_color: str       # caption line with Jarvis's reply
    panel_color: str       # caption bubble

    @classmethod
    def load(cls, path: Path) -> "UiConfig":
        return cls.parse(read_settings(path, "ui.json"), path.name)

    @classmethod
    def parse(cls, data: dict, name: str = "ui.json") -> "UiConfig":
        names = {f.name for f in fields(cls)}
        missing = names - data.keys()
        if missing:
            raise SystemExit(f"{name} is missing: {', '.join(sorted(missing))}")
        data = {k: v for k, v in data.items() if k in names}
        for key in ("colors", "spin"):
            missing = set(STATES) - data[key].keys()
            if missing:
                raise SystemExit(f"{name}: '{key}' is missing: {', '.join(sorted(missing))}")
        data["rings"] = tuple(Ring(**r) for r in data["rings"])
        return cls(**data)
