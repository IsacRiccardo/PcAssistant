"""
Email: the model starts it, then Jarvis asks for what's missing and fills the answers in itself,
because a small model can't be trusted to carry several answers across turns.
"""
import logging
import re
import webbrowser
from urllib.parse import quote

from ..config import Config
from ..languages import translate
from ..phrases import words

log = logging.getLogger("jarvis")

GMAIL_COMPOSE = "https://mail.google.com/mail/?view=cm&fs=1"   # takes to, su (subject) and body
EMAIL_ADDRESS = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
QUESTIONS = {   # asked in this order for whatever the user hasn't said yet
    "to": "Who should the email go to?",
    "subject": "What's the subject? You can say skip.",
    "body": "What should the email say?",
}
CANCEL_PHRASES = {words(p) for p in ("cancel", "cancel it", "cancel the email", "forget it", "stop")}
SKIP_PHRASES = {words(p) for p in ("skip", "no subject", "none", "nothing", "no")}
SAID_AT = re.compile(r"\s+at\s+")   # 'john at gmail dot com'
SAID_DOT = re.compile(r"\s+dot\s+")


class EmailComposer:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.draft = None   # email being put together: {field: value} for the fields answered so far

    @property
    def active(self) -> bool:
        """True while Jarvis is asking the user for the rest of an email."""
        return self.draft is not None

    def cancel(self) -> None:
        self.draft = None

    def compose_email(self, to: str = "", subject: str = "", body: str = "") -> str:
        """Write or send a new email. Jarvis asks the user for anything missing, then opens it filled in, ready to send.

        Args:
          to: Only if the user said it: who the email goes to, an email address or a contact name
          subject: Only if the user said it: the subject line
          body: Only if the user said it: the message. Never make one up.
        """
        self.draft = {}
        address = self._address(to or "")
        if address:
            self.draft["to"] = address
        if (subject or "").strip():
            self.draft["subject"] = subject.strip().rstrip(".")
        if (body or "").strip():
            self.draft["body"] = body.strip()
        question = self.next_question()
        if question:
            return f"Started the email. Jarvis is asking the user: {question}"
        return self._open_draft()

    def next_question(self, language: str | None = None) -> str | None:
        """What to ask next for the email being written, or None once it's complete."""
        field = next((f for f in QUESTIONS if f not in self.draft), None)
        return translate(language, QUESTIONS[field]) if field else None

    def answer(self, text: str, language: str | None = None) -> str:
        """Fill the user's answer into the email being written. Returns what Jarvis says next,
        in the language the user answered in."""
        spoken = words(text)
        if spoken in CANCEL_PHRASES:
            self.draft = None
            return translate(language, "Okay, I've dropped the email.")
        field = next(f for f in QUESTIONS if f not in self.draft)
        value = text.strip()
        if field == "to":
            value = self._address(value)
            if not value:
                return translate(language, "I didn't catch an email address. Say it like john at gmail "
                                           "dot com, or say cancel.")
        elif field == "subject":
            value = "" if spoken in SKIP_PHRASES else value.rstrip(".")
        self.draft[field] = value
        log.info("[email] %s = %r", field, value)
        return self.next_question(language) or self._open_draft(language)

    def _address(self, spoken: str) -> str | None:
        """An email address from what the user said: a contact name, or an address, also spoken
        as 'john at gmail dot com'."""
        text = spoken.lower()
        for name, address in self.cfg.contacts.items():
            if re.search(rf"\b{re.escape(name)}\b", text):
                return address
        text = SAID_AT.sub("@", text)
        text = SAID_DOT.sub(".", text)
        text = re.sub(r"\s*@\s*", "@", text)
        match = EMAIL_ADDRESS.search(text)
        return match.group(0) if match else None

    def _open_draft(self, language: str | None = None) -> str:
        d, self.draft = self.draft, None
        webbrowser.open(f"{GMAIL_COMPOSE}&to={quote(d['to'])}&su={quote(d['subject'])}"
                        f"&body={quote(d['body'])}")
        log.info("[email] opened draft to %s", d["to"])
        return translate(language, "Your email to {to} is filled in and open in Gmail, but not sent yet. "
                                   "Check it and press Send.", to=d["to"])
