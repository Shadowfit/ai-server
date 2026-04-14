"""Generate a representative squat reference sequence from a guide video."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.core.reference_builder import build_reference_sequence


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a squat reference sequence.")
    parser.add_argument("video", help="Path to the guide squat video")
    parser.add_argument(
        "--output",
        default="reference_data/squat_reference.json",
        help="Path to write the generated reference JSON",
    )
    parser.add_argument(
        "--target-length",
        type=int,
        default=30,
        help="Normalized frame count per repetition",
    )
    parser.add_argument(
        "--max-reps",
        type=int,
        default=5,
        help="Maximum number of high-quality reps used to build the reference",
    )
    args = parser.parse_args()

    reference_payload = build_reference_sequence(
        args.video,
        target_length=args.target_length,
        max_reps=args.max_reps,
    )

    output_path = Path(args.output).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(reference_payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print("=== 기준 자세 생성 완료 ===")
    print(f"입력 영상: {Path(args.video).resolve()}")
    print(f"감지된 총 반복 수: {reference_payload['total_reps_detected']}")
    print(f"기준 생성에 사용한 반복 수: {reference_payload['selected_rep_count']}")
    print(f"저장 위치: {output_path}")


if __name__ == "__main__":
    main()
