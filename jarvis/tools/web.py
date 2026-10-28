"""
Web searches: the results go in a window over the orb, like a search page, to read and click one of.
Jarvis doesn't read them out or talk them over: it is there to work the PC, not to chat.
"""
import logging
import webbrowser
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import quote_plus, urlsplit

from ..phrases import words
from ..ui.status import Status

log = logging.getLogger("jarvis")

GOOGLE = "https://www.google.com/search?q={}"
MAX_PAGES = 8    # web pages shown
MAX_NEWS = 4     # news stories shown above them, when the search is about news
# words that make a search about news, so the latest stories come first: 'what's happening in Milan today'.
# Only these: a news search matches loosely, so for anything else its stories would push good pages down
NEWS_WORDS = {"news", "headline", "headlines", "latest", "breaking", "today", "tonight", "yesterday", "happening"}


@dataclass(frozen=True)
class Result:
    title: str
    url: str
    snippet: str
    day: str = ""   # when a news story came out, like '28 October'

    @property
    def site(self) -> str:
        """Where it is, as a search page shows it: 'cararac.com', or 'msn.com, 28 October' for a story."""
        host = (urlsplit(self.url).hostname or self.url).removeprefix("www.")
        return f"{host}, {self.day}" if self.day else host


def _day(date: str) -> str:
    """'28 October' for '2026-10-28T16:58:55+00:00', with the year if it isn't this one."""
    try:
        when = datetime.fromisoformat(date).astimezone()
    except ValueError:
        return ""
    year = "" if when.year == datetime.now().year else f" {when.year}"
    return f"{when.day} {when:%B}{year}"


def _found(search: Future | None) -> list[dict]:
    """What one search found, or nothing if it failed (to ddgs, finding nothing is a failure too)."""
    try:
        return search.result() if search else []
    except Exception as e:
        log.info("[tool] web search failed: %s", e)
        return []


def _search(query: str, news: bool) -> list[Result]:
    """Web pages about query, after the latest news stories if news is True. Empty if the search
    engine can't be reached."""
    try:
        from ddgs import DDGS
    except ImportError:
        log.warning("Web search isn't installed (pip install ddgs), so it opens Google instead")
        return []
    with ThreadPoolExecutor(2) as pool:   # both at once
        pages = pool.submit(DDGS().text, query, max_results=MAX_PAGES)
        stories = pool.submit(DDGS().news, query, max_results=MAX_NEWS) if news else None
    results = [Result(r["title"], r["url"], r["body"], _day(r.get("date") or "")) for r in _found(stories)]
    results += [Result(r["title"], r["href"], r["body"]) for r in _found(pages)]
    seen = set()   # a story can also come up as a page
    return [r for r in results if r.url and not (r.url in seen or seen.add(r.url))]


class WebSearch:
    def __init__(self):
        self.screen = Status()   # where the results are shown; this one shows nothing
        self.request = ""        # what the user just said, set by Tools.begin
        # what Jarvis says itself about the last call, as (sentence, its values), or None for the model
        # to word the result, like a failure; set again by every call
        self.reply: tuple[str, dict] | None = None

    def search_web(self, query: str) -> str:
        """Search the web and show the results on screen. Use it for anything to look up online: facts, news, sports scores, how-tos.

        Args:
          query: What to search for
        """
        query = (query or "").strip()   # the model sometimes passes None
        if not query:
            return "Failed: say what to search for."
        google = GOOGLE.format(quote_plus(query))
        asked = set(words(f"{query} {self.request}").split())
        results = _search(query, news=bool(asked & NEWS_WORDS))
        # the results are on screen to read: the model would only read them out, or make up its own
        self.reply = ("Here's what I found on the web.", {})
        if results and self.screen.show_web_results(
                f"Web results for {query}", [(r.title, r.site, r.snippet) for r in results],
                on_open=lambda i: webbrowser.open(results[i].url), on_more=lambda: webbrowser.open(google)):
            return f"Showed {len(results)} web results for {query} on screen."
        webbrowser.open(google)   # no window to show them in (text mode, --no-ui), or nothing found
        return f"Opened a Google search for {query} in the browser."
