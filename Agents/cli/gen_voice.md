## Voice Generation

Generate spoken audio from text using `cli/gen_voice.py` in the foundation workspace:

```bash
python3 cli/gen_voice.py --source SOURCE --text "..." --output "path/to/clip.mp3" [--voice V] [--model M] [--format F]
```

**Sources** (pick based on need):
- `openai` — OpenAI TTS (`gpt-4o-mini-tts`, `tts-1`, `tts-1-hd`). Fast, low-cost, fixed allowlist of built-in voices. Best for narration, drafts, and high-volume jobs.
- `elevenlabs` — ElevenLabs TTS (`eleven_multilingual_v2`, `eleven_flash_v2_5`, ...). Higher fidelity, expressive voices, large live voice library. Best for hero / on-brand audio.

### Quickstart

```bash
# OpenAI, default voice "marin", model "gpt-4o-mini-tts", mp3 from extension
python3 cli/gen_voice.py --source openai --text "Welcome to the demo." --output assets/welcome.mp3

# ElevenLabs, explicit voice + model, browser-playable mp3
python3 cli/gen_voice.py --source elevenlabs \
  --text "Welcome to the demo." \
  --voice JBFqnCBsd6RMkjVDRZzb \
  --model eleven_multilingual_v2 \
  --output assets/welcome.mp3

# From a script file (UTF-8) into a WAV (auto-wrapped if provider returns raw PCM)
python3 cli/gen_voice.py --source openai --text-file scripts/intro.txt --output assets/intro.wav
```

### Discovery

```bash
python3 cli/gen_voice.py --source openai     --list-voices   # built-in allowlist
python3 cli/gen_voice.py --source openai     --list-models   # /v1/models filtered to tts
python3 cli/gen_voice.py --source elevenlabs --list-voices   # live /v1/voices on your account
python3 cli/gen_voice.py --source elevenlabs --list-models   # /v1/models can_do_text_to_speech
```

### Voices

- **OpenAI built-in:** `alloy`, `ash`, `ballad`, `coral`, `echo`, `fable`, `marin`, `nova`, `onyx`, `sage`, `shimmer`, `verse`, `cedar`. Custom voices may also be referenced by id but require prior consent + voice creation via OpenAI's audio endpoints.
- **ElevenLabs:** voices are account-scoped — always use `--list-voices` (or `ELEVENLABS_VOICE_ID` in `.env`) to pick a real `voice_id`.

### Models (defaults)

- `--source openai` → `OPENAI_TTS_MODEL` env or `gpt-4o-mini-tts` (only this model honors `--instructions`).
- `--source elevenlabs` → `ELEVENLABS_MODEL` env or `eleven_multilingual_v2`. Use `eleven_flash_v2_5` for low-latency / cheaper jobs.

### Format selection

If `--format` is omitted, format is inferred from the output file extension:

| extension | OpenAI `response_format` | ElevenLabs `output_format` |
|-----------|--------------------------|----------------------------|
| `.mp3`    | `mp3`                    | `mp3_44100_128`            |
| `.wav`    | `wav`                    | `pcm_44100` (auto-wrapped into WAV container) |
| `.opus`   | `opus`                   | —                          |
| `.flac`   | `flac`                   | —                          |
| `.aac`    | `aac`                    | —                          |
| `.pcm`    | `pcm` (24kHz s16le mono) | `pcm_44100` (raw)          |
| `.ulaw`   | —                        | `ulaw_8000` (raw, telephony) |

For anything else, pass `--format` explicitly with a value the provider accepts.

### Useful flags

- `--speed 1.1` — OpenAI accepts `0.25`–`4.0`; ElevenLabs accepts a narrower band via `voice_settings.speed`.
- `--instructions "Warm, confident narration."` — OpenAI `gpt-4o-mini-tts` only; ignored elsewhere.
- ElevenLabs voice tuning: `--stability 0.5 --similarity-boost 0.75 --style 0.1 --no-speaker-boost`.
- `--seed 42` — ElevenLabs best-effort deterministic output.
- `--language-code fr` — ElevenLabs forces a language when the model supports it.

### Output path & manifest

You decide the full path and filename based on what you're producing (e.g. `assets/voice/persona_laura_intro.mp3`). A `voice_manifest.json` is auto-created alongside outputs tracking source, model, voice, format, voice settings, and creation time.

### Environment

Reads from project-root `.env`:

```bash
OPENAI_API_KEY=...
OPENAI_TTS_MODEL=gpt-4o-mini-tts          # optional
OPENAI_TTS_VOICE=marin                    # optional

ELEVENLABS_API_KEY=...
ELEVENLABS_MODEL=eleven_multilingual_v2   # optional
ELEVENLABS_VOICE_ID=JBFqnCBsd6RMkjVDRZzb  # optional but required at runtime if --voice not passed
```

### Notes / gotchas

- OpenAI's documented max input length is **4096 characters** per request — split longer scripts into segments.
- `pcm` (OpenAI) and `ulaw_8000` (ElevenLabs) are **raw** byte streams with no container — only use them if you actually need that format (e.g. telephony). Prefer `.mp3` or `.wav` for ad-hoc playback.
- ElevenLabs uses the **`/stream`** endpoint variant under the hood, which returns the same audio bytes but starts faster.
- If you need word-level timing (subtitles, karaoke), call ElevenLabs `with-timestamps` directly — this CLI focuses on plain audio output.
