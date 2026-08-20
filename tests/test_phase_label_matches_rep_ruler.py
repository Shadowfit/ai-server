"""국면 이름표와 rep 판정이 **같은 자**를 쓰는가 (이슈 #218).

`phase` 는 내부값이 아니라 응답 스키마에 나간다(`models/video.py:15,33`). 예전에는 이름표만
95/155 상수였고 rep 판정은 100/150 이라, 두 밴드에서 응답이 자기모순이었다:

| 무릎 각도 | rep 상태기계 | 옛 이름표 | 결과 |
|---|---|---|---|
| 150~155° | `standing` → **rep 을 센다** | `ascending`/`transition` | rep 은 올랐는데 국면은 «아직 올라가는 중» |
| 95~100° | `bottom` | `descending`/`transition` | 바닥에 닿았는데 «아직 내려가는 중» |

어느 쪽을 고칠지는 정해져 있다 — **rep 자를 움직이면 그동안의 측정·기준이 전부 흔들린다.**
그래서 이름표를 rep 자에 맞췄고, 같은 값을 두 군데 적는 대신 **호출부가 rep 판정에 쓰는 그
변수를 그대로 넘긴다.** 아래 마지막 테스트가 그 «구조적으로 못 갈린다» 를 고정한다.
"""
import unittest

from app.core.squat_analyzer import _phase_from_angles, analyze_squat_frames

from tests.test_squat_analyzer import _frame

_BOTTOM = 100.0
_STANDING = 150.0


def _phase(angle: float, delta: float, bottom=_BOTTOM, standing=_STANDING) -> str:
    return _phase_from_angles(
        angle, delta, bottom_threshold=bottom, standing_threshold=standing
    )


class PhaseLabelMatchesRepRulerTest(unittest.TestCase):
    def test_150_에서_155_밴드가_standing_이다(self) -> None:
        """rep 을 세는 구간인데 «아직 올라가는 중» 으로 나가던 자리."""
        for angle in (150.0, 152.0, 154.9):
            with self.subTest(angle=angle):
                self.assertEqual(
                    _phase(angle, +5.0),
                    "standing",
                    f"{angle}° 는 rep 상태기계가 standing 으로 보는 각도다",
                )

    def test_95_에서_100_밴드가_bottom_이다(self) -> None:
        for angle in (95.1, 97.0, 100.0):
            with self.subTest(angle=angle):
                self.assertEqual(_phase(angle, -5.0), "bottom")

    def test_밴드_밖은_속도로_가른다(self) -> None:
        """delta 문턱(±4)은 각도 축이 아니라 속도 축이라 그대로 둔다."""
        self.assertEqual(_phase(120.0, -5.0), "descending")
        self.assertEqual(_phase(120.0, +5.0), "ascending")
        self.assertEqual(_phase(120.0, 0.0), "transition")

    def test_옛_상수였다면_어긋난다는_것을_증인으로_남긴다(self) -> None:
        """이 테스트들이 «고쳐진 뒤에도 통과하는 무의미한 단언» 이 아님을 보인다.

        위 두 밴드 테스트는 지금 코드에서 당연히 통과한다. 문제의 밴드가 **실재했다** 는 것은
        옛 자(95/155)를 명시적으로 넘겨봐야 보인다 — 같은 각도가 다른 이름표를 받는다.
        """
        self.assertEqual(_phase(152.0, +5.0, standing=155.0), "ascending")  # 옛 이름표
        self.assertEqual(_phase(152.0, +5.0, standing=_STANDING), "standing")  # rep 자
        self.assertEqual(_phase(97.0, -5.0, bottom=95.0), "descending")  # 옛 이름표
        self.assertEqual(_phase(97.0, -5.0, bottom=_BOTTOM), "bottom")  # rep 자

    def test_자를_바꾸면_이름표가_따라온다(self) -> None:
        """호출자가 문턱을 덮으면 이름표도 같이 움직여야 한다 — 상수였으면 안 따라온다."""
        self.assertEqual(_phase(140.0, +5.0, standing=135.0), "standing")
        self.assertEqual(_phase(140.0, +5.0, standing=160.0), "ascending")

    def test_전_프레임에서_이름표와_rep_자가_안_어긋난다(self) -> None:
        """실제 분석 경로로 돌려 «자기모순 프레임이 0개» 를 고정한다.

        이게 이 파일의 본체다 — 위 단위 테스트들은 함수 하나를 보지만, 여기서는
        `analyze_squat_frames` 가 실제로 내보내는 프레임 전부를 훑는다.
        """
        # 서 있다 → 바닥 → 다시 서기. 경계 밴드(95~100 · 150~155)를 반드시 지나가게 짠다.
        angles = [
            170.0, 160.0, 154.0, 148.0, 130.0, 110.0, 99.0, 92.0,
            99.0, 110.0, 130.0, 148.0, 152.0, 160.0, 170.0,
        ]
        frames = [_frame(a) for a in angles]

        frame_metrics, result = analyze_squat_frames(
            frames, bottom_threshold=_BOTTOM, standing_threshold=_STANDING
        )

        self.assertEqual(result.reps_detected, 1, "전제: 이 궤적은 rep 1회다")

        mismatches = []
        for index, metric in enumerate(frame_metrics):
            if metric is None:
                continue
            knee = metric.knee_angle
            # rep 상태기계가 쓰는 판정을 그대로 다시 적는다 — 이름표가 그 판정과 같아야 한다.
            if knee <= _BOTTOM and metric.phase != "bottom":
                mismatches.append((index, knee, metric.phase, "bottom"))
            if knee >= _STANDING and metric.phase != "standing":
                mismatches.append((index, knee, metric.phase, "standing"))

        self.assertEqual(
            mismatches,
            [],
            "rep 판정과 국면 이름표가 어긋난 프레임이 있다 "
            "(index, knee, 이름표, 기대): " + repr(mismatches),
        )


if __name__ == "__main__":
    unittest.main()
