# Meet Assistant v2

A live-conversation copilot for Linux: it listens to system audio, transcribes
it locally, detects when a question is being asked, and drafts an answer with
an LLM. Two modes:

- **Copilot** — for calls and live conversations where you need a quick,
  well-informed answer surfaced without breaking your flow (support/sales
  calls, oral exams, impromptu Q&A).
- **Session mode** — records a full meeting or lecture and produces a
  structured summary, key topics and action items at the end.

Rebuilt from scratch (v1 → v2) around one architectural bet: **let a VAD
decide where to cut**, instead of slicing audio on a fixed clock.

## Why this exists

v1 sliced audio every 3.5s on a clock. The cut landed mid-word, transcription
confidence was low and got dropped by the threshold — and there was no whole
sentence to anchor question detection on.

Here a VAD decides where to cut. Every `Utterance` starts and ends on a real
speech boundary.

## Architecture

```
capture/
  sources.py       PipeWire source discovery (default sink monitor)
  recorder.py      pw-record → PCM 16 kHz mono int16
  vad.py           Silero VAD via ONNX  [derived from BasedHardware/omi, MIT]
  segmenter.py     SILENCE/SPEECH/HANGOVER state machine → Utterance
  pipeline.py      wires it together; publishes turns to consumers
asr/
  base.py          Transcriber interface (swap engines without touching modes)
  whisper_local.py faster-whisper on CPU, int8, per-mode profiles
  worker.py        queue + thread; transcription never blocks capture
  streaming.py     LocalAgreement-2 — measured and kept out of the critical path (see below)
  live.py          VAD + streaming on a thread (partials for the UI)
modes/copilot/
  detector.py      pt-BR question detection (heuristic, 0ms, no network)
  engine.py        CopilotEngine: turn → detection → streaming LLM call
modes/session/
  recorder.py      full-session recording
  processor.py     batch transcription + structured summary
  prompts.py       summary/action-item prompt templates
llm/
  base.py          LLMClient interface
  openai_compat.py one client for OpenRouter/Groq/OpenAI/Ollama
config/
  settings.py      user config (BYOK) in ~/.config/meet-assistant/
ui/
  copilot_window.py  floating window (CustomTkinter)
domain/
  audio.py         Utterance, AudioSource, canonical format
  session.py        session-mode domain types
storage/
  sqlite_repo.py    session/turn persistence
exporters/
  obsidian.py        Markdown export to an Obsidian vault
  markdown.py, json_export.py
vendor/silero/     silero_vad.onnx model (MIT)
```

## Copilot: bring your own key (BYOK)

The app is built to be tested by other people, so **no key ships in the
code**. Each user configures their own on first run, stored in
`~/.config/meet-assistant/config.json` with `600` permissions.

Transcription runs locally — audio never leaves the machine. Only answer
generation uses the user's key.

Since OpenRouter, Groq, OpenAI and Ollama speak the same protocol, there is a
single client (`llm/openai_compat.py`) and switching providers just means
swapping the `base_url`:

| provider | why |
|---|---|
| OpenRouter (default) | one key, dozens of models, several `:free` |
| Groq | fast, free tier |
| OpenAI | |
| Ollama | local, no key, no internet |

### Free-tier quota

OpenRouter's `:free` models are capped at ~50 requests/day without account
credit. That cap does **not** wait out — it only resets the next day, and the
app distinguishes this from the per-minute limit (which does wait out, and
retries on its own).

A paid model fixes this for pennies. Real cost per response (~700 input
tokens, 150 output tokens):

| model | 1st token | cost/response | fits in US$ 3 |
|---|---|---|---|
| `google/gemma-3-12b-it` | 0.80s | US$ 0.000057 | ~52,000 responses |
| `qwen/qwen3-30b-a3b-instruct-2507` | 1.23s | US$ 0.000063 | ~47,000 |
| `google/gemini-2.5-flash-lite` | 1.58s | US$ 0.000065 | ~46,000 |

The first-run default stays on a `:free` model, so the app works without
requiring a card from anyone just trying it out.

### Choosing the default model

Measured, not picked by reputation. The metric that matters is **time to
first token** — that's what creates the feeling of real time, not full
response latency.

| model (free) | 1st token | result |
|---|---|---|
| `google/gemma-4-26b-a4b-it:free` | **1.8s** | ✅ default: direct, pt-BR, obeys `[SKIP]` |
| `nvidia/nemotron-3-nano-30b-a3b:free` | 1.0–5.2s | ❌ leaks reasoning in English |
| `nvidia/nemotron-3.5-lightning:free` | 2.2s | ❌ same, responds "Here's a thinking process:" |
| `nvidia/nemotron-3-super-120b-a12b:free` | 22.6s | ❌ too slow |
| `openai/gpt-oss-20b:free` | — | ❌ doesn't return `content` |

The Nemotron family is **reasoning models**: they emit the "thinking" along
with the answer. For text meant to be read at a glance mid-conversation,
that's the worst profile possible — even free and with a 1M context window.

Any model can be swapped in the Settings screen.

```bash
sudo apt install python3-tk        # Tk doesn't ship with Python on Ubuntu
.venv/bin/pip install httpx customtkinter
.venv/bin/python copilot_app.py
```

## Question detection

Local heuristic gate (0ms, no network) + `[SKIP]` in the LLM prompt as the
fine filter. The heuristic cuts ~90% of the volume; the model resolves
rhetoric and intent. One API call per real question, none on the rest.

The naive rule — "contains an interrogative word" — **failed on 100% of real
cases**: in continuous speech, Portuguese words like `como`, `quando` and
`para que` are often conjunctions, not question openers. What survived,
validated against real transcripts:

- ends in `?` → question
- explicit request without `?` ("tell me", "can you explain") → question
- interrogative word **opening** the turn and a short sentence (≤15 words) → question
- filler words (`né?`, `tá?`, `certo?`) never trigger

Validated live in a real class: 23 turns, 2 questions detected (both
correct), zero false positives.

The search is **per sentence, not per whole turn**: someone speaking without
pausing produces blocks with several sentences inside, and a question buried
in the middle disappears if you only look at the whole. But the **full turn**
goes to the LLM — a lone sentence is often a fragment with no referent.

### Auto vs. manual

The **Auto** switch decides whether Copilot calls the LLM on its own:

- **on** — answers every detected question automatically. Useful when you
  want zero extra effort and are comfortable letting every question trigger
  a response.
- **off** — transcribes and highlights questions, but only calls the LLM on
  the "Answer last turn" button. Right for a lecture or talk, where most
  questions are from the speaker to the audience, not to you.

### Measured session (4min19s)

```
18 turns transcribed    median 4.2s per turn (8s ceiling)
 4 LLM calls             100% OK, 0 errors, 0 abandoned
                         total cost ≈ US$ 0.0002
```

## Run

System requirements: PipeWire with `pw-record` and `pw-dump`
(`sudo apt install pipewire-bin`). No PyAudio, no PortAudio, no virtual audio
cable, no manual device setup.

```bash
python3 -m venv .venv
.venv/bin/pip install numpy onnxruntime faster-whisper

.venv/bin/python demo_capture.py --list      # available sources
.venv/bin/python demo_capture.py             # capture + VAD only
.venv/bin/python demo_live.py                # capture + live transcription
.venv/bin/python demo_live.py --profile aula # high-quality profile
.venv/bin/python tests/test_segmenter.py     # 8 tests, no sound card needed
```

## Transcription: measured numbers

Local Whisper (faster-whisper, int8, CPU), on this machine — 12 threads, no
GPU — over real audio from a Portuguese-language talk captured through the
pipeline. RTF = processing time ÷ audio duration.

| model | latency floor | RTF on a 25s turn | quality |
|---|---|---|---|
| tiny | ~0.5s | 0.03 | poor: misses proper nouns and technical terms |
| base | ~0.9s | 0.06 | mediocre |
| small | ~2.2s | 0.14 | good — readable and faithful to content |
| large-v3-turbo | ~10s | 0.44 | best: gets names and punctuation right |

Whisper processes in fixed 30s windows, so **there's a latency floor per
call, independent of turn size**. With `small`, a 2.8s turn took 2.2s. With
`large-v3-turbo`, a 1.1s turn took 10s — RTF 8.89.

Two design consequences:

1. **Copilot stays on `small`.** Best trade-off: ~2.2s floor, usable quality.
   No local model goes below that — the floor is architectural to Whisper,
   not a CPU shortage.

2. **Session mode can't transcribe turn by turn.** Paying a fixed 10s cost
   per sentence makes `large-v3-turbo` unviable that way. A session batches
   turns into ~30s blocks (the model's window size) and transcribes in
   bulk — the fixed cost dilutes and real RTF lands at 0.44, i.e. a 1h
   session transcribed in ~26 min of processing.

The quality justifies it: where `small` understood one thing, `turbo`
correctly caught names and technical terms `small` missed entirely.

## Continuous transcription: tried, measured, dropped from the critical path

The hypothesis was to take ASR off Copilot's critical path: transcribe while
the person speaks (LocalAgreement-2, `asr/streaming.py`), so the text would
already be ready by the end of the sentence. Implemented and measured on the
same audio, the hypothesis **didn't hold up**:

| model | streaming | batch |
|---|---|---|
| tiny | 0.7 – 1.4s | 0.5s |
| base | 1.3 – 3.3s | 0.9s |
| small | 2.8 – 4.7s | 2.2s |

Batch wins at every size, and streaming quality is worse too.

The reason is structural. Whisper's cost is dominated by the encoder pass
over a fixed 30s window, which costs nearly the same for 2s or 20s of audio.
LocalAgreement runs that pass several times per turn instead of once. On GPU,
where the pass costs ~100ms, that's cheap and streaming wins. On CPU, where
it costs 1–2s, multiplying the dominant operation only makes it worse.

The module stays in the project: it's the correct architecture if there's a
GPU, or if we swap in a natively streaming engine, and the partials during
speech serve the UI. It just doesn't sit between end-of-speech and the LLM.

Reproducible without a sound card:

```bash
.venv/bin/python demo_stream.py --model small --replay turno_0002.wav
```

## Two PipeWire traps

Both cost real debugging time and are documented in the code so they don't
come back:

1. **A sink's monitor is not a `<sink>.monitor` node.** That's PulseAudio
   convention. In PipeWire, monitors are ports on the sink itself.

2. **`pw-record --target <sink>` alone doesn't capture the monitor** — it
   silently ignores the target and falls back to the default microphone. It
   records sound, looks like it works, and captures the wrong thing. You
   need `-P stream.capture.sink=true`.

Minor detail, same effect: writing to stdout, `pw-record` emits an AU
container (magic `.snd`), not raw PCM. The header needs to be stripped.

## Validation

Tested with a YouTube talk playing through a Bluetooth headset (the default
sink is resolved at runtime, so headset or speakers both work):

```
[   0.0s →  25.5s]  25.5s of speech
[  26.0s →  31.4s]   5.3s of speech
[  31.9s →  37.6s]   5.7s of speech
[  38.1s →  39.2s]   1.1s of speech
```

Running the VAD again over the saved WAVs: 85–95% of each turn's windows are
speech, with 64–75% of the energy in the voice band (300–3400 Hz).

And with live transcription (`demo_live.py`, copilot profile):

```
[   0.0s] ( 9.4s · ASR 2.6s · RTF 0.28)
   ...This photo from August 21st, 2016. The day of the Olympic
   final here in Rio de Janeiro against Italy.

[  44.1s] ( 8.0s · ASR 2.5s · RTF 0.32)
   I have a bedside book that says Only the Paranoid Survive,
   as in, those guys who are almost paranoid about doing,
   about following through, about not giving up
```

## Credits

`capture/vad.py` and the segmentation constants are derived from
[BasedHardware/omi](https://github.com/BasedHardware/omi)
(`backend/utils/stt/vad.py` and `vad_gate.py`), MIT — copy at
`vendor/OMI_LICENSE`. The Silero VAD model is also MIT.

Dropped from Omi: the hosted VAD layer, Redis, telemetry, and the coupling
to Deepgram — none of which make sense for a local app.
