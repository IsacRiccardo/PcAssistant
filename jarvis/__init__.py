"""
Jarvis - a free, fully local voice assistant for Windows.

Pipeline:  wake word (openWakeWord) -> speech to text (faster-whisper)
           -> brain (Ollama local LLM + tools) -> voice (Piper TTS)
How one request is answered, step by step, is at the top of brain.py.

Package layout:
  __main__.py   command line, startup and the text / voice loops
  config.py     config/config.json loaded into typed settings
  brain.py      talks to the model, runs tools, streams the reply sentence by sentence
  languages.py  language names, and the sentences Jarvis says itself in each language
  memory.py     facts Jarvis remembers about the user
  phrases.py    matching short spoken commands like "shut down"
  tools/        everything the model can do: open apps and sites, look things up, email
  voice/        ears (wake word, recording, transcription) and mouth (speech)
  ui/           the floating orb overlay: how it moves (orb.py), and the window drawing it (hud.py)
  settings/     the settings page, run with `python -m jarvis.settings`
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent   # project folder: config/, voices/, logs/ live here
