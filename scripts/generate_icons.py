from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bot.cards import _render_art
from bot.journal import Journal
from bot.persist import load_bundle, save_bundle
from bot.shapes import ImageOptions

OUT_DIR = "docs/icons"
STORE_DIR = "data/trading-bot"

STYLE = (
    "App icon, square, one centered bold symbol with generous padding, "
    "dark navy background #070B14, signal blue #5B8CFF to violet #8B5CF6 gradient, "
    "one amber #F5B83D accent, flat vector, minimal, high contrast, "
    "recognizable at 16 pixels, no text, no letters, no numbers, no watermark, no frame"
)

CONCEPTS = (
    (
        "marketwingman-icon",
        "Wing Chart (project icon)",
        "a stylized bird wing whose layered feathers become ascending candlestick bars, "
        "the wing sweeping up to the right as a rising market chart",
    ),
)


def main() -> int:
    now = datetime.now(timezone.utc)
    journal = Journal()
    options = ImageOptions(size="1024x1024", response_format="b64_json", output_format="png")
    os.makedirs(OUT_DIR, exist_ok=True)
    manifest = []
    for slug, title, subject in CONCEPTS:
        prompt = f"{subject}, {STYLE}"
        rendered = _render_art(
            prompt, "icon_card", {"concept": slug, "title": title},
            journal, now, "", None, None, options,
        )
        if rendered is None:
            print(f"{slug}: skipped (see journal)")
            manifest.append({"slug": slug, "title": title, "prompt": prompt, "status": "skipped"})
            continue
        art = rendered.get("art")
        if art is None and rendered.get("url"):
            with urllib.request.urlopen(str(rendered["url"]), timeout=60) as response:
                art = response.read()
        if art is None:
            print(f"{slug}: no image bytes")
            manifest.append({"slug": slug, "title": title, "prompt": prompt, "status": "empty"})
            continue
        path = os.path.join(OUT_DIR, f"{slug}.png")
        with open(path, "wb") as handle:
            handle.write(art)
        manifest.append({
            "slug": slug, "title": title, "prompt": prompt, "status": "ok",
            "file": path, "bytes": len(art), "art_hash": rendered["art_hash"],
            "model": "muse-image-1.0",
        })
        print(f"{slug}: {path} ({len(art)} bytes)")
    with open(os.path.join(OUT_DIR, "manifest.json"), "w", encoding="utf-8") as handle:
        json.dump({"generated_utc": now.isoformat(), "concepts": manifest}, handle, indent=2)
    if os.path.isdir(STORE_DIR):
        store, bundle_journal = load_bundle(STORE_DIR)
        added = bundle_journal.merge(journal)
        save_bundle(STORE_DIR, store, bundle_journal)
        print(f"journal: merged {added} entries into {STORE_DIR}")
    ok = sum(1 for item in manifest if item["status"] == "ok")
    print(f"done: {ok}/{len(CONCEPTS)} icons")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
