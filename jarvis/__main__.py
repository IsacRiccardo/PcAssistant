"""
Run:   python -m jarvis          (voice mode with the animated window, say "hey jarvis")
       python -m jarvis --no-ui  (voice mode, terminal only)
       python -m jarvis --text   (type instead of talk, good for testing)
Add --debug to any of them to write everything to logs/jarvis.log.
"""
import argparse
import logging
import os
import sys
import threading
import warnings
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import ollama

from . import ROOT
from .brain import Brain
from .config import Config
from .memory import Memory
from .phrases import said
from .tools import Tools

log = logging.getLogger("jarvis")


def setup_logging(log_dir: Path, debug: bool) -> None:
    """Warnings and errors always show on the terminal. With debug, everything (tool calls, status,
    the full conversation) also goes to the log file; without it nothing is written to disk."""
    to_terminal = logging.StreamHandler()
    to_terminal.setLevel(logging.WARNING)
    to_terminal.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    to_terminal.addFilter(lambda record: record.name != "py.warnings")   # library warnings stay off the terminal
    handlers = [to_terminal]
    if debug:
        log_dir.mkdir(parents=True, exist_ok=True)
        to_file = logging.FileHandler(log_dir / "jarvis.log", mode="w", encoding="utf-8")   # fresh log each launch
        to_file.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        handlers.append(to_file)
        logging.captureWarnings(True)                     # library warnings -> log file
    else:
        warnings.simplefilter("ignore")                   # library warnings would only clutter the terminal
    logging.basicConfig(level=logging.INFO, handlers=handlers)
    for noisy in ("httpx", "httpcore"):                   # skip one line per Ollama request
        logging.getLogger(noisy).setLevel(logging.WARNING)


def check_ollama() -> None:
    try:
        ollama.list()
    except Exception:
        log.error("Can't reach Ollama. Open the Ollama app (or run 'ollama serve') and try again.")
        sys.exit(1)


def run_text(cfg: Config, brain: Brain) -> None:
    log.info("Text mode started")
    while True:
        try:
            text = input("\nYou: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if text.lower() in ("exit", "quit") or (not brain.dictating and said(text, cfg.shutdown_phrases)):
            print(f"Jarvis: {cfg.farewell}")
            break
        if text:
            log.info("You: %s", text)
            print("Jarvis:", end="", flush=True)
            reply = []
            for sentence in brain.reply(text):   # print each sentence as soon as it's written
                reply.append(sentence)
                print(f" {sentence}", end="", flush=True)
            print()
            log.info("Jarvis: %s", " ".join(reply))


def make_hud(cfg: Config):
    from .ui.config import UiConfig
    try:
        from .ui.hud import Hud
    except ImportError:
        raise SystemExit("The orb window needs Qt: run the setup task again (or 'pip install PySide6-Essentials'), "
                         "or start Jarvis with --no-ui.") from None
    return Hud(UiConfig.load(cfg.ui_config))


def run_voice(cfg: Config, brain: Brain, no_ui: bool) -> None:
    from .ui import Status

    status = Status() if no_ui else make_hud(cfg)

    def voice():
        from .voice import Ears, Mouth

        mouth = Mouth(cfg, status)
        with ThreadPoolExecutor(1) as pool:
            # speech models load while the greeting plays (loading both at once only competes for the CPU)
            ears = pool.submit(Ears, cfg, status)
            mouth.say(cfg.greeting)
            ears = ears.result()
        ears.listen_until_shutdown(lambda text, language: mouth.say(brain.reply(text, language), language),
                                   on_sleep=brain.end_conversation,
                                   dictating=lambda: brain.dictating)
        mouth.say(cfg.farewell)
        status.close()

    if no_ui:
        try:
            voice()
        except KeyboardInterrupt:
            log.info("Stopped by user")
        return

    status.run(voice)   # voice loop on a background thread, window on this one
    log.info("Window closed")
    logging.shutdown()
    os._exit(0)         # the voice thread may be blocked on the microphone; don't wait for it


def main():
    parser = argparse.ArgumentParser(prog="jarvis", description="Jarvis local assistant")
    parser.add_argument("--text", action="store_true", help="type instead of using the microphone")
    parser.add_argument("--no-ui", action="store_true", help="voice mode without the animated window")
    parser.add_argument("--config", type=Path, default=ROOT / "config" / "config.json",
                        help="settings file (default: config/config.json)")
    parser.add_argument("--debug", action="store_true", help="write a detailed log to the log folder")
    args = parser.parse_args()
    # a terminal shows accented letters anyway, but redirected output would fall back to a code page without them
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    cfg = Config.load(args.config)
    setup_logging(cfg.log_dir, args.debug)
    check_ollama()

    memory = Memory(cfg.memory_file)
    brain = Brain(cfg, Tools(cfg, memory), memory)
    log.info("Model: %s", brain.model())
    # loading the model takes seconds; do it while everything else starts up
    threading.Thread(target=brain.warm_up, daemon=True).start()

    if args.text:
        run_text(cfg, brain)
    else:
        run_voice(cfg, brain, args.no_ui)


if __name__ == "__main__":
    main()
