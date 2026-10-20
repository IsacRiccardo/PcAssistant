"""Ears: wake word, recording a command, and transcribing it."""
import logging
import queue
from collections.abc import Callable

import numpy as np
import psutil
import sounddevice as sd
import winsound

from ..config import Config
from ..phrases import said, wake_phrase
from ..ui import Status

log = logging.getLogger("jarvis")

SAMPLE_RATE = 16000
CHUNK = 1280            # 80 ms of audio, the frame size openWakeWord expects


def _load_whisper(name: str):
    from faster_whisper import WhisperModel

    options = dict(device="cpu", compute_type="int8", cpu_threads=psutil.cpu_count(logical=False) or 4)
    try:   # use the downloaded copy without asking Hugging Face for updates on every launch
        return WhisperModel(name, local_files_only=True, **options)
    except Exception:
        return WhisperModel(name, **options)


class LanguagePicker:
    """Stands in for Whisper's model so that its language detection only picks from the user's languages:
    left to itself it can pick one they don't speak, mostly on short commands.
    Transcribing with multilingual=True detects from the same pass over the audio that the words come
    from, so knowing the language costs no extra time."""

    def __init__(self, whisper, languages: tuple[str, ...]):
        self.whisper = whisper
        self.tokens = {f"<|{lang}|>" for lang in languages}
        self.picked = languages[0]

    def __getattr__(self, name):   # everything else is Whisper's own
        return getattr(self.whisper, name)

    def detect_language(self, encoder_output):
        # (a code Whisper doesn't know matches nothing; then it keeps its own ranking)
        ranked = [[(token, p) for token, p in found if token in self.tokens] or found
                  for found in self.whisper.detect_language(encoder_output)]
        self.picked = ranked[0][0][0][2:-2]   # '<|de|>' -> 'de'
        return ranked


class Ears:
    def __init__(self, cfg: Config, status: Status):
        from openwakeword.model import Model

        self.cfg = cfg
        self.status = status
        log.info("Loading speech models...")
        self.whisper = _load_whisper(cfg.whisper_model)
        self.picker = None   # only needed when there's more than one language to choose from
        if len(cfg.languages) > 1 and self.whisper.model.is_multilingual:
            self.picker = self.whisper.model = LanguagePicker(self.whisper.model, cfg.languages)
        self.wake = Model(wakeword_models=list(cfg.wake_word), inference_framework="onnx")   # one score per wake word
        self.chunks = queue.Queue()   # microphone audio, CHUNK samples at a time, as it comes in
        self.transcribe(np.zeros(SAMPLE_RATE, np.float32))   # loads the speech detector now, not on the first command
        log.info("Speech models loaded")

    def _on_audio(self, indata, frames, time_info, status):
        """Called by the microphone stream, on its own thread, for every chunk it records."""
        chunk = indata[:, 0].copy()
        self.status.mic_loudness = float(np.abs(chunk).mean())
        self.chunks.put(chunk)

    def _drain(self):
        """Drop the audio recorded so far, like Jarvis's own voice while it was talking."""
        while not self.chunks.empty():
            self.chunks.get_nowait()

    def record_command(self, wait_seconds: float) -> np.ndarray | None:
        """Record until the user stops talking. None if they never started within wait_seconds."""
        frames, silent, started = [], 0, False
        per_sec = SAMPLE_RATE / CHUNK
        max_chunks = int(self.cfg.max_command_seconds * per_sec)
        silence_chunks = int(self.cfg.silence_seconds * per_sec)
        no_speech_chunks = int(wait_seconds * per_sec)
        grace_chunks = int(self.cfg.beep_grace_seconds * per_sec)
        for _ in range(max_chunks):
            chunk = self.chunks.get()
            frames.append(chunk)
            if len(frames) <= grace_chunks:   # the echo of the wake beep or reply, not the user talking
                continue
            if np.abs(chunk).mean() > self.cfg.silence_level:
                started, silent = True, 0
            else:
                silent += 1
            if started and silent >= silence_chunks:
                break
            if not started and len(frames) > no_speech_chunks:
                return None
        return np.concatenate(frames).astype(np.float32) / 32768.0

    def transcribe(self, audio: np.ndarray) -> tuple[str, str]:
        """What was said, and the language it was said in. With no languages set, Whisper chooses
        from all of them, which takes a separate pass over the audio."""
        languages = self.cfg.languages
        segments, info = self.whisper.transcribe(
            audio, language=languages[0] if languages else None, multilingual=self.picker is not None,
            beam_size=1, vad_filter=True, condition_on_previous_text=False, without_timestamps=True,
        )
        text = " ".join(s.text for s in segments).strip()   # the picker picks while this runs
        return text, self.picker.picked if self.picker else info.language

    def converse(self, on_command: Callable[[str, str], None], dictating: Callable[[], bool]) -> bool:
        """After the wake word, keep taking commands until the user goes quiet or says 'that's all'.
        Returns True if the user asked Jarvis to shut down."""
        wait = self.cfg.no_speech_seconds
        while True:
            print("Listening...", flush=True)
            self.status.set_state("listening")
            audio = self.record_command(wait)
            if audio is None:
                return False
            self.status.set_state("thinking")
            text, language = self.transcribe(audio)
            if not text:
                print("(didn't catch that)", flush=True)
                continue
            print(f"You: {text}", flush=True)
            log.info("You (%s): %s", language, text)
            self.status.heard(text)
            if not dictating():   # an email saying 'thanks, goodbye' shouldn't end the conversation
                if said(text, self.cfg.shutdown_phrases):
                    return True
                if said(text, self.cfg.sleep_phrases):
                    return False
            on_command(text, language)
            self._drain()       # ignore audio captured while Jarvis was talking
            wait = self.cfg.follow_up_seconds

    def listen_until_shutdown(self, on_command: Callable[[str, str], None], on_sleep: Callable[[], None],
                              dictating: Callable[[], bool], wake_reply: Callable[[], None] | None = None,
                              sleep_reply: Callable[[], None] | None = None) -> None:
        """on_command(text, language) handles each request, on_sleep() runs when a conversation ends,
        dictating() is True while the user is answering a question like the email ones.
        wake_reply() answers the wake word, sleep_reply() says Jarvis is going back to sleep; without
        them, a short beep."""
        phrase = " or ".join(f'"{wake_phrase(w)}"' for w in self.cfg.wake_word)
        with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                            blocksize=CHUNK, callback=self._on_audio):
            log.info("Listening... say %s", phrase)
            self.status.set_state("sleeping")
            while True:
                chunk = self.chunks.get()
                scores = self.wake.predict(chunk)
                if max(scores.values()) < self.cfg.wake_threshold:
                    continue
                if wake_reply:
                    wake_reply()
                else:
                    winsound.Beep(*self.cfg.wake_beep)
                self._drain()       # ignore the wake word's tail, and Jarvis's own answer to it
                if self.converse(on_command, dictating):
                    log.info("Shutdown requested")
                    return
                on_sleep()
                if sleep_reply:
                    sleep_reply()   # said while the orb is still up; it leaves right after
                self.status.set_state("sleeping")
                if not sleep_reply:
                    winsound.Beep(*self.cfg.sleep_beep)
                print(f"(sleeping - say {phrase})", flush=True)
                self._drain()
                self.wake.reset()
                log.info("Listening...")
