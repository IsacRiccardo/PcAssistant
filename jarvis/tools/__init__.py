"""
Tools: functions the model can decide to call.
The docstrings matter - Ollama turns them into the tool descriptions.

To add a new ability: write a function (or a method, if it needs settings or state) with a
clear docstring in one of the modules here, then add it to the list in Tools.__init__ and give it
its words in tool_words in the config.
"""
import inspect
import logging

from ..config import Config
from ..memory import Memory
from ..phrases import words
from .email import EmailComposer
from .launcher import Launcher
from .lookup import Weather, battery_status, get_stock_price, get_time, web_lookup

log = logging.getLogger("jarvis")


def _clean_arguments(fn, args: dict) -> dict:
    """Small models sometimes copy the tool's own schema around the arguments, like
    {'type': 'object', 'properties': {'company': 'bitcoin'}}. Unwrap that and keep only what fn takes."""
    for _ in range(3):   # the wrappers can be nested: {'parameters': {'properties': {...}}}
        inner = next((args[k] for k in ("properties", "parameters", "arguments") if isinstance(args.get(k), dict)), None)
        if inner is None:
            break
        args = inner
    takes = inspect.signature(fn).parameters
    # a value that is itself a schema ({'type': 'string', ...}) means the model never filled it in
    return {k: v for k, v in args.items() if k in takes and not (isinstance(v, dict) and "type" in v)}


def _word_forms(phrase: str) -> set[str]:
    """The forms of a tool word a request may use, so a list needn't spell each one out: 'share' also
    matches 'shares', 'rain' 'raining', 'charge' 'charging', 'shares' 'share'. Only the last word of a
    phrase changes ('dow jones'). Endings come off only if 4 letters stay, so 'news' never matches 'new'."""
    *start, last = phrase.split()
    forms = {last, *(last + ending for ending in ("s", "es", "'s", "d", "ed", "ing"))}
    if last.endswith("e"):
        forms.add(last[:-1] + "ing")
    for ending in ("s", "es"):
        if last.endswith(ending) and len(last) - len(ending) >= 4:
            forms.add(last[:-len(ending)])
    return {" ".join([*start, form]) for form in forms}


class Tools:
    def __init__(self, cfg: Config, memory: Memory):
        self.cfg = cfg
        self.memory = memory
        self.launcher = Launcher(cfg)
        self.email = EmailComposer(cfg)
        self.weather = Weather()
        self.functions = [get_time, self.launcher.open_app, self.launcher.close_app, self.launcher.open_website,
                          self.launcher.search_web, self.email.compose_email, self.weather.get_weather,
                          get_stock_price, web_lookup, battery_status, self.remember]
        self.by_name = {f.__name__: f for f in self.functions}
        missing = self.by_name.keys() - cfg.tool_words.keys()
        if missing:
            raise SystemExit(f"config.json: 'tool_words' is missing: {', '.join(sorted(missing))}")
        # every form of every tool word, padded with spaces so they only match whole words
        self.triggers = {name: {f" {form} " for word in tool_words for form in _word_forms(word)}
                         for name, tool_words in cfg.tool_words.items()}

    def offered(self, text: str) -> list | None:
        """The tools whose words the request contains, or None for small talk, which stays fast that way.
        Fewer tools to choose from also means a small model picks the right one more often."""
        text = f" {words(text)} "   # the tool words are in this form too, so accents never matter
        found = [f for f in self.functions if any(trigger in text for trigger in self.triggers[f.__name__])]
        return found or None

    def fetch_ahead(self, request: str, previous: list | None) -> list[tuple[str, dict]]:
        """Calls to make before the model answers. A follow-up that only changes the day, like 'and
        tomorrow?' right after the weather, gets that day for the same city: small models answer those
        from the last result instead, and make the numbers up. previous: the last request's tools."""
        if previous and self.weather.get_weather in previous:
            also = {w for w in self.cfg.tool_words["get_weather"] if " " not in w}   # 'will it rain on friday?'
            args = self.weather.follow_up_args(request, also)
            if args:
                return [("get_weather", args)]
        return []

    def begin(self, request: str) -> None:
        """A new request: tools can check the model's arguments against what the user actually said."""
        self.launcher.request = self.weather.request = request

    def call(self, name: str, args: dict) -> str:
        fn = self.by_name.get(name)
        if fn is None:
            result = f"Failed: there is no tool called {name}. Use one of: {', '.join(self.by_name)}."
        else:
            try:
                result = fn(**_clean_arguments(fn, args))
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
