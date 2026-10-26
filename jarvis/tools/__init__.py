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
from .files import FileSearch
from .launcher import Launcher
from .lookup import Weather, battery_status, get_stock_price, get_time, web_lookup
from .system import Power, level_asked, set_brightness, set_volume

log = logging.getLogger("jarvis")

# What Jarvis says itself when one of these worked, since the model would go on about it:
# tool -> (how its result starts when it worked, the reply). The file tools set their own (FileSearch.reply).
DONE_REPLIES = {
    "open_app": ("Opened ", "It's open."),
    "open_website": ("Opened ", "It's open."),
}


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
        self.files = FileSearch()
        self.launcher = Launcher(cfg, self.files.open_document)
        self.email = EmailComposer(cfg)
        self.weather = Weather()
        self.power = Power()
        self.functions = [get_time, self.launcher.open_app, self.launcher.close_app, self.launcher.open_website,
                          self.launcher.search_web, self.email.compose_email, self.weather.get_weather,
                          get_stock_price, web_lookup, battery_status, self.remember,
                          set_volume, set_brightness, self.power.computer_power,
                          self.files.find_files, self.files.open_file, self.files.show_in_folder]
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

    def direct_calls(self, request: str, offered: list | None, previous: list | None) -> list[tuple[str, dict]]:
        """Calls Jarvis makes itself, before the model, because the request says plainly what to do and
        small models get these wrong: they claim a change they never made, pick the wrong level, or
        answer from the last result with made-up numbers. The model then only words the answer.
        offered: the tools offered for this request; previous: the last request's tools."""
        # stopping a shutdown that is counting down, whatever was said before
        if self.power.cancel_asked(request):
            return [("computer_power", {"action": "cancel"})]
        # 'and tomorrow?' right after the weather: that day, for the same city
        if previous and self.weather.get_weather in previous:
            also = {w for w in self.cfg.tool_words["get_weather"] if " " not in w}   # 'will it rain on friday?'
            args = self.weather.follow_up_args(request, also)
            if args:
                return [("get_weather", args)]
        # 'open the second one' or 'where is it?' right after a file search
        pick = self.files.follow_up(request)
        if pick:
            return [pick]
        # 'find my resume', 'open my resume': small models answer that they can't, or call open_app
        # only for apps, so a plain file request is carried out without them
        find = self.files.asked_to_find(request)
        if find:
            return [("find_files", find)]
        said = self.files.asked_to_open(request, set(self.cfg.browsers))   # 'a png from my downloads'
        args = self.files.open_args(said) if said and not self.launcher.knows(said) else None
        if args:
            return [("open_file", args)]
        # 'set the volume to 30', 'a bit more', 'mute', 'make the screen brighter'
        offered = offered or []
        if (set_volume in offered) != (set_brightness in offered):   # one of the two, so 'up' is clear
            tool = set_volume if set_volume in offered else set_brightness
            level = level_asked(request, can_mute=tool is set_volume)
            # 'what's the volume?' reads it: it may have been changed by hand since Jarvis last set it.
            # Only when the request itself is about it, so a 'thanks' afterwards isn't.
            if not level and tool in (self.offered(request) or []):
                level = "check"
            if level:
                return [(tool.__name__, {"level": level})]
        return []

    def begin(self, request: str) -> None:
        """A new request: tools can check the model's arguments against what the user actually said."""
        self.launcher.request = self.weather.request = self.power.request = request

    def call(self, name: str, args: dict) -> str:
        fn = self.by_name.get(name)
        self.files.reply = None   # only this call's counts
        if fn is None:
            result = f"Failed: there is no tool called {name}. Use one of: {', '.join(self.by_name)}."
        else:
            try:
                result = fn(**_clean_arguments(fn, args))
            except Exception as e:
                result = f"Failed: {e}"
        log.info("[tool] %s(%s) -> %s", name, args, result)
        return str(result)

    def done_reply(self, results: list[tuple[str, str]]) -> tuple[str, dict] | None:
        """What Jarvis says itself after these (tool name, result) pairs, as (sentence, its values):
        'It's open.', or the files it found. None for the model to word them, like when one failed."""
        if len(results) == 1 and self.files.reply:   # a file tool, or open_app that opened a file
            return self.files.reply
        worked = all(name in DONE_REPLIES and result.startswith(DONE_REPLIES[name][0]) for name, result in results)
        replies = {DONE_REPLIES[name][1] for name, _ in results if name in DONE_REPLIES}
        return (replies.pop(), {}) if results and worked and len(replies) == 1 else None

    def remember(self, fact: str) -> str:
        """Save a fact about the user to long-term memory, when the user asks you to remember something.

        Args:
          fact: The fact to remember, written as a short sentence
        """
        self.memory.add(fact)
        return "Saved to memory."
