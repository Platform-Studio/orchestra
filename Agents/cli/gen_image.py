#!/usr/bin/env python3
"""
Image generation and stock photo CLI for foundation agents.

Usage:
    python gen_image.py --prompt "A professional at a career crossroads" --source openai --output path/to/image.png
    python gen_image.py --prompt "career transition" --source pexels --output assets/hero.jpg --count 3
    python gen_image.py --prompt "welding sparks close-up" --source pixabay --output assets/welding.jpg
    python gen_image.py --prompt "photorealistic office worker" --source fal --output assets/worker.png --size 1080x1080

Sources:
    openai   - DALL-E 3 generation (best for illustrations, conceptual art, custom scenes)
    fal      - Flux generation via fal.ai (fast, photorealistic, cheaper)
    pexels   - Stock photo search (free, lifestyle/workplace photography)
    pixabay  - Stock photo search (free, supplementary imagery)

Common ad sizes:
    1200x628  - Facebook/LinkedIn feed
    1080x1080 - Instagram/Facebook square
    1080x1920 - Instagram/TikTok story
    1200x900  - LinkedIn spotlight
    1280x720  - YouTube thumbnail
    1024x1024 - Default square (DALL-E native)
"""

import argparse
import json
import os
import re
import sys
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

# Load .env from project root (one level up from cli/)
load_dotenv(Path(__file__).parent.parent / ".env")

MANIFEST_NAME = "image_manifest.json"


def parse_size(size_str: str) -> tuple[int, int]:
    """Parse WxH string into (width, height) tuple."""
    match = re.match(r"^(\d+)x(\d+)$", size_str)
    if not match:
        raise ValueError(f"Invalid size format '{size_str}'. Use WxH, e.g. 1024x1024")
    return int(match.group(1)), int(match.group(2))


def download_image(url: str, output_path: Path) -> None:
    """Download an image from a URL to the output path."""
    req = urllib.request.Request(url, headers={"User-Agent": "FoundationImageGen/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        output_path.write_bytes(resp.read())


def numbered_path(base_path: Path, index: int, count: int) -> Path:
    """Return path with _N suffix if count > 1, otherwise the original path."""
    if count <= 1:
        return base_path
    stem = base_path.stem
    return base_path.with_name(f"{stem}_{index + 1}{base_path.suffix}")


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


# --- Source implementations ---

def generate_openai(prompt: str, width: int, height: int, output_path: Path, count: int, model: str | None = None) -> list[Path]:
    """Generate images using OpenAI DALL-E 3."""
    from openai import OpenAI

    model = model or "dall-e-3"

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY not set in .env")

    client = OpenAI(api_key=api_key)

    # DALL-E 3 supports: 1024x1024, 1792x1024, 1024x1792
    # Map requested size to nearest supported size
    if width > height:
        dalle_size = "1792x1024"
    elif height > width:
        dalle_size = "1024x1792"
    else:
        dalle_size = "1024x1024"

    saved = []
    for i in range(count):
        response = client.images.generate(
            model=model,
            prompt=prompt,
            size=dalle_size,
            quality="standard",
            n=1,
        )
        image_url = response.data[0].url
        revised_prompt = response.data[0].revised_prompt

        out = numbered_path(output_path, i, count)
        out.parent.mkdir(parents=True, exist_ok=True)
        download_image(image_url, out)

        # Resize if requested size differs from DALL-E native
        dalle_w, dalle_h = parse_size(dalle_size)
        if (width, height) != (dalle_w, dalle_h):
            resize_image(out, width, height)
        else:
            ensure_image_format(out)

        update_manifest(out, {
            "file": str(out),
            "source": "openai",
            "model": model,
            "prompt": prompt,
            "revised_prompt": revised_prompt,
            "size": f"{width}x{height}",
            "dalle_native_size": dalle_size,
            "created": datetime.now(timezone.utc).isoformat(),
        })
        saved.append(out)
        print(f"[openai] Saved: {out}")
        if revised_prompt and revised_prompt != prompt:
            print(f"  DALL-E revised prompt: {revised_prompt}")

    return saved


def generate_fal(prompt: str, width: int, height: int, output_path: Path, count: int, model: str | None = None) -> list[Path]:
    """Generate images using fal.ai. Supports any fal.ai model identifier."""
    import fal_client

    api_key = os.environ.get("FAL_KEY")
    if not api_key:
        raise RuntimeError("FAL_KEY not set in .env")

    os.environ["FAL_KEY"] = api_key  # fal_client reads from env

    fal_model = model or "fal-ai/flux/schnell"

    saved = []
    result = fal_client.run(
        fal_model,
        arguments={
            "prompt": prompt,
            "image_size": {"width": width, "height": height},
            "num_images": count,
        },
    )

    for i, image_data in enumerate(result["images"]):
        image_url = image_data["url"]
        out = numbered_path(output_path, i, count)
        out.parent.mkdir(parents=True, exist_ok=True)
        download_image(image_url, out)
        ensure_image_format(out)

        update_manifest(out, {
            "file": str(out),
            "source": "fal",
            "model": fal_model,
            "prompt": prompt,
            "size": f"{width}x{height}",
            "created": datetime.now(timezone.utc).isoformat(),
        })
        saved.append(out)
        print(f"[fal] Saved: {out}")

    return saved


def search_pexels(prompt: str, width: int, height: int, output_path: Path, count: int) -> list[Path]:
    """Search and download stock photos from Pexels."""
    api_key = os.environ.get("PEXELS_API_KEY")
    if not api_key:
        raise RuntimeError("PEXELS_API_KEY not set in .env")

    # Determine orientation from dimensions
    if width > height:
        orientation = "landscape"
    elif height > width:
        orientation = "portrait"
    else:
        orientation = "square"

    query = urllib.parse.urlencode({
        "query": prompt,
        "per_page": count,
        "orientation": orientation,
    })
    url = f"https://api.pexels.com/v1/search?{query}"
    req = urllib.request.Request(url, headers={
        "Authorization": api_key,
        "User-Agent": "FoundationImageGen/1.0",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())

    if not data.get("photos"):
        print(f"[pexels] No results for '{prompt}'")
        return []

    saved = []
    for i, photo in enumerate(data["photos"][:count]):
        # Pick best size: original, large2x, large, medium
        photo_url = photo["src"].get("large2x") or photo["src"].get("large") or photo["src"]["original"]

        out = numbered_path(output_path, i, count)
        out.parent.mkdir(parents=True, exist_ok=True)
        download_image(photo_url, out)

        # Resize to exact dimensions
        resize_image(out, width, height)

        update_manifest(out, {
            "file": str(out),
            "source": "pexels",
            "prompt": prompt,
            "photo_id": photo["id"],
            "photographer": photo["photographer"],
            "pexels_url": photo["url"],
            "size": f"{width}x{height}",
            "created": datetime.now(timezone.utc).isoformat(),
        })
        saved.append(out)
        print(f"[pexels] Saved: {out} (by {photo['photographer']})")

    return saved


def search_pixabay(prompt: str, width: int, height: int, output_path: Path, count: int) -> list[Path]:
    """Search and download stock photos from Pixabay."""
    api_key = os.environ.get("PIXABAY_API_KEY")
    if not api_key:
        raise RuntimeError("PIXABAY_API_KEY not set in .env")

    # Determine orientation
    if width > height:
        orientation = "horizontal"
    elif height > width:
        orientation = "vertical"
    else:
        orientation = "horizontal"  # pixabay has no "square"

    query = urllib.parse.urlencode({
        "key": api_key,
        "q": prompt,
        "per_page": max(count, 3),  # Pixabay minimum is 3
        "orientation": orientation,
        "image_type": "photo",
        "safesearch": "true",
    })
    url = f"https://pixabay.com/api/?{query}"
    req = urllib.request.Request(url, headers={"User-Agent": "FoundationImageGen/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())

    if not data.get("hits"):
        print(f"[pixabay] No results for '{prompt}'")
        return []

    saved = []
    for i, hit in enumerate(data["hits"][:count]):
        photo_url = hit.get("largeImageURL") or hit["webformatURL"]

        out = numbered_path(output_path, i, count)
        out.parent.mkdir(parents=True, exist_ok=True)
        download_image(photo_url, out)

        # Resize to exact dimensions
        resize_image(out, width, height)

        update_manifest(out, {
            "file": str(out),
            "source": "pixabay",
            "prompt": prompt,
            "pixabay_id": hit["id"],
            "user": hit["user"],
            "pixabay_url": hit["pageURL"],
            "size": f"{width}x{height}",
            "tags": hit.get("tags", ""),
            "created": datetime.now(timezone.utc).isoformat(),
        })
        saved.append(out)
        print(f"[pixabay] Saved: {out} (by {hit['user']})")

    return saved


def ensure_image_format(path: Path) -> None:
    """Re-save image so the file format matches the extension."""
    from PIL import Image

    ext_to_format = {
        ".png": "PNG",
        ".jpg": "JPEG",
        ".jpeg": "JPEG",
        ".webp": "WEBP",
    }
    expected_format = ext_to_format.get(path.suffix.lower())
    if not expected_format:
        return

    with Image.open(path) as img:
        if img.format == expected_format:
            return
        save_img = img
        if expected_format == "JPEG" and img.mode in ("RGBA", "P"):
            save_img = img.convert("RGB")
        save_img.save(path, format=expected_format)


def resize_image(path: Path, width: int, height: int) -> None:
    """Resize an image to exact dimensions using Pillow."""
    from PIL import Image

    with Image.open(path) as img:
        if img.size == (width, height):
            return
        resized = img.resize((width, height), Image.LANCZOS)
        resized.save(path)


# --- Main ---

SOURCE_HANDLERS = {
    "openai": generate_openai,
    "fal": generate_fal,
    "pexels": search_pexels,
    "pixabay": search_pixabay,
}


def main():
    parser = argparse.ArgumentParser(
        description="Generate or find images for foundation agents.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--prompt", required=True, help="Image description or search query")
    parser.add_argument("--source", required=True, choices=SOURCE_HANDLERS.keys(),
                        help="Image source: openai, fal, pexels, pixabay")
    parser.add_argument("--output", required=True, help="Output file path (e.g. assets/hero.png)")
    parser.add_argument("--size", default="1024x1024", help="Image dimensions as WxH (default: 1024x1024)")
    parser.add_argument("--count", type=int, default=1, help="Number of images to generate/find (default: 1)")
    parser.add_argument("--model", default=None,
                        help="Model to use (e.g. fal-ai/flux/schnell, fal-ai/flux-pro/v1.1, dall-e-3). "
                             "Defaults: fal → fal-ai/flux/schnell, openai → dall-e-3")

    args = parser.parse_args()

    width, height = parse_size(args.size)
    output_path = Path(args.output).resolve()

    handler = SOURCE_HANDLERS[args.source]
    # Pass model to sources that support it
    handler_kwargs = {}
    if args.model and args.source in ("fal", "openai"):
        handler_kwargs["model"] = args.model
    try:
        saved = handler(args.prompt, width, height, output_path, args.count, **handler_kwargs)
        if saved:
            print(f"\nDone. {len(saved)} image(s) saved.")
        else:
            print("\nNo images were saved.")
            sys.exit(1)
    except Exception as e:
        print(f"\nError [{args.source}]: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
