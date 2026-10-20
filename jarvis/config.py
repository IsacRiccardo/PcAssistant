"""config/config.json loaded into typed objects, then passed to whatever needs it."""
import json
import shutil
from dataclasses import dataclass, fields
from pathlib import Path

from . import ROOT
from .phrases import words

DEFAULTS = ROOT / "config" / "defaults"   # factory settings, copied in when a settings file is missing
WAKE_WORDS = ("hey_jarvis", "alexa", "hey_mycroft", "hey_rhasspy")   # openWakeWord's built-in wake words


def read_settings(path: Path, default: str) -> dict:
    """A settings file as a dict, created from config/defaults/<default> first if it doesn't exist yet."""
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(DEFAULTS / default, path)
    return json.loads(path.read_text(encoding="utf-8"))


def _wake_words(value: str | list[str], name: str) -> tuple[str, ...]:
    """wake_word as a tuple: one wake word or a list of them, each a built-in name like hey_jarvis
    or an openWakeWord model file (.onnx, relative to the project folder)."""
    found = []
    for w in [value] if isinstance(value, str) else value:
        if w.endswith(".onnx"):
            model = ROOT / w
            if not model.is_file():
                raise SystemExit(f"{name}: the wake word model {model} doesn't exist")
            found.append(str(model))
        elif w in WAKE_WORDS:
            found.append(w)
        else:   # openWakeWord only hears phrases it has a trained model for
            raise SystemExit(f'{name}: there is no wake word "{w}". Use {", ".join(WAKE_WORDS)}, '
                             "or the path to an openWakeWord model (.onnx)")
    if not found:
        raise SystemExit(f"{name}: wake_word needs at least one wake word")
    return tuple(found)


@dataclass(frozen=True)
class Browser:
    command: str      # what 'start' runs, e.g. 'firefox' or 'msedge'
    args: list[str]   # flags placed before the url, e.g. ['-new-tab']
    new_tab: str      # page to open for a blank new tab


@dataclass(frozen=True)
class Config:
    # who Jarvis is
    user_name: str
    user_title: str                 # what Jarvis calls the user instead of their name ('sir', 'miss'); empty: the name
    personality: str
    greeting: str
    farewell: str
    # the language model
    model_plugged_in: str
    model_on_battery: str
    context_size: int
    keep_alive: str
    history_turns: int
    temperature: float
    max_tool_rounds: int
    # hearing
    wake_word: tuple[str, ...]      # built-in names or paths to .onnx models; any of them wakes Jarvis
    wake_threshold: float
    wake_reply: tuple[str, ...]     # said when the wake word is heard (one at random); empty: the wake beep
    whisper_model: str
    languages: tuple[str, ...]      # what the user speaks, e.g. ('en', 'de'); empty: any language
    silence_level: float
    silence_seconds: float
    no_speech_seconds: float
    follow_up_seconds: float
    max_command_seconds: float
    wake_beep: tuple[int, int]      # (Hz, ms)
    sleep_beep: tuple[int, int]
    sleep_reply: tuple[str, ...]    # said when Jarvis goes back to sleep (one at random); empty: the sleep beep
    beep_grace_seconds: float
    sleep_phrases: tuple[str, ...]
    shutdown_phrases: tuple[str, ...]
    # speaking: one voice per language; the first also says the greeting and farewell
    piper_voices: tuple[str, ...]
    # files, relative to the project folder
    memory_file: Path
    log_dir: Path
    ui_config: Path
    # what the tools know about (apps aren't here: Jarvis finds them in the Start menu)
    sites: dict[str, str]
    contacts: dict[str, str]
    browsers: dict[str, Browser]
    tool_words: dict[str, tuple[str, ...]]   # tool name -> words that offer it to the model

    @classmethod
    def load(cls, path: Path) -> "Config":
        return cls.parse(read_settings(path, "config.json"), path.name)

    @classmethod
    def parse(cls, data: dict, name: str = "config.json") -> "Config":
        names = {f.name for f in fields(cls)}
        missing = names - data.keys()
        if missing:
            raise SystemExit(f"{name} is missing: {', '.join(sorted(missing))}")
        data = {k: v for k, v in data.items() if k in names}
        for key in ("memory_file", "log_dir", "ui_config"):
            data[key] = ROOT / data[key]
        data["wake_word"] = _wake_words(data["wake_word"], name)
        for beep in ("wake_beep", "sleep_beep"):
            data[beep] = (int(data[beep]["hz"]), int(data[beep]["ms"]))
        for key in ("sites", "contacts"):
            data[key] = {k.lower(): v for k, v in data[key].items()}
        data["browsers"] = {k.lower(): Browser(**v) for k, v in data["browsers"].items()}
        for key in ("sleep_phrases", "shutdown_phrases"):
            data[key] = tuple(words(w) for w in data[key])
        data["tool_words"] = {k: tuple(words(w) for w in v) for k, v in data["tool_words"].items()}
        data["languages"] = tuple(lang.strip().lower() for lang in data["languages"])
        data["piper_voices"] = tuple(data["piper_voices"])
        for key in ("wake_reply", "sleep_reply"):
            data[key] = tuple(r.strip() for r in data[key] if r.strip())
        return cls(**data)
