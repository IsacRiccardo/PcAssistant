"""A simple JSON list of facts Jarvis keeps about the user."""
import json
from pathlib import Path


class Memory:
    def __init__(self, path: Path):
        self.path = path
        try:   # read once; every request builds the system prompt from these
            self._facts = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            self._facts = []

    def facts(self) -> list[str]:
        return list(self._facts)

    def add(self, fact: str) -> None:
        self._facts.append(fact)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._facts, indent=2, ensure_ascii=False), encoding="utf-8")
