from collections.abc import Callable

STATES = ("starting", "sleeping", "listening", "thinking", "speaking", "error")


class Status:
    """What the voice loop reports to. This base does nothing, so running with --no-ui needs no checks."""
    mic_loudness = 0.0  # mean int16 loudness of the latest microphone chunk
    voice_level = 0.0   # 0..1, how loud Jarvis's own voice is right now
    can_choose = False  # whether choose() shows a list, so a search may find many files
    choosing = False    # a list is up for the user to pick from, so Jarvis doesn't go back to sleep

    def set_state(self, state: str) -> None: ...
    def heard(self, text: str) -> None: ...
    def replied(self, text: str) -> None: ...
    def close(self) -> None: ...

    def choose(self, title: str, items: list[tuple[str, str]], on_pick: Callable[[int], None]) -> bool:
        """Show a list to click one of: items are (name, detail) lines, and on_pick(index) runs for the
        one clicked. False if there's no window to show it in, so Jarvis says the names instead."""
        return False

    def close_choices(self) -> None:
        """Take the list away, like when the user picked by voice instead."""
