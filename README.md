# Jarvis - free local voice assistant

Runs fully offline on your laptop: wake word (openWakeWord) → speech to text (faster-whisper) → local AI (Ollama, Llama 3.2 3B) → voice (Piper TTS).

## First-time setup

1. Clone or download this repository somewhere simple, like `C:\Projects\jarvis` (avoid OneDrive folders).
2. Open the folder in VS Code (**File > Open Folder**). Install the recommended Python extension when asked.
3. **Terminal > Run Task... > Setup Jarvis (install everything)**.
   It installs Python 3.11 and Ollama if missing, creates `.venv`, installs packages, and downloads all models (about 3 GB total, takes a while the first time).
   If winget installs Python or Ollama, restart VS Code and run the setup task once more.

## Running

- **Terminal > Run Task... > Run Jarvis (text mode)**: type to it. Test this first.
- **Terminal > Run Task... > Run Jarvis (voice)**: say "Hey Jarvis", wait for it to answer "Yes?", then talk. After each reply it keeps listening, so you can give the next command without the wake word. It goes back to sleep (it says "Going to sleep.") once you stay quiet for `follow_up_seconds` or say "that's all".
  When Jarvis wakes up, an animated orb fades in at the bottom centre of the screen, just above the taskbar, floating over whatever is on screen. It reacts to your voice while listening, spins while thinking and pulses with Jarvis's voice while speaking, with a caption of what you said and the reply. It fades away when Jarvis goes back to sleep. Clicks pass straight through it and it never takes keyboard focus, so it can't get in your way. To quit, say "shut down" while Jarvis is listening (or press Ctrl+C in the terminal, or close it). Run `python -m jarvis --no-ui` for voice mode without the orb.
- Or press **F5** and pick a configuration to run with the debugger.

The tasks and F5 configurations marked "debug log" also write a detailed log to `logs/jarvis.log`.

Plug the laptop in. On battery Jarvis switches to the smaller 1B model automatically.

## Try saying

- "What time is it?"
- "Open Spotify" / "Open calculator": any app in your Start menu works, nothing to set up. Jarvis looks them up each time it starts, so it suits whatever PC it runs on. Short names work too ("chrome" for Google Chrome, "vs code" for Visual Studio Code)
- "Close Excel": the same as clicking its X, so an app with unsaved work still asks whether to save first. Every window of that app closes (all Chrome windows for "close Chrome")
- "Search the web for Fiat Panda oil type"
- "How's my battery?"
- "Remember that my favourite football team is Poli Timișoara"
- "Send an email" (Jarvis asks who to, the subject and what it should say), or all at once: "Send an email to mom saying I'll be late". Say addresses like "john at gmail dot com". Jarvis opens the filled-in email in Gmail; you check it and press Send. Say "cancel" to drop it.

## Customising

The easy way: **Terminal > Run Task... > Configure Jarvis (settings)** (or `python -m jarvis.settings`). A settings page opens in your browser with every option explained, a live preview of the orb, a microphone test for `silence_level`, and buttons to play the beeps. Each setting can go back to its default, and **Save** writes the two files below. Restart Jarvis afterwards to use them. The page only runs on this PC, and it stops by itself when you close it.

The factory settings live in `config/defaults/`. If `config/config.json` or `config/ui.json` is missing, Jarvis (and setup) creates it from there, so deleting a file is a quick way to start over.

Or edit the files by hand. Everything is in `config/config.json`:

- `personality`, `greeting`, `user_name`
- `user_title`: what Jarvis calls you instead of your name, like `"sir"`, `"miss"` or `"ma'am"`, at most once in a reply. Leave it empty and Jarvis uses `user_name` now and then
- `languages`: the languages you speak, as codes (`["en", "de"]`). Jarvis works out which one each command is in and answers in it. With one language it skips that step, so it replies a little faster; an empty list understands any language. Needs a Whisper model without `.en`
- `piper_voices`: one Piper voice per language (`"de_DE-thorsten-medium"`; the name starts with the language). Replies use the voice for the language you spoke; the first voice also says the greeting and farewell. Rerun setup after adding one, to download it
- `model_plugged_in` / `model_on_battery`: any model from ollama.com (run `ollama pull <name>` after changing)
- `context_size`: how many tokens of conversation the model can see. 2048 lets the 3B model fit entirely on a 4 GB graphics card, which makes replies about 30% faster; raise it to 4096 if Jarvis forgets things in long conversations
- `keep_alive`: how long the model stays loaded after the last request (e.g. `"30m"`, or `-1` for as long as Jarvis runs). Loading it again takes several seconds
- `silence_level`: raise it if Jarvis never stops recording in a noisy room, lower it if it cuts you off
- `silence_seconds`: how long you have to be quiet before Jarvis treats the command as finished. Lower is snappier; raise it if Jarvis cuts you off when you pause mid-sentence
- `follow_up_seconds`: how long Jarvis keeps waiting for your next command before going back to sleep
- `sleep_phrases`: short phrases that end the conversation right away. This and the other phrase lists can mix languages, and accents don't matter ("café" and "cafe" both work)
- `shutdown_phrases`, `farewell`: short phrases that stop Jarvis completely, and what it says before it goes. They only count as a short command on its own (5 words or fewer), so "shut down" works but a longer sentence containing it doesn't
- `wake_word`: what wakes Jarvis up: one or more of `hey_jarvis`, `alexa`, `hey_mycroft` and `hey_rhasspy` (`["hey_jarvis", "alexa"]`; any of them works). For your own phrase you need an openWakeWord model for it (a `.onnx` file: train one with openWakeWord's training notebook, or download one someone shared). Put it in the `wake_words` folder and pick it on the settings page, or add its path to the list, like `"wake_words/hey_computer.onnx"`
- `wake_threshold`: lower (0.3) if it doesn't hear you, higher (0.7) if it wakes by accident
- `wake_reply`: what Jarvis says when it hears the wake word, one picked at random (`["Yes?", "Yes, sir?"]`). Keep them short: it listens once it has finished speaking. An empty list plays `wake_beep` instead
- `sites`: website names → address (e.g. `"github": "github.com"`), so "open github" works without spelling out the URL
- `contacts`: names → email addresses (e.g. `"mom": "mom@example.com"`), so you can say "email mom" instead of spelling out the address
- `browsers`: browsers Jarvis can open sites or new tabs in (command, flags before the URL, blank new-tab page)
- `tool_words`: for each tool, the words that offer it to the model (`"battery_status": ["battery", "charging"]`). Jarvis only offers the tools whose words your request contains as whole words, in simple forms too ("share" also matches "shares", "rain" matches "raining"), so small talk stays fast and the model picks from just a few. Add a word if a request should trigger a tool but doesn't; an empty list turns the tool off
- `wake_beep`, `sleep_beep`: the beeps when Jarvis wakes up and goes back to sleep, when it has no `wake_reply` or `sleep_reply` to say (pitch in `hz`, length in `ms`)
- `sleep_reply`: what Jarvis says when it goes back to sleep, one picked at random (`["Going to sleep.", "Call me if you need me."]`). An empty list plays `sleep_beep` instead
- `beep_grace_seconds`: how long to ignore the mic after the wake beep, so its echo doesn't start a recording
- `memory_file`, `log_dir`: where memories and logs are kept, relative to the project folder
- `ui_config`: the file with the orb's look, relative to the project folder (see below)

The orb's look lives in `config/ui.json`:

- `width`, `height`, `margin`: overlay size and the gap above the taskbar; `orb_y`: orb centre from the top of the overlay
- `fps`: animation smoothness. Speeds don't change with it, only how fluid the motion is
- `opacity`: how solid the orb is
- `fade_seconds`: how long the orb takes to appear. It springs in while its glow blooms, the sound bars grow out of the core and the rings sweep in last; going to sleep, the rings unwind and it sinks away a little quicker. The caption grows out of a small pill and springs to fit its text
- `bounce`: 0 to 1, how much the orb and caption overshoot as they spring into place (0 glides in without any)
- `mic_full_scale`: microphone loudness that fills the sound ring. Lower it if the ring barely moves when you talk
- `bars`, `rings`: number of bars in the sound ring, and the spinning arcs around it (`radius`, `start` angle, arc `length` in degrees, line `width`, `speed` multiplier, negative spins backwards)
- `colors`, `spin`: orb colour and ring speed (degrees per second) for each state: starting, sleeping, listening, thinking, speaking, error
- `font`, `heard_font_size`, `reply_font_size`, `heard_color`, `reply_color`, `panel_color`: the caption under the orb

Every key in both files is required; if one is missing Jarvis tells you which on startup. To try different settings without touching your main file: `python -m jarvis --config other.json`.

New abilities: write a function with a clear docstring in one of the modules in `jarvis/tools/` and add it to the list in `Tools.__init__` (`jarvis/tools/__init__.py`). Then give it its words in `tool_words`, in `config/defaults/config.json` and your own `config/config.json` (Jarvis tells you on startup if you forget), and a name on the settings page in `TOOL_LABELS` (`jarvis/settings/page.html`). The model decides when to call it.

Memories are stored in `data/memory.json`. Delete the file to wipe them. Logging is off unless you start Jarvis with a "debug log" task or F5 configuration, or add `--debug` on the command line (`python -m jarvis --debug`, `python -m jarvis --text --debug`). Then tool calls, status and the full conversation go to `logs/jarvis.log`, which is cleared on each launch. Warnings and errors always show in the terminal.

## Project layout

```
config/            settings: config.json (behaviour) and ui.json (the orb's look)
  defaults/        the factory settings, used when a file is missing
jarvis/            the assistant, run with `python -m jarvis`
  __main__.py      command line, startup, text and voice loops
  brain.py         talks to the model, runs tools, streams the reply sentence by sentence
  config.py        loads config/config.json
  languages.py     language names, and the sentences Jarvis says itself in each language
  memory.py        facts Jarvis remembers about you
  phrases.py       matching short commands like "shut down"
  tools/           what the model can do
    launcher.py    open apps, websites, browser tabs, Google searches
    lookup.py      time, battery, stock prices, web lookups
    email.py       the step-by-step email flow
  voice/
    ears.py        wake word, recording, speech to text
    mouth.py       text to speech
  ui/
    orb.py         how the floating orb overlay moves
    hud.py         draws it with Qt
    config.py      loads config/ui.json
    status.py      what the voice loop reports to the orb
  settings/        the settings page, run with `python -m jarvis.settings`
    server.py      a small local server that reads and writes the settings files
    page.html      the page itself
voices/            Piper voice files (downloaded by setup)
wake_words/        your own wake word models (.onnx), if any
data/              memory.json (created when Jarvis first remembers something)
logs/              jarvis.log in debug mode, cleared on each launch
```

## Troubleshooting

- **"Can't reach Ollama"**: open the Ollama app from the Start menu.
- **Scripts disabled error**: the task already uses `-ExecutionPolicy Bypass`; if running manually, use `powershell -ExecutionPolicy Bypass -File setup.ps1`.
- **No microphone input**: Windows Settings > Privacy & security > Microphone, allow desktop apps.
- **Robotic voice**: Piper failed to install, so it fell back to the Windows voice. Rerun setup.
