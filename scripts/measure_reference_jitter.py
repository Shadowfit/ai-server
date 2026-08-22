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
import hashlib
import os
import statistics
import sys

from app.core.dtw_calculator import compute_dtw_distance, compute_sync_rate
from app.core.reference_builder import _segment_reps
from app.core.video_processor import analyze_video


def describe_input(video: str, runs: int) -> str:
    """이 판이 «무엇을》 먹었는지 한 덩어리로 돌려준다.

    🔴 이 함수가 생긴 이유 (#256 착수 중 발견). 이 rig 의 결과 README 는 측정일·rig 경로·
    선행 이슈까지 적으면서 **정작 입력 영상을 안 적었다** — 「다시 돌리는 법」이
    `--video <스쿼트 영상 경로>` 라는 플레이스홀더로 끝난다.

    그래서 2026-08-16 판(#234, 답 0.28점)은 **어떤 영상으로 잰 값인지 알 수 없고, 재현도
    비교도 불가능하다.** 이 rig 이 재는 값(정답지 흔들림)은 영상에 따라 달라지므로 그건
    치명적이다 — 새로 재도 «같은 조건인가» 를 말할 수 없다.

    사람이 결과에 적어주기를 기대하지 않고 **rig 이 스스로 남긴다.** 해시를 쓰는 이유는
    파일명이 같아도 다른 영상일 수 있어서다(재인코딩·자르기).
    """
    path = os.path.abspath(video)
    size = os.path.getsize(path)
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return "\n".join([
        "=== 입력 (결과에 이 블록을 그대로 옮길 것) ===",
        f"  영상      : {path}",
        f"  크기      : {size:,} bytes",
        f"  sha256    : {h.hexdigest()}",
        f"  반복 수   : {runs}",
    ])


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

    if not os.path.isfile(args.video):
        # analyze_video 까지 가면 MediaPipe 안쪽에서 알아보기 어려운 오류가 난다.
        print(f"🔴 영상을 찾을 수 없다: {args.video}")
        return 1

    provenance = describe_input(args.video, args.runs)
    print(provenance)
    print()

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
    print(f"  {'run':<5}{'ref score':>10}{'ref frames':>12}{'ref min_knee':>14}"
          f"{'DTW 거리':>12}{'sync_rate':>12}")

    rates, dists, min_knees = [], [], []
    for run, score, fc, mk, ref_angles in references:
        d = compute_dtw_distance(ref_angles, user_seq)
        s = compute_sync_rate(ref_angles, user_seq)
        rates.append(s)
        dists.append(d)
        min_knees.append(mk)
        print(f"  {run:<5}{score:>10}{fc:>12}{mk:>14.2f}{d:>12.4f}{s:>12.2f}")

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

    # ── #256: 판별 정답지의 min_knee 변동폭 ──
    #
    # feedback-type-detector.md ① 은 「깊이 축(HIP_HIGH) 을 버린다」 를
    # 「세어진 rep ≤ 100°, 정답지 96.5° → 겹치는 폭 3.5° 이고 그건 지터와
    # 구분이 안 된다」 로 세웠는데, 그 「지터 3.5°」 가 미측정이었다.
    # 여기서 나오는 폭이 그 수치의 진위를 정한다 (#256).
    #
    # ⚠️ 재는 것은 「랜드마크 지터」 자체가 아니라 **판별 정답지의
    #    min_knee 변동**이다. 같은 영상을 다시 추출하면 (ㄱ) 랜드마크가 흔들리고
    #    (ㄴ) 그 때문에 「최고 점수 rep」 자체가 다른 rep 으로 바뀌기도 한다.
    #    둘이 섞인 값이다 — 그러나 깊이 판정이 실제로 마주하는 것도 그 섞인
    #    값이라 이쪽이 맞는 질문이다.
    if len(min_knees) > 1:
        mk_span = max(min_knees) - min(min_knees)
        print()
        print(f"  🔴 정답지 min_knee : {min(min_knees):.2f}° ~ {max(min_knees):.2f}° "
              f"(폭 {mk_span:.2f}°)")
        print(f"     sd             : {statistics.stdev(min_knees):.3f}°")
        print("     문서의 3.5° 대조 : "
              + ("폭 ≥ 3.5° — ① 유지 (깊이 축 판정 불가)"
                 if mk_span >= 3.5 else
                 "폭 < 3.5° — 🔴 ① 의 결론이 뒤집힌다 (깊이 축이 살아난다)"))

    # 입력을 끝에서 한 번 더 낸다. 결과를 옮길 때 «꼬리만» 복사하는 일이 흔한데,
    # 그러면 위쪽 입력 블록이 떨어져 나가 이 rig 이 고치려던 문제가 그대로 재발한다.
    print()
    print(provenance)
    return 0


if __name__ == "__main__":
    sys.exit(main())
