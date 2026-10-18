"""Mouth: Piper TTS (offline, natural) with the Windows voice as fallback."""
import logging
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import sounddevice as sd

from .. import ROOT
from ..config import Config
from ..languages import of_voice
from ..ui import Status

log = logging.getLogger("jarvis")

VOICE_LEVEL_FPS = 30    # how often Jarvis's own loudness is sent to the window while speaking


class Mouth:
    def __init__(self, cfg: Config, status: Status):
        self.status = status
        self.voices = {}    # language -> Piper voice, in the order of piper_voices
        self.engine = None
        try:
            from piper import PiperVoice

            def load(name: str):
                path = ROOT / "voices" / f"{name}.onnx"
                if path.exists():
                    return PiperVoice.load(str(path))
                log.warning("Piper voice not found at %s.", path)

            with ThreadPoolExecutor(len(cfg.piper_voices) or 1) as pool:   # side by side they take hardly longer than one
                for name, voice in zip(cfg.piper_voices, pool.map(load, cfg.piper_voices)):
                    if voice:
                        self.voices.setdefault(of_voice(name), voice)
        except Exception as e:
            log.warning("Piper unavailable (%s).", e)
        if not self.voices:
            log.warning("No Piper voice, using the Windows voice.")
            import pyttsx3
            self.engine = pyttsx3.init()
        for language in cfg.languages:
            if self.voices and language not in self.voices:
                log.warning("No Piper voice speaks '%s', so replies in it use the first voice.", language)

    def say(self, text: str | Iterable[str], language: str | None = None) -> str:
        """Speak a reply: one string, or sentences as they arrive from a streamed reply, each spoken
        as soon as it comes in, with the voice for this language. Returns everything that was said."""
        pieces = [text] if isinstance(text, str) else text
        spoken = []
        print("Jarvis:", end="", flush=True)
        for piece in pieces:
            spoken.append(piece)
            print(f" {piece}", end="", flush=True)
            self.status.replied(" ".join(spoken))
            if piece:
                self._speak(piece, language)
        print()
        reply = " ".join(spoken)
        log.info("Jarvis: %s", reply)
        return reply

    def _speak(self, text: str, language: str | None) -> None:
        if not self.voices:
            self.status.set_state("speaking")
            self.engine.say(text)
            self.engine.runAndWait()
            return
        voice = self.voices.get(language) or next(iter(self.voices.values()))
        for chunk in voice.synthesize(text):   # one chunk per sentence
            data, rate = chunk.audio_int16_array, chunk.sample_rate
            self.status.set_state("speaking")
            sd.play(data, rate)
            self._follow_loudness(data, rate)
            sd.wait()

    def _follow_loudness(self, data: np.ndarray, rate: int) -> None:
        """While the audio plays, keep telling the window how loud this moment is, so the orb pulses along."""
        block = rate // VOICE_LEVEL_FPS
        n = len(data) // block
        if n == 0:
            return
        rms = np.sqrt((data[:n * block].astype(np.float32).reshape(n, block) ** 2).mean(axis=1))
        envelope = rms / (rms.max() + 1e-9)
        start = time.monotonic()
        while (i := int((time.monotonic() - start) * VOICE_LEVEL_FPS)) < n:
            self.status.voice_level = float(envelope[i])
            time.sleep(1 / VOICE_LEVEL_FPS)
        self.status.voice_level = 0.0
