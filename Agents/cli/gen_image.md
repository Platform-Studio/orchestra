## Image Generation

Generate images using `cli/gen_image.py` in the foundation workspace:

```bash
python3 cli/gen_image.py --prompt "description" --source SOURCE --output "path/to/file.png" [--size WxH] [--count N] [--model MODEL]
```

**Sources** (pick based on need):
- `openai` — DALL-E 3, best for illustrations and custom conceptual art (~$0.04/image)
- `fal` — Flux, fast photorealistic generation (~$0.003/image). Supports any fal.ai model.
- `pexels` — Free stock photo search, good for lifestyle/workplace photography
- `pixabay` — Free stock photo search, good for supplementary imagery

**Model override:** Use `--model` to specify a model (fal or openai sources only). Defaults: fal → `fal-ai/flux/schnell`, openai → `dall-e-3`.
You can find the list of available fal models here: https://fal.ai/models

Examples:
- `--source fal --model fal-ai/flux-pro/v1.1` — Flux Pro (higher quality, ~$0.05/image)
- `--source fal --model fal-ai/flux/dev` — Flux Dev (balanced quality/cost)
- `--source fal --model fal-ai/recraft-v3` — Recraft V3 (design-focused)
- `--source openai --model dall-e-3` — DALL-E 3 (default)

**Common sizes:** 1200x628 (Facebook/LinkedIn), 1080x1080 (Instagram square), 1080x1920 (Stories/TikTok), 1280x720 (YouTube thumbnail)

**Output path:** You decide the full path and filename based on what you're creating. Use descriptive names (e.g. `hero_banner.png`, `persona_laura_ad.jpg`).

**Multiple options:** Use `--count 3` to get variants. Files are saved as `name_1.png`, `name_2.png`, etc.

A manifest (`image_manifest.json`) is auto-created alongside outputs tracking prompts, sources, and attribution.
