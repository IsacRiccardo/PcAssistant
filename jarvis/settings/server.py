"""
Run:   python -m jarvis.settings                    (opens the settings page in the browser)
       python -m jarvis.settings --config other.json

A small web server on 127.0.0.1 that hands page.html the current settings, the factory defaults
and what is installed on this PC, then writes the files when you press Save. Every request
needs the token that was put into the page, so other websites can't change the settings.
It stops a few seconds after the page is closed, or with Ctrl+C.
"""
import argparse
import json
import os
import secrets
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .. import ROOT
from ..config import DEFAULTS, Config
from ..ui.config import UiConfig

PAGE = Path(__file__).with_name("page.html")
WAKE_WORDS = ("hey_jarvis", "alexa", "hey_mycroft", "hey_rhasspy")   # openWakeWord's built-in wake words
CLOSE_SECONDS = 3       # after the page is closed; a reload comes back well within this
MIC_IDLE_SECONDS = 2    # the microphone closes once the page stops asking for its level


def _show(path: Path) -> str:
    """A path as the page shows it: relative to the project folder when it's inside it."""
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def _read(path: Path) -> tuple[dict | None, str | None]:
    """(settings, problem): settings is None when the file is missing or isn't valid JSON."""
    if not path.exists():
        return None, None
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        return None, f"{_show(path)} couldn't be read ({e}). The defaults are shown; saving replaces it."


def _ollama_models() -> list[str] | None:
    """Models already downloaded in Ollama, or None if Ollama isn't running."""
    try:
        import ollama
        return sorted(m.model for m in ollama.list().models)
    except Exception:
        return None


def _write(path: Path, text: str) -> None:
    """Write via a temporary file, so a crash halfway never leaves Jarvis a broken settings file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text.rstrip() + "\n", encoding="utf-8")
    os.replace(tmp, path)


class Mic:
    """Microphone loudness in the units silence_level and mic_full_scale use: the mean of one
    80 ms chunk of int16 samples, recorded the same way Jarvis's ears do."""

    def __init__(self):
        self.lock = threading.Lock()
        self.stream = None
        self.level = 0.0
        self.asked = 0.0

    def read(self) -> float:
        with self.lock:
            self.asked = time.monotonic()
            if self.stream is None:
                import numpy as np
                import sounddevice as sd

                from ..voice.ears import CHUNK, SAMPLE_RATE

                def callback(indata, frames, time_info, status):
                    self.level = float(np.abs(indata[:, 0]).mean())

                self.stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                                             blocksize=CHUNK, callback=callback)
                self.stream.start()
                threading.Thread(target=self._close_when_unused, daemon=True).start()
        return self.level

    def _close_when_unused(self):
        while time.monotonic() - self.asked < MIC_IDLE_SECONDS:
            time.sleep(0.5)
        with self.lock:
            self.stream.close()
            self.stream, self.level = None, 0.0


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, config_path: Path):
        super().__init__(("127.0.0.1", 0), Handler)
        self.config_path = config_path
        self.token = secrets.token_urlsafe(24)
        self.mic = Mic()
        self.closing = None
        self.closing_lock = threading.Lock()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_port}/"

    def page_closed(self) -> None:
        with self.closing_lock:
            if self.closing is None:
                self.closing = threading.Timer(CLOSE_SECONDS, self.shutdown)
                self.closing.start()

    def page_active(self) -> None:
        with self.closing_lock:
            if self.closing is not None:
                self.closing.cancel()
                self.closing = None

    def settings(self) -> dict:
        defaults = {"config": json.loads((DEFAULTS / "config.json").read_text(encoding="utf-8")),
                    "ui": json.loads((DEFAULTS / "ui.json").read_text(encoding="utf-8"))}
        config, config_problem = _read(self.config_path)
        ui_path = ROOT / (config or defaults["config"]).get("ui_config", defaults["config"]["ui_config"])
        ui, ui_problem = _read(ui_path)
        voices = sorted(p.name.removesuffix(".onnx") for p in (ROOT / "voices").glob("*.onnx"))
        return {
            "config": config, "ui": ui, "defaults": defaults,
            "paths": {"config": _show(self.config_path), "ui": _show(ui_path)},
            "problems": [p for p in (config_problem, ui_problem) if p],
            "models": _ollama_models(), "voices": voices, "wake_words": WAKE_WORDS,
        }

    def save(self, texts: dict) -> dict:
        """Check both files the way Jarvis will load them, then write them."""
        try:
            config, ui = json.loads(texts["config"]), json.loads(texts["ui"])
            Config.parse(config, self.config_path.name)
            UiConfig.parse(ui, Path(config["ui_config"]).name)
        except (SystemExit, Exception) as e:   # the loaders exit with a message for a missing key
            raise ValueError(f"Not saved: {e}") from None
        ui_path = ROOT / config["ui_config"]
        _write(self.config_path, texts["config"])
        _write(ui_path, texts["ui"])
        return {"paths": {"config": _show(self.config_path), "ui": _show(ui_path)}}


class Handler(BaseHTTPRequestHandler):
    server: Server

    def log_message(self, format, *args):   # keep the terminal quiet
        pass

    def _send(self, status: int, body: bytes, kind: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, status: int = 200) -> None:
        self._send(status, json.dumps(data).encode("utf-8"), "application/json")

    def _host_ok(self) -> bool:
        """Asked for by this address, not by a website whose name was pointed at 127.0.0.1."""
        port = self.server.server_port
        return self.headers.get("Host") in (f"127.0.0.1:{port}", f"localhost:{port}")

    def _allowed(self) -> bool:
        """Only the page this server handed out: it alone knows the token."""
        query = parse_qs(urlparse(self.path).query)
        token = self.headers.get("X-Jarvis-Token") or query.get("token", [""])[0]
        return self._host_ok() and secrets.compare_digest(token, self.server.token)

    def _body(self) -> dict:
        return json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")

    def do_GET(self):
        path = urlparse(self.path).path
        self.server.page_active()
        if path == "/" and self._host_ok():
            page = PAGE.read_text(encoding="utf-8").replace("{{TOKEN}}", self.server.token)
            self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
        elif not self._allowed():
            self._json({"error": "Not allowed"}, 403)
        elif path == "/api/settings":
            self._json(self.server.settings())
        elif path == "/api/mic":
            try:
                self._json({"level": self.server.mic.read()})
            except Exception as e:
                self._json({"error": f"Couldn't open the microphone: {e}"}, 500)
        else:
            self._json({"error": "Not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        if not self._allowed():
            self._json({"error": "Not allowed"}, 403)
            return
        if path == "/api/bye":
            self.server.page_closed()
            self._json({})
            return
        self.server.page_active()
        try:
            if path == "/api/settings":
                self._json(self.server.save(self._body()))
            elif path == "/api/beep":
                import winsound
                body = self._body()
                winsound.Beep(int(body["hz"]), int(body["ms"]))
                self._json({})
            else:
                self._json({"error": "Not found"}, 404)
        except Exception as e:
            self._json({"error": str(e)}, 400)


def main():
    parser = argparse.ArgumentParser(prog="jarvis.settings", description="Jarvis settings page")
    parser.add_argument("--config", type=Path, default=ROOT / "config" / "config.json",
                        help="settings file to edit (default: config/config.json)")
    parser.add_argument("--no-browser", action="store_true", help="print the address instead of opening it")
    args = parser.parse_args()

    server = Server(args.config.resolve())
    print(f"Jarvis settings: {server.url}")
    print("It closes by itself when you close the page (or press Ctrl+C).", flush=True)
    if not args.no_browser:
        webbrowser.open(server.url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    server.server_close()
    print("Settings page closed.")


if __name__ == "__main__":
    main()
