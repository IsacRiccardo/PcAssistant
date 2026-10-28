from collections.abc import Callable
from dataclasses import dataclass

STATES = ("starting", "sleeping", "listening", "thinking", "speaking", "error")


@dataclass(frozen=True)
class Card:
    """Facts to show above the orb, like the weather or a stock price: a big figure with notes beside
    it, then a line chart, a table and a link to more."""
    title: str
    big: str                               # the main figure, like '12°' or '345.48 USD'
    notes: tuple[str, ...]                 # lines beside it, like ('Partly cloudy', 'Feels like 10°')
    rows: tuple[tuple[str, ...], ...] = ()   # the table, like ('Friday', 'Light rain', '60% rain', '8° / 15°')
    marked: tuple[int, ...] = ()           # rows to make stand out, like the days asked about
    chart: tuple[tuple[float, float], ...] = ()   # points to draw a line through: (0 to 1 across, value)
    baseline: float | None = None          # a dotted line across the chart, like yesterday's close
    trend: int = 0                         # 1 or -1: the first note and the chart go green or red
    link: str = ""                         # what the link at the bottom says, like 'See it on Google'
    on_link: Callable[[], None] | None = None


class Status:
    """What the voice loop reports to. This base does nothing, so running with --no-ui needs no checks."""
    mic_loudness = 0.0  # mean int16 loudness of the latest microphone chunk
    voice_level = 0.0   # 0..1, how loud Jarvis's own voice is right now
    can_choose = False  # whether choose() shows a list, so a search may find many files
    # something is up above the orb (a list, web results, a card) until the user closes it or picks
    # from it, so Jarvis doesn't go back to sleep
    popup_open = False

    def set_state(self, state: str) -> None: ...
    def heard(self, text: str) -> None: ...
    def replied(self, text: str) -> None: ...
    def close(self) -> None: ...

    def choose(self, title: str, items: list[tuple[str, str]], on_pick: Callable[[int], None]) -> bool:
        """Show a list to click one of: items are (name, detail) lines, and on_pick(index) runs for the
        one clicked. False if there's no window to show it in, so Jarvis says the names instead."""
        return False

    def show_web_results(self, title: str, results: list[tuple[str, str, str]], on_open: Callable[[int], None],
                         on_more: Callable[[], None]) -> bool:
        """Show a web search's results, like a search page: results are (title, site, snippet), on_open(index)
        runs for the one clicked and on_more() for the link to the whole search. Like a list of files,
        Jarvis stays awake while the user reads them. False if there's no window to show them in."""
        return False

    def show_card(self, card: Card) -> bool:
        """Show facts like the weather or a stock price, until the user closes them; Jarvis stays awake
        till then. False if there's no window to show them in."""
        return False

    def close_choices(self) -> None:
        """Take away what's shown above the orb, like a list when the user picked by voice instead."""
