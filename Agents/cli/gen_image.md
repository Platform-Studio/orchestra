## Image Generation

Generate images using `cli/gen_image.py` in the Orchestra repository:

```bash
python3 cli/gen_image.py --prompt "description" --source SOURCE --output "path/to/file.png" [--size WxH] [--count N] [--model MODEL]
```

**Sources** (pick based on need):
- `openai` — GPT Image 2, best for illustrations and custom conceptual art
- `pexels` — Free stock photo search, good for lifestyle/workplace photography
- `pixabay` — Free stock photo search, good for supplementary imagery

**Model override:** Use `--model` to specify an OpenAI image model. Default: `gpt-image-2`.

Examples:
- `--source openai --model gpt-image-2` — GPT Image 2 (default)

**Common sizes:** 1200x628 (Facebook/LinkedIn), 1080x1080 (Instagram square), 1080x1920 (Stories/TikTok), 1280x720 (YouTube thumbnail)

**Output path:** You decide the full path and filename based on what you're creating. Use descriptive names (e.g. `hero_banner.png`, `persona_laura_ad.jpg`).

**Multiple options:** Use `--count 3` to get variants. Files are saved as `name_1.png`, `name_2.png`, etc.

A manifest (`image_manifest.json`) is auto-created alongside outputs tracking prompts, sources, and attribution.
