"""Analyze a squat demo video and print a concise summary."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.core.video_processor import analyze_video


def main() -> None:
    parser = argparse.ArgumentParser(description="Run squat analysis on a local video.")
    parser.add_argument("video", help="Path to the local demo video file")
    parser.add_argument(
        "--output",
        help="Optional path to write the full JSON analysis result",
    )
    args = parser.parse_args()

    result = analyze_video(args.video, "squat")
    payload = result.model_dump()

    summary = payload.get("squat_analysis") or {}
    print("=== Squat Demo Summary ===")
    print(f"Video: {Path(args.video).resolve()}")
    print(f"Detected reps: {summary.get('reps_detected', 0)}")
    print(f"Current phase: {summary.get('current_phase', 'unknown')}")
    print(f"Quality score: {summary.get('quality_score', 0)}")
    print(f"Feedback: {', '.join(summary.get('feedback', []))}")

    if args.output:
        output_path = Path(args.output).resolve()
        output_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"Full analysis saved to: {output_path}")


if __name__ == "__main__":
    main()
