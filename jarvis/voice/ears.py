"""Ears: wake word, recording a command, and transcribing it."""
import ctypes
import logging
import queue
import shutil
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

import numpy as np
import psutil
import sounddevice as sd
import winsound

from ..config import Config
from ..phrases import about_the_computer, said, wake_phrase
from ..ui import Status

log = logging.getLogger("jarvis")

SAMPLE_RATE = 16000
CHUNK = 1280            # 80 ms of audio, the frame size openWakeWord expects
VAD_WINDOW = 512        # samples the speech detector judges at a time
VAD_SAMPLES = 8 * VAD_WINDOW   # the newest audio it's given for each chunk, a quarter second
SPEECH_PROBABILITY = 0.5       # how sure it must be that it hears speech
QUIET_FLOOR = 0.25      # share of silence_level below which even speech counts as silence, like a far-off TV
EARLY_SILENCE_SECONDS = 0.5   # quiet this long and Whisper starts on what was said, while silence_seconds runs out


def _samples(frames: list[np.ndarray]) -> np.ndarray:
    """Recorded chunks as one clip, the way Whisper takes it."""
    return np.concatenate(frames).astype(np.float32) / 32768.0


def _beep(beep: tuple[int, int]) -> None:
    """A (Hz, ms) beep; a length of 0 is silence."""
    if beep[1] > 0:
        winsound.Beep(*beep)


def _preload_cublas() -> None:
    """Whisper needs NVIDIA's cuBLAS library to run on the graphics card. Ollama comes with a copy, so load
    that one, and Whisper uses it; without it, Whisper looks for cuBLAS on the PATH."""
    ollama = shutil.which("ollama")
    if not ollama:
        return
    try:
        # by its full path rather than by adding its folder to the PATH: the apps Jarvis opens would
        # inherit that, and with it Ollama's own copies of the Visual C++ runtime
        ctypes.WinDLL(str(Path(ollama).parent / "lib" / "ollama" / "cuda_v12" / "cublas64_12.dll"))
    except OSError:
        pass


def _load_whisper(name: str):
    """Whisper on the NVIDIA graphics card if there is one, where a command takes a fraction of a second
    instead of about two, or else on the processor."""
    import ctranslate2
    from faster_whisper import WhisperModel

    def load(**options):
        try:   # use the downloaded copy without asking Hugging Face for updates on every launch
            return WhisperModel(name, local_files_only=True, **options)
        except Exception:
            return WhisperModel(name, **options)

    if ctranslate2.get_cuda_device_count():
        _preload_cublas()
        try:
            whisper = load(device="cuda", compute_type="int8_float16")
            # a missing library only shows when Whisper first hears something, so try it now; that also
            # gets the card ready, so the first command doesn't wait for it
            list(whisper.transcribe(np.zeros(SAMPLE_RATE, np.float32), language="en", beam_size=1,
                                    temperature=0.0)[0])
            log.info("Whisper runs on the graphics card")
            return whisper
        except Exception as e:
            log.info("Whisper can't use the graphics card (%s), so it runs on the processor", e)
    return load(device="cpu", compute_type="int8", cpu_threads=psutil.cpu_count(logical=False) or 4)


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
        from faster_whisper.vad import get_vad_model
        self.vad = get_vad_model()   # the speech detector Whisper itself uses, to know when the user stops
        self.chunks = queue.Queue()   # microphone audio, CHUNK samples at a time, as it comes in
        self.transcriber = ThreadPoolExecutor(1)   # one at a time: side by side they'd only slow each other down
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

    def _hears_speech(self, frames: list[np.ndarray]) -> bool:
        """Whether the newest audio is someone talking, by the speech detector rather than loudness:
        the end of a word, or a quieter 'from desktop', is often below silence_level."""
        recent = np.concatenate(frames[-(VAD_SAMPLES // CHUNK + 1):])[-VAD_SAMPLES:].astype(np.float32) / 32768.0
        recent = np.pad(recent, (VAD_SAMPLES - len(recent), 0))
        newest = CHUNK // VAD_WINDOW + 1   # the windows the newest chunk falls in
        return float(np.max(self.vad(recent)[-newest:])) >= SPEECH_PROBABILITY

    def record_command(self, wait_seconds: float) -> Future[tuple[str, str]] | None:
        """Record until the user stops talking, and transcribe it: what was said and its language, as
        transcribe returns them. None if they never started within wait_seconds.
        Talking starts when it's louder than silence_level; it ends after silence_seconds without
        speech. Once they're talking, quieter speech still counts, if the speech detector hears it.
        Whisper starts as soon as they pause, so it is mostly done by the time the silence shows they
        have finished. If they go on, that try is dropped."""
        frames, silent, started, loudest = [], 0, False, 0.0
        per_sec = SAMPLE_RATE / CHUNK
        max_chunks = int(self.cfg.max_command_seconds * per_sec)
        silence_chunks = int(self.cfg.silence_seconds * per_sec)
        early_chunks = min(int(EARLY_SILENCE_SECONDS * per_sec), silence_chunks)
        no_speech_chunks = int(wait_seconds * per_sec)
        grace_chunks = int(self.cfg.beep_grace_seconds * per_sec)
        ended = "the time limit"
        early = None   # Whisper's go at what was said before the latest pause
        for _ in range(max_chunks):
            chunk = self.chunks.get()
            frames.append(chunk)
            if len(frames) <= grace_chunks:   # the echo of the wake beep or reply, not the user talking
                continue
            level = float(np.abs(chunk).mean())
            loudest = max(loudest, level)
            talking = level > self.cfg.silence_level or (
                started and level > self.cfg.silence_level * QUIET_FLOOR and self._hears_speech(frames))
            if talking:
                started, silent, early = True, 0, None   # they went on, so that wasn't all of it
            else:
                silent += 1
            if started and silent == early_chunks:
                early = self.transcriber.submit(self.transcribe, _samples(frames))
            if started and silent >= silence_chunks:
                ended = "silence"
                break
            if not started and len(frames) > no_speech_chunks:
                return None
        log.info("Recorded %.1f s, loudest %.0f (silence_level %d), ended by %s",
                 len(frames) / per_sec, loudest, self.cfg.silence_level, ended)
        return early or self.transcriber.submit(self.transcribe, _samples(frames))

    def transcribe(self, audio: np.ndarray) -> tuple[str, str]:
        """What was said, and the language it was said in. With no languages set, Whisper chooses
        from all of them, which takes a separate pass over the audio."""
        languages = self.cfg.languages
        segments, info = self.whisper.transcribe(
            audio, language=languages[0] if languages else None, multilingual=self.picker is not None,
            beam_size=1, vad_filter=True, condition_on_previous_text=False, without_timestamps=True,
            # one try: unsure of mumbled audio, Whisper would otherwise try again up to 5 times, 5 guesses
            # each, taking 3 times as long only to come up with different nonsense
            temperature=0.0,
        )
        text = " ".join(s.text for s in segments).strip()   # the picker picks while this runs
        return text, self.picker.picked if self.picker else info.language

    def converse(self, on_command: Callable[[str, str], None], dictating: Callable[[], bool],
                 waiting: Callable[[], bool]) -> bool:
        """After the wake word, keep taking commands until the user goes quiet or says 'that's all'.
        While waiting() is True, like while a popup is up above the orb, going quiet doesn't end it.
        Returns True if the user asked Jarvis to shut down."""
        wait = self.cfg.no_speech_seconds
        while True:
            print("Listening...", flush=True)
            self.status.set_state("listening")
            heard = self.record_command(wait)
            if heard is None:
                if waiting():   # still up to the user: they may pick with the mouse, or say which
                    continue
                return False
            self.status.set_state("thinking")
            text, language = heard.result()
            if not text:
                print("(didn't catch that)", flush=True)
                continue
            print(f"You: {text}", flush=True)
            log.info("You (%s): %s", language, text)
            self.status.heard(text)
            # an email saying 'thanks, goodbye' shouldn't end the conversation, and 'shut down the
            # computer' is for the computer, not Jarvis
            if not dictating() and not about_the_computer(text):
                if said(text, self.cfg.shutdown_phrases):
                    return True
                if said(text, self.cfg.sleep_phrases):
                    return False
            on_command(text, language)
            self._drain()       # ignore audio captured while Jarvis was talking
            wait = self.cfg.follow_up_seconds

    def _wait_for_wake_word(self) -> None:
        while max(self.wake.predict(self.chunks.get()).values()) < self.cfg.wake_threshold:
            pass

    def listen_until_shutdown(self, on_command: Callable[[str, str], None], on_sleep: Callable[[], None],
                              dictating: Callable[[], bool], waiting: Callable[[], bool] = lambda: False,
                              awake: bool = False, wake_reply: Callable[[], None] | None = None,
                              sleep_reply: Callable[[], None] | None = None) -> None:
        """on_command(text, language) handles each request, on_sleep() runs when a conversation ends,
        dictating() is True while the user is answering a question like the email ones, waiting() while
        Jarvis waits for them to do something, like close a popup or pick from it, and mustn't go back
        to sleep. awake: take a request straight away, as after the greeting, without the wake word.
        wake_reply() answers the wake word, sleep_reply() says Jarvis is going back to sleep; without
        them, a short beep."""
        phrase = " or ".join(f'"{wake_phrase(w)}"' for w in self.cfg.wake_word)
        with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                            blocksize=CHUNK, callback=self._on_audio):
            if not awake:
                log.info("Listening... say %s", phrase)
                self.status.set_state("sleeping")
            while True:
                if not awake:
                    self._wait_for_wake_word()
                    if wake_reply:
                        wake_reply()
                    else:
                        _beep(self.cfg.wake_beep)
                    self._drain()       # ignore the wake word's tail, and Jarvis's own answer to it
                awake = False
                if self.converse(on_command, dictating, waiting):
                    log.info("Shutdown requested")
                    return
                on_sleep()
                if sleep_reply:
                    sleep_reply()   # said while the orb is still up; it leaves right after
                self.status.set_state("sleeping")
                if not sleep_reply:
                    _beep(self.cfg.sleep_beep)
                print(f"(sleeping - say {phrase})", flush=True)
                self._drain()
                self.wake.reset()
                log.info("Listening...")
