"""Matching what the user said against short command phrases."""
import re
import unicodedata


def words(text: str) -> str:
    """Lowercase words only, without accents, so 'Shut down.' and 'shut down' compare equal, and so do
    'Oprește-te' and 'opreste te' (speech recognition doesn't always write Romanian accents the same way)."""
    text = "".join(c for c in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^\w\s']", " ", text).split())


def said(text: str, phrases: tuple[str, ...]) -> bool:
    """True if text is one of the phrases as a short command on its own, not part of a longer request.
    The phrases must already be in words() form."""
    w = words(text)
    return len(w.split()) <= 5 and any(f" {p} " in f" {w} " for p in phrases)
