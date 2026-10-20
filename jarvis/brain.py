"""
Brain: talks to the local model through Ollama and runs the tools it asks for.

How one request is answered (Brain.reply):
  1. Pick the tools to offer: the ones whose words the request contains (Tools.offered), or for a
     follow-up like "and microsoft?", the ones the last request used.
  2. Build the messages: the system prompt, the conversation so far, and the request.
  3. Ask the model. If it calls tools, run them, add their results and ask again, up to
     max_tool_rounds times. Its words are passed on sentence by sentence, so speaking starts early.
  4. Keep the turn, so the next request can refer back to it.
"""
import json
import logging
import re
from collections.abc import Generator, Iterator

import ollama
import psutil

from . import languages
from .config import Config
from .memory import Memory
from .tools import Tools

log = logging.getLogger("jarvis")

RULES = (
    "Your replies are spoken aloud, so keep them short (1-3 sentences), "
    "with no markdown, lists or emojis. Reply in the language the user speaks. Use a tool only when the user asks for an action "
    "or for live information.\n"
    "You have no internet access except through your tools. Never say you are looking "
    "something up unless you are calling a tool, and never guess prices, news or other "
    "live facts. If you can't get it, say so plainly. A tool result is live data fetched just "
    "now, so when one answers the question, give the user that answer.\n"
    "Only say you did something if a tool result confirms it. If a tool reports an error "
    "or that it doesn't know something, tell the user it didn't work. You can't see the "
    "screen, so don't describe what is on it."
)
NO_TOOLS = (
    "You have no tools for this message, so you can't perform any action right now. "
    "Never say you opened, did or will do something. If the user wants an action, "
    "ask them to say it directly, for example 'open gmail'."
)
SENTENCE_END = re.compile(r"(?<=[.!?])\s+")

ToolCall = tuple[str, dict]   # (tool name, arguments)


def _looks_like_json(content: str) -> bool:
    """Whether the model is writing JSON: a tool call typed out as text, never to be read aloud."""
    return content.lstrip().startswith("{")


def _text_tool_calls(content: str | None) -> list[ToolCall]:
    """Small models sometimes write a tool call as plain JSON text instead of making it,
    which would otherwise be read out loud. Turn that back into a real call."""
    try:
        data = json.loads((content or "").strip())
    except json.JSONDecodeError:
        return []
    if not isinstance(data, dict) or not isinstance(data.get("name"), str):
        return []
    args = data.get("parameters", data.get("arguments")) or {}
    return [(data["name"], args if isinstance(args, dict) else {})]


class Sentences:
    """Collects streamed text and hands back each sentence as soon as it is complete."""

    def __init__(self):
        self.buf = ""

    def feed(self, text: str) -> list[str]:
        self.buf += text
        *done, self.buf = SENTENCE_END.split(self.buf)
        return [s.strip() for s in done if s.strip()]

    def flush(self) -> list[str]:
        rest, self.buf = self.buf.strip(), ""
        return [rest] if rest else []


class Brain:
    def __init__(self, cfg: Config, tools: Tools, memory: Memory):
        self.cfg = cfg
        self.tools = tools
        self.memory = memory
        self.turns = []           # each: the request, its tool calls and their results, the reply
        self.tool_messages = []   # the tool calls and results of the request being answered
        self.follow_up_tools = None   # the last request's tools, if it used one: 'of apple?' after a stock price

    @property
    def dictating(self) -> bool:
        """True while the user is answering one of Jarvis's questions, like the email ones."""
        return self.tools.email.active

    def model(self) -> str:
        """Bigger model when plugged in, smaller one on battery."""
        battery = psutil.sensors_battery()
        plugged_in = battery is None or battery.power_plugged
        return self.cfg.model_plugged_in if plugged_in else self.cfg.model_on_battery

    def system_prompt(self) -> str:
        facts = "\n".join(f"- {fact}" for fact in self.memory.facts()) or "(nothing yet)"
        return (
            f"{self.cfg.personality}\n\n"
            f"{self._addressing()}"
            f"Things you remember about the user:\n{facts}\n\n"
            f"{RULES}\n"
            f"Site names open_website knows: {', '.join(self.cfg.sites)}."
        )

    def _addressing(self) -> str:
        """How to address the user. Given only their name, small models put it in nearly every reply."""
        name, title = self.cfg.user_name, self.cfg.user_title
        if title:
            known = f"The user's name is {name}, but don't call them by it. " if name else ""
            return f'{known}Address the user as "{title}", at most once in a reply.\n'
        if name:
            return f"The user's name is {name}. Use it only now and then, not in every reply.\n"
        return ""

    def _chat(self, messages: list, tools=None, **options):
        # the same context_size every call: Ollama reloads the model whenever it changes
        return ollama.chat(
            model=self.model(),
            messages=messages,
            tools=tools,
            stream=True,
            think=False,   # thinking models (qwen3) would reason for seconds before saying anything; others ignore it
            keep_alive=self.cfg.keep_alive,
            options={"temperature": self.cfg.temperature, "num_ctx": self.cfg.context_size, **options},
        )

    def warm_up(self) -> None:
        """Load the model and read the system prompt now, so the first request doesn't wait for
        either (loading alone takes several seconds)."""
        try:
            for _ in self._chat([{"role": "system", "content": self.system_prompt()}], num_predict=1):
                pass
            log.info("Model %s loaded", self.model())
        except Exception as e:
            log.warning("Couldn't warm up the model: %s", e)

    def end_conversation(self) -> None:
        """Jarvis went back to sleep: drop a half-written email so the next request doesn't land in it,
        and don't treat the next request as a follow-up."""
        self.tools.email.cancel()
        self.follow_up_tools = None

    def ask(self, text: str, language: str | None = None) -> str:
        """The whole reply at once."""
        return " ".join(self.reply(text, language))

    def reply(self, text: str, language: str | None = None) -> Iterator[str]:
        """The reply, one sentence at a time as the model writes it, so speaking can start early.
        language is the one the user spoke in ('de'), if known; the reply is in it too."""
        spoken = []
        self.tool_messages = []
        for sentence in self._reply(text, language):
            spoken.append(sentence)
            yield sentence
        self.turns.append([{"role": "user", "content": text}, *self.tool_messages,
                           {"role": "assistant", "content": " ".join(spoken)}])
        self.turns = self.turns[-self.cfg.history_turns:]

    def _history(self) -> list:
        """Earlier turns as what was asked and answered. The last one also keeps its tool calls and
        results: seeing only 'NVIDIA is at $216', small models answer 'and apple?' with a made-up
        price instead of calling the tool again. Only the last one, so the context stays small."""
        if not self.turns:
            return []
        return [message for turn in self.turns[:-1] for message in (turn[0], turn[-1])] + self.turns[-1]

    def _reply(self, text: str, language: str | None) -> Iterator[str]:
        if self.tools.email.active:   # answering one of the email questions
            yield self.tools.email.answer(text, language)
            return

        previous_tools, self.follow_up_tools = self.follow_up_tools, None
        # a request without tool words of its own may be a follow-up to the last one ('and microsoft?')
        tools = self.tools.offered(text) or previous_tools
        messages = self._messages(text, language, tools)
        self.tools.begin(text)
        fetched_ahead = self.tools.fetch_ahead(text, previous_tools)   # like 'and tomorrow?' after the weather
        if fetched_ahead:
            self._run_tools(messages, fetched_ahead, tools)

        for round_number in range(self.cfg.max_tool_rounds):
            # While the model may still decide to act, hold its words back until it's clear it isn't
            # calling a tool, so it never announces an action that didn't happen. Otherwise speak
            # each sentence as soon as it's written.
            speak_as_written = tools is None or round_number > 0
            calls, said_with_calls = yield from self._ask_model(messages, tools, speak_as_written)
            if not calls:
                return
            self._run_tools(messages, calls, tools, said_with_calls)
            if self.tools.email.active:   # an email was started: ask for what's missing, word for word
                yield self.tools.email.next_question(language)
                return

        yield languages.translate(language, "Sorry, I got a bit stuck on that one.")

    def _messages(self, text: str, language: str | None, tools: list | None) -> list:
        """The system prompt, the conversation so far, and the request. Notes for the model go next to
        the request, where small models actually heed them; the history keeps the request as said."""
        notes = [f"Reply in {languages.name(language)}."] if language else []
        if tools is None:
            notes.append(NO_TOOLS)
        request = f"{text}\n\n({' '.join(notes)})" if notes else text
        return ([{"role": "system", "content": self.system_prompt()}] + self._history()
                + [{"role": "user", "content": request}])

    def _ask_model(self, messages: list, tools: list | None,
                   speak_as_written: bool) -> Generator[str, None, tuple[list[ToolCall], str]]:
        """One round with the model. Yields its reply sentence by sentence (or all at once at the end,
        if held back), and returns (the tool calls it made, what it wrote alongside them). When it
        calls tools, nothing is yielded."""
        content, raw_calls, sentences = "", [], Sentences()
        for part in self._chat(messages, tools):
            raw_calls += part.message.tool_calls or []
            content += part.message.content or ""
            if speak_as_written and not raw_calls and not _looks_like_json(content):
                yield from sentences.feed(part.message.content or "")

        calls = [(c.function.name, c.function.arguments or {}) for c in raw_calls] or _text_tool_calls(content)
        if calls:
            return calls, content if raw_calls else ""   # a call typed out as JSON isn't kept as its words
        if speak_as_written and not _looks_like_json(content):
            yield from sentences.flush()
        elif content.strip():   # held back the whole time: say it all now
            yield content.strip()
        return [], content

    def _run_tools(self, messages: list, calls: list[ToolCall], tools: list, said_with_calls: str = "") -> None:
        """Run tool calls, adding them and their results to this request's messages and its history."""
        self.follow_up_tools = tools
        # a call typed out as JSON is recorded as a real one, so the history never shows that habit
        self.tool_messages.append(ollama.Message(
            role="assistant", content=said_with_calls,
            tool_calls=[ollama.Message.ToolCall(function=ollama.Message.ToolCall.Function(name=name, arguments=args))
                        for name, args in calls]))
        for name, args in calls:
            result = self.tools.call(name, args)
            self.tool_messages.append({"role": "tool", "content": result, "tool_name": name})
        messages += self.tool_messages[-1 - len(calls):]
