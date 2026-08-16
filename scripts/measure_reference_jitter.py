"""#234 — 정답지 추출의 비결정성이 «사용자가 받는 점수» 로 얼마가 되는가.

측정 결과는 loadtest/results/reference-jitter-2026-08-16/README.md 에 있다 (답: 0.28점).
다시 돌리는 법도 거기 있다. 이 스크립트는 #233(«폭이 영상마다 다른가»)에서 영상만 바꿔
그대로 재사용하기 위해 rig 으로 승격됐다.

설계는 이슈가 적어둔 그대로다:
  ① 같은 영상을 N회 추출한다 → 서로 다른 정답지 N벌 (#224 가 확인한 비결정성)
  ② 같은 사용자 rep 시퀀스 하나를 고정한다
  ③ N벌 각각에 대해 compute_sync_rate 를 돌린다 → 점수 N개
  ④ 그 분산이 답이다

정답지는 «최고 점수 rep» 이다(V4__seed_squat_reference.sql 이 쓴 방법과 같다).
사용자 입력은 «정답지로 뽑히지 않은 다른 rep» 을 첫 판에서 뽑아 고정한다.

🔴 이슈가 미리 적어둔 주의: DTW 거리는 «각도 값 차이» 와 «시퀀스 길이 차이» 를 같이 탄다.
   정답지 프레임 수가 35~37 로 흔들리므로 그 자체가 거리에 들어간다. 이 실험은 둘을 가르지
   않는다 — 사용자가 실제로 받는 점수의 폭이 궁금한 것이라, 섞인 채로 재는 것이 맞다.
   가르는 것은 별개 설계다.
"""

from __future__ import annotations

import argparse
import statistics
import sys

from app.core.dtw_calculator import compute_dtw_distance, compute_sync_rate
from app.core.reference_builder import _segment_reps
from app.core.video_processor import analyze_video


def extract_reps(video: str):
    """한 판 추출 → rep 목록 (점수 내림차순)."""
    result = analyze_video(video, "squat")
    frames = result.frames if hasattr(result, "frames") else result.get("frames")
    reps = _segment_reps(frames)
    return sorted(reps, key=lambda r: r.score, reverse=True)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--video", required=True)
    p.add_argument("--runs", type=int, default=5)
    args = p.parse_args()

    references = []   # (run, score, frame_count, min_knee, angles)
    user_seq = None
    user_desc = ""

    for run in range(1, args.runs + 1):
        reps = extract_reps(args.video)
        if not reps:
            print(f"[{run}] rep 0개 — 중단")
            return 1
        best = reps[0]
        references.append((run, best.score, best.frame_count, best.min_knee_angle, best.angles))
        print(f"[{run}] 정답지 후보: score={best.score} frames={best.frame_count} "
              f"min_knee={best.min_knee_angle} (rep {len(reps)}개 중 1위)")

        if user_seq is None:
            # 사용자 입력은 첫 판에서 «정답지로 안 뽑힌» rep 을 골라 고정한다.
            others = reps[1:]
            if not others:
                print("🔴 rep 이 1개뿐이라 사용자 입력을 같은 영상에서 못 고른다")
                return 1
            u = others[0]
            user_seq = u.angles
            user_desc = (f"rep_index={u.rep_index} score={u.score} "
                         f"frames={u.frame_count} min_knee={u.min_knee_angle}")

    print()
    print(f"고정한 사용자 rep : {user_desc}")
    print(f"정답지 {len(references)}벌 각각으로 같은 사용자 rep 을 채점한다")
    print()
    print(f"  {'run':<5}{'ref score':>10}{'ref frames':>12}{'DTW 거리':>12}{'sync_rate':>12}")

    rates, dists = [], []
    for run, score, fc, _mk, ref_angles in references:
        d = compute_dtw_distance(ref_angles, user_seq)
        s = compute_sync_rate(ref_angles, user_seq)
        rates.append(s)
        dists.append(d)
        print(f"  {run:<5}{score:>10}{fc:>12}{d:>12.4f}{s:>12.2f}")

    print()
    ref_scores = [r[1] for r in references]
    print("=== 결과 ===")
    print(f"  정답지 score 폭   : {min(ref_scores):.2f} ~ {max(ref_scores):.2f} "
          f"(폭 {max(ref_scores) - min(ref_scores):.2f})")
    print(f"  정답지 프레임 폭  : {min(r[2] for r in references)} ~ {max(r[2] for r in references)}")
    print(f"  DTW 거리          : {min(dists):.4f} ~ {max(dists):.4f}")
    print(f"  🔴 sync_rate      : {min(rates):.2f} ~ {max(rates):.2f} "
          f"(폭 {max(rates) - min(rates):.2f}점)")
    if len(rates) > 1:
        print(f"     sd             : {statistics.stdev(rates):.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
