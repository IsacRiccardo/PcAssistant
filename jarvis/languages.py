"""The languages Jarvis speaks: their names for the model, and the few sentences Jarvis says word for word."""

NAMES = {"en": "English", "ro": "Romanian", "de": "German", "fr": "French", "es": "Spanish",
         "it": "Italian", "nl": "Dutch", "pt": "Portuguese", "pl": "Polish", "hu": "Hungarian"}

# What Jarvis says itself instead of the model, keyed by the English. A missing one is said in English.
TRANSLATIONS = {
    "ro": {
        "Who should the email go to?": "Cui să trimit emailul?",
        "What's the subject? You can say skip.": "Care e subiectul? Poți spune fără subiect.",
        "What should the email say?": "Ce să scrie în email?",
        "Okay, I've dropped the email.": "Bine, am renunțat la email.",
        "I didn't catch an email address. Say it like john at gmail dot com, or say cancel.":
            "N-am prins nicio adresă de email. Spune-o așa: ion arond gmail punct com, sau spune anulează.",
        "Your email to {to} is filled in and open in Gmail, but not sent yet. Check it and press Send.":
            "Emailul către {to} e completat și deschis în Gmail, dar nu e trimis încă. Verifică-l și apasă Trimite.",
        "Sorry, I got a bit stuck on that one.": "Scuze, m-am cam încurcat la asta.",
    },
}


def name(language: str) -> str:
    """'Romanian' for 'ro', to tell the model which language to reply in."""
    return NAMES.get(language, f"the language with the code '{language}'")


def translate(language: str | None, sentence: str, **values) -> str:
    """One of Jarvis's own sentences in this language, with its {placeholders} filled in."""
    return TRANSLATIONS.get(language, {}).get(sentence, sentence).format(**values)


def of_voice(voice: str) -> str:
    """The language a Piper voice speaks, from its name: 'ro' for 'ro_RO-mihai-medium'."""
    return voice.split("-")[0].split("_")[0].lower()
