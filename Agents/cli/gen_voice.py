#!/usr/bin/env python3
"""
Voice / text-to-speech CLI for autonomous agents.

Usage:
    python gen_voice.py --text "Hello world" --source openai --output path/to/clip.mp3
    python gen_voice.py --text "Hello world" --source elevenlabs --output assets/intro.mp3
    python gen_voice.py --text-file script.txt --source openai --voice nova --output narration.wav
    python gen_voice.py --source openai --list-voices
    python gen_voice.py --source elevenlabs --list-voices
    python gen_voice.py --source elevenlabs --list-models
    python gen_voice.py --source elevenlabs --search-shared "warm narrator" --gender female --language en
    python gen_voice.py --source elevenlabs --add-voice <PUBLIC_OWNER_ID>:<VOICE_ID> --new-name "Hero Narrator"

Sources:
    openai      - OpenAI text-to-speech (gpt-4o-mini-tts, tts-1, tts-1-hd).
                  Best for fast, low-cost narration with a fixed allowlist of voices.
    elevenlabs  - ElevenLabs text-to-speech (eleven_multilingual_v2, eleven_flash_v2_5, ...).
                  Best for high-fidelity / expressive voices and a large live voice library.

Output format is inferred from the output file extension when --format is omitted:
    .mp3  -> mp3
    .wav  -> wav
    .opus -> opus (openai), or fallback for elevenlabs
    .flac -> flac (openai only)
    .aac  -> aac  (openai only)
    .pcm  -> raw PCM (openai)
    .ulaw -> raw 8 kHz μ-law (elevenlabs ulaw_8000)

Common voices:
    OpenAI built-in: alloy ash ballad coral echo fable marin nova onyx sage shimmer verse cedar
    ElevenLabs:      use --list-voices to see voices available on your account.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

# Load .env from project root (three levels up from cli/)
load_dotenv(Path(__file__).parent.parent.parent / ".env")

MANIFEST_NAME = "voice_manifest.json"

# Built-in OpenAI voice allowlist (per OpenAI docs)
OPENAI_VOICES = [
    "alloy", "ash", "ballad", "coral", "echo", "fable",
    "marin", "nova", "onyx", "sage", "shimmer", "verse", "cedar",
]

# OpenAI-supported response formats
OPENAI_FORMATS = {"mp3", "opus", "aac", "flac", "wav", "pcm"}

# Mapping of file extension -> default provider format string
EXT_TO_OPENAI_FORMAT = {
    ".mp3": "mp3",
    ".wav": "wav",
    ".opus": "opus",
    ".flac": "flac",
    ".aac": "aac",
    ".pcm": "pcm",
}

EXT_TO_ELEVENLABS_FORMAT = {
    ".mp3": "mp3_44100_128",
    ".wav": "pcm_44100",          # ElevenLabs returns raw PCM; we wrap into WAV below
    ".pcm": "pcm_44100",
    ".ulaw": "ulaw_8000",
}


# --- Shared helpers ---

def update_manifest(output_path: Path, entry: dict) -> None:
    """Append an entry to the manifest file in the same directory as the output."""
    manifest_path = output_path.parent / MANIFEST_NAME
    manifest = []
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text())
        except (json.JSONDecodeError, OSError):
            manifest = []
    manifest.append(entry)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")


def http_post_bytes(url: str, headers: dict, body: bytes, timeout: int = 120) -> bytes:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code} from {url}: {detail}") from e


def http_get_json(url: str, headers: dict, timeout: int = 30) -> dict:
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code} from {url}: {detail}") from e


def wrap_pcm_as_wav(pcm_bytes: bytes, sample_rate: int, channels: int = 1, sample_width: int = 2) -> bytes:
    """Wrap raw little-endian PCM bytes in a WAV container (no extra deps)."""
    import io
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)
    return buf.getvalue()


# --- OpenAI ---

def openai_headers() -> dict:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY not set in .env")
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def list_openai_voices() -> None:
    print("OpenAI built-in voices (per docs):")
    for v in OPENAI_VOICES:
        print(f"  - {v}")
    print(
        "\nOpenAI does not expose a live voices endpoint for built-in voices.\n"
        "Custom voices are referenced by id via {\"id\": \"voice_...\"} in the request."
    )


def list_openai_models() -> None:
    headers = {"Authorization": openai_headers()["Authorization"]}
    data = http_get_json("https://api.openai.com/v1/models", headers)
    tts_models = sorted(
        m["id"] for m in data.get("data", [])
        if "tts" in m.get("id", "").lower()
    )
    print("OpenAI TTS-capable models:")
    for m in tts_models:
        print(f"  - {m}")


def generate_openai(
    text: str,
    output_path: Path,
    voice: str,
    model: str,
    response_format: str,
    speed: float | None,
    instructions: str | None,
) -> Path:
    if response_format not in OPENAI_FORMATS:
        raise ValueError(
            f"Unsupported OpenAI format '{response_format}'. "
            f"Supported: {sorted(OPENAI_FORMATS)}"
        )

    payload: dict = {
        "model": model,
        "input": text,
        "voice": voice,
        "response_format": response_format,
    }
    if speed is not None:
        payload["speed"] = speed
    if instructions:
        # Only gpt-4o-mini-tts supports instructions; OpenAI ignores it elsewhere.
        payload["instructions"] = instructions

    body = json.dumps(payload).encode("utf-8")
    audio = http_post_bytes(
        "https://api.openai.com/v1/audio/speech",
        openai_headers(),
        body,
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(audio)

    update_manifest(output_path, {
        "file": str(output_path),
        "source": "openai",
        "model": model,
        "voice": voice,
        "format": response_format,
        "speed": speed,
        "instructions": instructions,
        "text_chars": len(text),
        "created": datetime.now(timezone.utc).isoformat(),
    })
    print(f"[openai] Saved: {output_path} ({len(audio)} bytes, format={response_format})")
    return output_path


# --- ElevenLabs ---

def elevenlabs_headers() -> dict:
    api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        raise RuntimeError("ELEVENLABS_API_KEY not set in .env")
    return {
        "xi-api-key": api_key,
        "Content-Type": "application/json",
    }


def list_elevenlabs_voices() -> None:
    headers = {"xi-api-key": elevenlabs_headers()["xi-api-key"]}
    data = http_get_json("https://api.elevenlabs.io/v1/voices", headers)
    print("ElevenLabs voices:")
    print(f"  {'NAME':<28} {'VOICE_ID':<24} CATEGORY")
    for v in data.get("voices", []):
        name = v.get("name", "")[:28]
        vid = v.get("voice_id", "")
        cat = v.get("category", "")
        print(f"  {name:<28} {vid:<24} {cat}")


def list_elevenlabs_models() -> None:
    headers = {"xi-api-key": elevenlabs_headers()["xi-api-key"]}
    data = http_get_json("https://api.elevenlabs.io/v1/models", headers)
    print("ElevenLabs TTS-capable models:")
    for m in data:
        if not m.get("can_do_text_to_speech"):
            continue
        mid = m.get("model_id", "")
        name = m.get("name", "")
        max_chars = m.get("maximum_text_length_per_request", "?")
        print(f"  - {mid:<32} {name}  (max chars: {max_chars})")


def search_elevenlabs_shared(
    query: str | None,
    page_size: int,
    gender: str | None,
    age: str | None,
    accent: str | None,
    language: str | None,
    category: str | None,
    use_cases: str | None,
    descriptives: str | None,
    featured: bool,
) -> None:
    """Search the ElevenLabs public Voice Library (shared voices)."""
    headers = {"xi-api-key": elevenlabs_headers()["xi-api-key"]}
    params: dict = {"page_size": page_size}
    if query:
        params["search"] = query
    if gender:
        params["gender"] = gender
    if age:
        params["age"] = age
    if accent:
        params["accent"] = accent
    if language:
        params["language"] = language
    if category:
        params["category"] = category
    if use_cases:
        params["use_cases"] = use_cases
    if descriptives:
        params["descriptives"] = descriptives
    if featured:
        params["featured"] = "true"

    qs = urllib.parse.urlencode(params)
    url = f"https://api.elevenlabs.io/v1/shared-voices?{qs}"
    data = http_get_json(url, headers)
    voices = data.get("voices", [])
    if not voices:
        print(f"[elevenlabs] No shared voices matched.")
        return

    print(f"ElevenLabs shared voices (showing {len(voices)}):")
    print(f"  {'NAME':<24} {'VOICE_ID':<24} {'PUBLIC_OWNER_ID':<34} GENDER  ACCENT          LANG  CATEGORY")
    for v in voices:
        name = (v.get("name") or "")[:24]
        vid = v.get("voice_id") or ""
        owner = v.get("public_owner_id") or ""
        gen = (v.get("gender") or "")[:6]
        acc = (v.get("accent") or "")[:14]
        lang = (v.get("language") or "")[:4]
        cat = v.get("category") or ""
        print(f"  {name:<24} {vid:<24} {owner:<34} {gen:<7} {acc:<15} {lang:<5} {cat}")
    print(
        "\nTo add a voice to your collection:\n"
        "  python3 cli/gen_voice.py --source elevenlabs \\\n"
        "    --add-voice <PUBLIC_OWNER_ID>:<VOICE_ID> --new-name 'My Name For It'"
    )


def add_elevenlabs_shared_voice(public_user_id: str, voice_id: str, new_name: str) -> None:
    """Add a shared voice from the public Voice Library to your account."""
    headers = elevenlabs_headers()
    url = f"https://api.elevenlabs.io/v1/voices/add/{public_user_id}/{voice_id}"
    body = json.dumps({"new_name": new_name}).encode("utf-8")
    resp = http_post_bytes(url, headers, body)
    try:
        data = json.loads(resp)
    except json.JSONDecodeError:
        print(resp.decode("utf-8", errors="replace"))
        return
    new_voice_id = data.get("voice_id") or "(unknown)"
    print(f"[elevenlabs] Added voice '{new_name}' to your account.")
    print(f"  voice_id: {new_voice_id}")
    print(f"  Use it via: --voice {new_voice_id}")
    print(f"  Or set ELEVENLABS_VOICE_ID={new_voice_id} in .env")


def generate_elevenlabs(
    text: str,
    output_path: Path,
    voice: str,
    model: str,
    output_format: str,
    stability: float,
    similarity_boost: float,
    style: float | None,
    speed: float | None,
    use_speaker_boost: bool,
    seed: int | None,
    language_code: str | None,
) -> Path:
    voice_settings: dict = {
        "stability": stability,
        "similarity_boost": similarity_boost,
        "use_speaker_boost": use_speaker_boost,
    }
    if style is not None:
        voice_settings["style"] = style
    if speed is not None:
        voice_settings["speed"] = speed

    payload: dict = {
        "text": text,
        "model_id": model,
        "voice_settings": voice_settings,
    }
    if seed is not None:
        payload["seed"] = seed
    if language_code:
        payload["language_code"] = language_code

    qs = urllib.parse.urlencode({"output_format": output_format})
    url = (
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice}/stream?{qs}"
    )
    body = json.dumps(payload).encode("utf-8")
    audio = http_post_bytes(url, elevenlabs_headers(), body)

    # If the user asked for a .wav file but we requested raw PCM, wrap it.
    suffix = output_path.suffix.lower()
    if suffix == ".wav" and output_format.startswith("pcm_"):
        try:
            sample_rate = int(output_format.split("_", 1)[1])
        except (IndexError, ValueError):
            sample_rate = 44100
        audio = wrap_pcm_as_wav(audio, sample_rate=sample_rate)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(audio)

    update_manifest(output_path, {
        "file": str(output_path),
        "source": "elevenlabs",
        "model": model,
        "voice_id": voice,
        "format": output_format,
        "voice_settings": voice_settings,
        "seed": seed,
        "language_code": language_code,
        "text_chars": len(text),
        "created": datetime.now(timezone.utc).isoformat(),
    })
    print(f"[elevenlabs] Saved: {output_path} ({len(audio)} bytes, format={output_format})")
    return output_path


# --- Main ---

def resolve_format(source: str, output_path: Path, explicit: str | None) -> str:
    if explicit:
        return explicit
    suffix = output_path.suffix.lower()
    if source == "openai":
        fmt = EXT_TO_OPENAI_FORMAT.get(suffix)
        if fmt:
            return fmt
        return "mp3"
    if source == "elevenlabs":
        fmt = EXT_TO_ELEVENLABS_FORMAT.get(suffix)
        if fmt:
            return fmt
        return "mp3_44100_128"
    raise ValueError(f"Unknown source: {source}")


def main():
    parser = argparse.ArgumentParser(
        description="Generate spoken audio from text using OpenAI or ElevenLabs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--source", required=True, choices=["openai", "elevenlabs"],
                        help="TTS provider")
    parser.add_argument("--text", help="Text to synthesize")
    parser.add_argument("--text-file", help="Path to a UTF-8 text file to synthesize")
    parser.add_argument("--output", help="Output audio file path (e.g. assets/intro.mp3)")
    parser.add_argument("--voice", help=(
        "Voice name (openai) or voice_id (elevenlabs). "
        "Defaults: openai=$OPENAI_TTS_VOICE or 'marin'; "
        "elevenlabs=$ELEVENLABS_VOICE_ID."
    ))
    parser.add_argument("--model", help=(
        "Model id. Defaults: openai=$OPENAI_TTS_MODEL or 'gpt-4o-mini-tts'; "
        "elevenlabs=$ELEVENLABS_MODEL or 'eleven_multilingual_v2'."
    ))
    parser.add_argument("--format", help=(
        "Provider response format. If omitted, inferred from output extension. "
        "OpenAI: mp3|opus|aac|flac|wav|pcm. "
        "ElevenLabs: mp3_44100_128|pcm_44100|ulaw_8000|wav_44100|..."
    ))
    parser.add_argument("--speed", type=float, help="Speaking speed (0.25-4.0 for OpenAI; 0.7-1.2 typical for ElevenLabs)")
    parser.add_argument("--instructions", help="OpenAI: speaking-style guidance (gpt-4o-mini-tts only)")

    # ElevenLabs-specific tuning
    parser.add_argument("--stability", type=float, default=0.5,
                        help="ElevenLabs voice_settings.stability (default: 0.5)")
    parser.add_argument("--similarity-boost", type=float, default=0.75,
                        help="ElevenLabs voice_settings.similarity_boost (default: 0.75)")
    parser.add_argument("--style", type=float, default=None,
                        help="ElevenLabs voice_settings.style (0.0-1.0)")
    parser.add_argument("--no-speaker-boost", action="store_true",
                        help="ElevenLabs: disable use_speaker_boost (default: enabled)")
    parser.add_argument("--seed", type=int, help="ElevenLabs: best-effort deterministic seed")
    parser.add_argument("--language-code", help="ElevenLabs: force a language code (e.g. fr, es)")

    # Discovery commands
    parser.add_argument("--list-voices", action="store_true", help="List available voices for --source and exit")
    parser.add_argument("--list-models", action="store_true", help="List TTS-capable models for --source and exit")

    # ElevenLabs Voice Library (shared voices)
    parser.add_argument("--search-shared", nargs="?", const="", default=None, metavar="QUERY",
                        help="ElevenLabs: search the public Voice Library. "
                             "Pass a query string, or use alone with filters (--gender, --accent, ...)")
    parser.add_argument("--shared-page-size", type=int, default=20,
                        help="ElevenLabs --search-shared page size (default: 20)")
    parser.add_argument("--gender", help="ElevenLabs --search-shared filter: male|female|neutral")
    parser.add_argument("--age", help="ElevenLabs --search-shared filter: young|middle_aged|old")
    parser.add_argument("--accent", help="ElevenLabs --search-shared filter (e.g. american, british, australian)")
    parser.add_argument("--language", help="ElevenLabs --search-shared filter (e.g. en, fr, es)")
    parser.add_argument("--category", help="ElevenLabs --search-shared filter (e.g. professional, high_quality, famous)")
    parser.add_argument("--use-cases", help="ElevenLabs --search-shared filter (e.g. narration, social_media, characters)")
    parser.add_argument("--descriptives", help="ElevenLabs --search-shared filter (e.g. warm, calm, energetic)")
    parser.add_argument("--featured", action="store_true", help="ElevenLabs --search-shared: only featured voices")
    parser.add_argument("--add-voice", metavar="PUBLIC_OWNER_ID:VOICE_ID",
                        help="ElevenLabs: add a shared voice to your account. Requires --new-name.")
    parser.add_argument("--new-name", help="ElevenLabs: name to assign when --add-voice")

    args = parser.parse_args()

    # Discovery short-circuits
    if args.list_voices:
        try:
            if args.source == "openai":
                list_openai_voices()
            else:
                list_elevenlabs_voices()
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        return

    if args.list_models:
        try:
            if args.source == "openai":
                list_openai_models()
            else:
                list_elevenlabs_models()
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        return

    if args.search_shared is not None:
        if args.source != "elevenlabs":
            parser.error("--search-shared is only supported for --source elevenlabs")
        try:
            search_elevenlabs_shared(
                query=args.search_shared or None,
                page_size=args.shared_page_size,
                gender=args.gender,
                age=args.age,
                accent=args.accent,
                language=args.language,
                category=args.category,
                use_cases=args.use_cases,
                descriptives=args.descriptives,
                featured=args.featured,
            )
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        return

    if args.add_voice:
        if args.source != "elevenlabs":
            parser.error("--add-voice is only supported for --source elevenlabs")
        if ":" not in args.add_voice:
            parser.error("--add-voice expects PUBLIC_OWNER_ID:VOICE_ID")
        if not args.new_name:
            parser.error("--add-voice requires --new-name")
        public_user_id, voice_id = args.add_voice.split(":", 1)
        try:
            add_elevenlabs_shared_voice(public_user_id, voice_id, args.new_name)
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        return

    # Generation requires text + output
    if not args.output:
        parser.error("--output is required for generation")
    if not args.text and not args.text_file:
        parser.error("either --text or --text-file is required for generation")

    text = args.text
    if args.text_file:
        text = Path(args.text_file).read_text(encoding="utf-8").strip()
    if not text:
        parser.error("text is empty")

    output_path = Path(args.output).resolve()
    response_format = resolve_format(args.source, output_path, args.format)

    try:
        if args.source == "openai":
            voice = args.voice or os.environ.get("OPENAI_TTS_VOICE") or "marin"
            model = args.model or os.environ.get("OPENAI_TTS_MODEL") or "gpt-4o-mini-tts"
            if len(text) > 4096:
                print(
                    f"Warning: input is {len(text)} chars; OpenAI's documented max is 4096.",
                    file=sys.stderr,
                )
            generate_openai(
                text=text,
                output_path=output_path,
                voice=voice,
                model=model,
                response_format=response_format,
                speed=args.speed,
                instructions=args.instructions,
            )
        else:
            voice = args.voice or os.environ.get("ELEVENLABS_VOICE_ID")
            if not voice:
                parser.error(
                    "ElevenLabs requires --voice (a voice_id) or "
                    "ELEVENLABS_VOICE_ID in .env. Run with --list-voices to discover ids."
                )
            model = args.model or os.environ.get("ELEVENLABS_MODEL") or "eleven_multilingual_v2"
            generate_elevenlabs(
                text=text,
                output_path=output_path,
                voice=voice,
                model=model,
                output_format=response_format,
                stability=args.stability,
                similarity_boost=args.similarity_boost,
                style=args.style,
                speed=args.speed,
                use_speaker_boost=not args.no_speaker_boost,
                seed=args.seed,
                language_code=args.language_code,
            )
    except Exception as e:
        print(f"\nError [{args.source}]: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
