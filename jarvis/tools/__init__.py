"""
Tools: functions the model can decide to call.
The docstrings matter - Ollama turns them into the tool descriptions.

To add a new ability: write a function (or a method, if it needs settings or state) with a
clear docstring in one of the modules here, then add it to the list in Tools.__init__.
"""
import logging

from ..config import Config
from ..memory import Memory
from ..phrases import words
from .email import EmailComposer
from .launcher import Launcher
from .lookup import battery_status, get_stock_price, get_time, web_lookup

log = logging.getLogger("jarvis")


class Tools:
    def __init__(self, cfg: Config, memory: Memory):
        self.cfg = cfg
        self.memory = memory
        self.launcher = Launcher(cfg)
        self.email = EmailComposer(cfg)
        self.all = [get_time, self.launcher.open_app, self.launcher.close_app, self.launcher.open_website,
                    self.launcher.search_web, self.email.compose_email, get_stock_price,
                    web_lookup, battery_status, self.remember]
        self.by_name = {f.__name__: f for f in self.all}

    def wanted(self, text: str) -> bool:
        """Only offer tools when the request sounds like an action, so small talk stays fast."""
        text = words(text)   # the action words are in this form too, so accents never matter
        return any(w in text for w in self.cfg.action_words)

    def begin(self, request: str) -> None:
        """A new request: tools can check the model's arguments against what the user actually said."""
        self.launcher.request = request

    def call(self, name: str, args: dict) -> str:
        fn = self.by_name.get(name)
        try:
            result = fn(**args) if fn else f"Failed: there is no tool called {name}. Use one of: {', '.join(self.by_name)}."
        except Exception as e:
            result = f"Failed: {e}"
        log.info("[tool] %s(%s) -> %s", name, args, result)
        return str(result)

    def remember(self, fact: str) -> str:
        """Save a fact about the user to long-term memory, when the user asks you to remember something.

        Args:
          fact: The fact to remember, written as a short sentence
        """
        self.memory.add(fact)
        return "Saved to memory."
