"""Matching what the user said against short command phrases."""
import re
import unicodedata
from pathlib import PurePath


def words(text: str) -> str:
    """Lowercase words only, without accents, so 'Shut down.' and 'shut down' compare equal, and so do
    'Café' and 'cafe' (speech recognition doesn't always write accents the same way)."""
    text = "".join(c for c in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^\w\s']", " ", text).split())


def wake_phrase(wake_word: str) -> str:
    """What to say for a wake word: 'hey_jarvis', or a model file like
    'wake_words/hey_computer_v0.1.onnx', is 'hey computer'."""
    name = re.sub(r"_v\d+(\.\d+)*$", "", PurePath(wake_word).name.removesuffix(".onnx"))
    return name.replace("_", " ")


def said(text: str, phrases: tuple[str, ...]) -> bool:
    """True if text is one of the phrases as a short command on its own, not part of a longer request.
    The phrases must already be in words() form."""
    w = words(text)
    return len(w.split()) <= 5 and any(f" {p} " in f" {w} " for p in phrases)
