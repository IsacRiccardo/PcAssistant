"""The languages Jarvis speaks: their names for the model, and the few sentences Jarvis says word for word."""

NAMES = {"en": "English", "ro": "Romanian", "de": "German", "fr": "French", "es": "Spanish",
         "it": "Italian", "nl": "Dutch", "pt": "Portuguese", "pl": "Polish", "hu": "Hungarian"}

# What Jarvis says itself instead of the model, per language and keyed by the English, e.g.
# {"de": {"What should the email say?": "Was soll in der E-Mail stehen?"}}. A missing one is said in English.
TRANSLATIONS: dict[str, dict[str, str]] = {}


def name(language: str) -> str:
    """'German' for 'de', to tell the model which language to reply in."""
    return NAMES.get(language, f"the language with the code '{language}'")


def translate(language: str | None, sentence: str, **values) -> str:
    """One of Jarvis's own sentences in this language, with its {placeholders} filled in."""
    return TRANSLATIONS.get(language, {}).get(sentence, sentence).format(**values)


def of_voice(voice: str) -> str:
    """The language a Piper voice speaks, from its name: 'de' for 'de_DE-thorsten-medium'."""
    return voice.split("-")[0].split("_")[0].lower()
