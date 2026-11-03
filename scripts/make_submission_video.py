#!/usr/bin/env python3
"""Build and render the hackathon submission video with HyperFrames.

Reads the stored bundle (default data/trading-bot), builds the submission
composition from real stored numbers, writes the HyperFrames project, and
renders submission.mp4 next to it. Rendering needs Node 22+ (npx),
FFmpeg, and headless Chrome.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime, timezone

sys.path.insert(0, ".")

from bot.persist import load_bundle, save_bundle
from bot.spark import latest_spark_audit, spark_audit_response
from bot.video import build_submission_brief, render_video, write_submission_project


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store-dir", default="data/trading-bot")
    parser.add_argument("--symbol", default="SPY")
    parser.add_argument("--project-dir", default="docs/submission-video")
    parser.add_argument("--write-only", action="store_true", help="write the project, skip rendering")
    parser.add_argument(
        "--offline", action="store_true",
        help="skip the live spark audit (deterministic OFF video, honestly labeled)",
    )
    parser.add_argument(
        "--narration-audio", default="",
        help="audio file to mux into submission.mp4 (silent cut kept as submission-silent.mp4)",
    )
    args = parser.parse_args()

    store, journal = load_bundle(args.store_dir)
    if store.read_latest(args.symbol) is None:
        print(f"no snapshot for {args.symbol} in {args.store_dir}; seed it first", file=sys.stderr)
        return 2
    now = datetime.now(timezone.utc)
    if not args.offline:
        receipts_before = sum(1 for entry in journal.entries() if entry.kind == "model_receipt")
        audit = spark_audit_response(
            f"spark audit on {args.symbol}", args.symbol, store, journal, now,
            live_models=True, grounding=True,
        )
        receipts_after = sum(1 for entry in journal.entries() if entry.kind == "model_receipt")
        status, _expected, model_dir, _confidence = latest_spark_audit(journal)
        print(f"spark audit (live, grounded): {audit.splitlines()[0]}")
        if status == "OFF" or receipts_after == receipts_before:
            print(
                "live spark audit did not run (no model receipt journaled); "
                "set META_MODEL_API_KEY + MODEL_DAY_BUDGET_USD or pass --offline",
                file=sys.stderr,
            )
            return 3
        print(f"features on: live_models=True grounding=True model={model_dir} verdict={status}")
    brief = build_submission_brief(args.symbol, store, journal, now)
    save_bundle(args.store_dir, store, journal)
    path = write_submission_project(args.project_dir, brief)
    print(f"project: {path} ({len(brief.scenes)} scenes, {brief.total_seconds}s)")
    for scene in brief.scenes:
        print(f"  {scene.start_seconds:>3}s +{scene.duration_seconds:<3}s {scene.scene_id}: {scene.title}")
    if args.write_only:
        print(f"render: cd {args.project_dir} && npx hyperframes render --output submission.mp4")
        return 0
    result = render_video(args.project_dir, "submission.mp4")
    if not result.ok:
        print(f"render failed: {result.error}", file=sys.stderr)
        return 1
    if args.narration_audio:
        silent = f"{args.project_dir}/submission-silent.mp4"
        narrated = f"{args.project_dir}/submission-narrated.mp4"
        subprocess.run(
            ["cp", f"{args.project_dir}/submission.mp4", silent], check=True
        )
        mux = subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", silent, "-i", args.narration_audio,
             "-c:v", "copy", "-c:a", "aac", "-shortest", narrated],
            capture_output=True, text=True,
        )
        if mux.returncode != 0:
            print(f"narration mux failed: {mux.stderr.strip()[-500:]}", file=sys.stderr)
            return 1
        subprocess.run(["cp", narrated, f"{args.project_dir}/submission.mp4"], check=True)
        print(f"narration muxed: {args.narration_audio} (silent cut: {silent})")
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration,size",
         "-of", "default=noprint_wrappers=1", f"{args.project_dir}/submission.mp4"],
        capture_output=True, text=True,
    )
    print(f"rendered: {args.project_dir}/submission.mp4")
    print(probe.stdout.strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
