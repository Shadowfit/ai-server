"""논문을 참고한 스쿼트 주기 판정. 실시간/영상 분석이 이 구현을 공유한다.

근거: Sousa et al. (2026), doi:10.3390/jfmk11020162, Listing 1.
시간 후처리 원칙: Postlmayr et al. (2024), doi:10.1016/j.smhl.2024.100516.
전체 논문 알고리즘의 재현이 아니며 추가 설정의 출처·한계는
docs/decisions/squat-repetition-counting.md에 구분한다.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace

from app.models.pose import Landmark


@dataclass(frozen=True)
class SquatThresholds:
    # MDL Listing 1의 기본 각도. 2D 카메라 오차를 위한 여유값은 아래에 분리한다.
    standing_knee: float = 160.0
    standing_hip: float = 160.0
    bottom_knee: float = 100.0
    bottom_hip: float = 150.0
    standing_knee_margin: float = 8.0
    initial_hip_margin: float = 10.0
    bottom_knee_margin: float = 15.0
    bottom_dwell_margin: float = 5.0  # 경계 통과 구간을 바닥 체류 시간에서 제외
    min_hip_drop: float = 0.25  # 선 자세 몸통 길이에 대한 골반 하강량
    hip_return_tolerance: float = 0.30
    ankle_outlier_grace_sec: float = 0.7
    # PersonalPT의 400ms 유효 동작 / 200ms 분리 원칙을 FSM에 적용.
    # 그 논문의 GMM 상태와 여기의 동작 구간은 같지 않다.
    min_active_sec: float = 0.4
    standing_confirm_sec: float = 0.2
    # 아래 값은 논문 검증값이 아닌 조정 가능한 구현 기본값이다.
    bottom_confirm_sec: float = 0.12
    max_bottom_sec: float = 8.0  # Engineering guard against seated rests; not a paper threshold.
    max_gap_sec: float = 1.2
    smoothing_sec: float = 0.12
    visibility: float = 0.55
    ankle_tolerance: float = 0.35  # 준비 자세 몸통 길이의 배수
    phase_velocity: float = 12.0  # 도/초; 표시용이며 카운트 조건은 아님

    def __post_init__(self):
        if not (0 < self.bottom_knee < self.standing_knee <= 180
                and 0 < self.bottom_hip < self.standing_hip <= 180):
            raise ValueError("squat angle thresholds must be ordered in (0, 180]")
        values = (self.min_active_sec, self.standing_confirm_sec, self.bottom_confirm_sec,
                  self.max_bottom_sec, self.max_gap_sec, self.smoothing_sec,
                  self.ankle_tolerance, self.phase_velocity, self.standing_knee_margin,
                  self.initial_hip_margin, self.bottom_knee_margin, self.bottom_dwell_margin,
                  self.min_hip_drop,
                  self.hip_return_tolerance, self.ankle_outlier_grace_sec)
        if not all(math.isfinite(v) and v > 0 for v in values) or not 0 <= self.visibility <= 1:
            raise ValueError("invalid squat timing/visibility thresholds")
        if (self.standing_knee - self.standing_knee_margin <=
                self.bottom_knee + self.bottom_knee_margin):
            raise ValueError("squat standing and bottom knee bands must not overlap")
        if self.bottom_dwell_margin >= self.bottom_knee:
            raise ValueError("bottom dwell margin must be smaller than bottom knee angle")


# 하프 스쿼트 프리셋 (2026-09-21 사용자 확정: 무릎각 115~140도 밴드).
# bottom_knee=125 + margin(15, 풀 스쿼트와 동일 — 이 margin은 2D 랜드마크 잡음 흡수용이지
# 목표 깊이와 무관하므로 모드별로 바꾸지 않는다) = 실질 진입 각도 140도.
#
# 🔴 bottom_hip·min_hip_drop은 실측이 아니라 풀 스쿼트 값에서 비례 추정한 값이다(VALIDATE).
#    풀 스쿼트의 무릎:고관절 굴곡은 서기(160/160)에서 바닥(100/150)까지 각각 60도/10도이고,
#    이 비율(10/60)을 하프 스쿼트의 무릎 굴곡 35도(160→125)에 적용하면
#    고관절 굴곡 ≈ 5.8도 → bottom_hip ≈ 154도. min_hip_drop도 같은 방식으로
#    0.25 × (35/60) ≈ 0.15로 비례 추정했다. 실촬영 하프 스쿼트 영상으로 재검증하기 전까지는
#    이 두 값을 신뢰할 수 있는 기준으로 취급하지 않는다 — 2026-09-21 검증 보고서와 같은 방식으로
#    docs/verification/에 별도 검증 문서를 남긴 뒤에만 사용자에게 정확도를 주장한다.
HALF_SQUAT_THRESHOLDS: SquatThresholds = replace(
    SquatThresholds(),
    bottom_knee=125.0,
    bottom_hip=154.0,   # VALIDATE
    min_hip_drop=0.15,  # VALIDATE
)

# 세션의 depth_mode 문자열 → 임계값 프리셋. 인식하지 못하는 문자열은 호출부가 "FULL"로
# 취급한다(app/grpc/exercise_servicer.py의 StartAnalysis/ReattachAnalysis 참고).
DEPTH_MODE_THRESHOLDS: dict[str, SquatThresholds] = {
    "FULL": SquatThresholds(),
    "HALF": HALF_SQUAT_THRESHOLDS,
}


@dataclass(frozen=True)
class SquatObservation:
    knees: tuple[float, float]
    hips: tuple[float, float]
    torso_tilt: float
    hip_height: float
    ankles: tuple[tuple[float, float] | None, tuple[float, float] | None]
    torso_length: float
    visible_sides: tuple[bool, bool]

    @property
    def knee_angle(self) -> float:
        return sum(self.knees) / 2

    @property
    def hip_angle(self) -> float:
        return sum(self.hips) / 2


def _angle(a, b, c):
    u, v = (a[0] - b[0], a[1] - b[1]), (c[0] - b[0], c[1] - b[1])
    norm = math.hypot(*u) * math.hypot(*v)
    if norm < 1e-8:
        raise ValueError("degenerate joint")
    return math.degrees(math.acos(max(-1., min(1., (u[0] * v[0] + u[1] * v[1]) / norm))))


def observe_squat(landmarks: list[Landmark], aspect_ratio: float, visibility: float) -> SquatObservation:
    """이미지 비율을 보정한 2D 내각. 정규화 z를 물리적 깊이로 섞지 않는다."""
    if not math.isfinite(aspect_ratio) or aspect_ratio <= 0:
        raise ValueError("invalid image aspect ratio")
    lm = {p.index: p for p in landmarks}
    chains = ((11, 23, 25, 27), (12, 24, 26, 28))
    valid: list[tuple[int, float, float]] = []
    p: dict[int, tuple[float, float]] = {}
    for side, chain in enumerate(chains):
        if any(i not in lm or not all(math.isfinite(v) for v in
               (lm[i].x, lm[i].y, lm[i].visibility)) or
               lm[i].visibility < visibility for i in chain):
            continue
        for i in chain:
            p[i] = (lm[i].x * aspect_ratio, lm[i].y)
        try:
            knee = _angle(p[chain[1]], p[chain[2]], p[chain[3]])
            hip = _angle(p[chain[0]], p[chain[1]], p[chain[2]])
        except ValueError:
            continue
        valid.append((side, knee, hip))
    if not valid:
        raise ValueError("no visible leg chain")

    shoulders = tuple(sum(p[chains[side][0]][axis] for side, _, _ in valid) / len(valid)
                      for axis in (0, 1))
    hips = tuple(sum(p[chains[side][1]][axis] for side, _, _ in valid) / len(valid)
                 for axis in (0, 1))
    torso_length = math.dist(shoulders, hips)
    if torso_length < 1e-8:
        raise ValueError("degenerate torso")
    knees = {side: knee for side, knee, _ in valid}
    hip_angles = {side: hip for side, _, hip in valid}
    if len(valid) == 1:
        side = valid[0][0]
        knees[1 - side] = knees[side]
        hip_angles[1 - side] = hip_angles[side]
    visible_sides = {side for side, _, _ in valid}
    return SquatObservation(
        knees=(knees[0], knees[1]), hips=(hip_angles[0], hip_angles[1]),
        torso_tilt=abs(math.degrees(math.atan2(shoulders[0] - hips[0], hips[1] - shoulders[1]))),
        hip_height=hips[1], ankles=(p.get(27), p.get(28)), torso_length=torso_length,
        visible_sides=(0 in visible_sides, 1 in visible_sides),
    )


@dataclass
class SquatCounter:
    thresholds: SquatThresholds = field(default_factory=SquatThresholds)
    rep_count: int = 0
    stage: str = "waiting_for_standing"
    phase: str = "unknown"
    guidance: str = "정면 기준 45도에서 머리부터 발목까지 보이도록 똑바로 서주세요."
    last_time: float | None = None
    standing_since: float | None = None
    started_at: float | None = None
    bottom_since: float | None = None
    bottom_band_since: float | None = None
    filtered: SquatObservation | None = None
    anchors: tuple = ()
    scale: float = 1.0
    top_hip_height: float | None = None
    attempt_max_hip_drop: float = 0.0
    coaching_event: str | None = None
    ankle_outlier_since: float | None = None
    tracked_sides: tuple[bool, bool] = (False, False)

    def invalidate(self, message="영상이 끊겼습니다. 똑바로 서서 다시 준비해주세요."):
        # 완성된 횟수와 시각 단조성은 보존한다.
        self.stage, self.phase = "waiting_for_standing", "unknown"
        self.standing_since = self.started_at = self.bottom_since = self.bottom_band_since = None
        self.filtered = None
        self.anchors = ()
        self.top_hip_height = self.ankle_outlier_since = None
        self.attempt_max_hip_drop = 0.0
        self.coaching_event = None
        self.tracked_sides = (False, False)
        self.guidance = message

    def update(self, landmarks: list[Landmark] | None, timestamp_sec: float,
               aspect_ratio: float = 1.0) -> tuple[SquatObservation | None, bool]:
        now, t = timestamp_sec, self.thresholds
        self.coaching_event = None
        if not math.isfinite(now) or (self.last_time is not None and now <= self.last_time):
            return None, False
        dt = now - self.last_time if self.last_time is not None else 0.
        self.last_time = now
        if dt > t.max_gap_sec:
            self.invalidate()
        try:
            raw = observe_squat(landmarks or [], aspect_ratio, t.visibility)
        except ValueError:
            self.invalidate("어깨·골반·무릎·발목이 한쪽 다리에서 모두 보이도록 위치를 조정해주세요.")
            return None, False
        if self.anchors:
            shared = tuple(was and now_visible for was, now_visible in
                           zip(self.tracked_sides, raw.visible_sides))
            if not any(shared):
                self.invalidate("동작 중 추적하는 다리가 바뀌었습니다. 제자리에서 다시 준비해주세요.")
                return raw, False
            # A person may place a foot before the first descent. Keep calibrating
            # the stance only while both leg angles still describe an upright pose.
            # Once descent starts, the foot anchors are fixed for the whole rep.
            if (self.stage == "ready" and
                    raw.knee_angle >= t.standing_knee and
                    raw.hip_angle >= t.standing_hip and
                    self.top_hip_height is not None and
                    raw.hip_height - self.top_hip_height <= t.hip_return_tolerance * self.scale):
                self.anchors, self.scale = raw.ankles, raw.torso_length
                self.ankle_outlier_since = None
            if any(a is not None and b is not None and
                   math.dist(a, b) > t.ankle_tolerance * self.scale
                   for a, b, visible in zip(raw.ankles, self.anchors,
                                            raw.visible_sides) if visible):
                if self.ankle_outlier_since is None:
                    self.ankle_outlier_since = now
                elif now - self.ankle_outlier_since >= t.ankle_outlier_grace_sec:
                    self.invalidate("발 위치가 바뀌었습니다. 제자리에서 다시 준비해주세요.")
                return None, False
            self.ankle_outlier_since = None
            self.tracked_sides = shared

        previous = self.filtered
        if previous is not None:
            alpha = 1 - math.exp(-dt / t.smoothing_sec)
            smooth = lambda before, after: tuple(a + alpha * (b - a) for a, b in zip(before, after))
            raw = replace(raw, knees=smooth(previous.knees, raw.knees),
                          hips=smooth(previous.hips, raw.hips),
                          hip_height=previous.hip_height + alpha * (raw.hip_height - previous.hip_height))
        self.filtered = raw

        knee_straight = raw.knee_angle >= t.standing_knee - t.standing_knee_margin
        if self.top_hip_height is None:
            standing = knee_straight and raw.hip_angle >= t.standing_hip - t.initial_hip_margin
            bottom = False
        else:
            hip_drop = raw.hip_height - self.top_hip_height
            standing = knee_straight and hip_drop <= t.hip_return_tolerance * self.scale
            bottom = (raw.knee_angle <= t.bottom_knee + t.bottom_knee_margin and
                      raw.hip_angle <= t.bottom_hip and
                      hip_drop >= t.min_hip_drop * self.scale)
        velocity = (raw.knee_angle - previous.knee_angle) / dt if previous is not None else 0.
        self.phase = ("standing" if standing else "bottom" if bottom else
                      "descending" if velocity < -t.phase_velocity else
                      "ascending" if velocity > t.phase_velocity else "transition")

        if self.stage == "waiting_for_standing":
            if standing:
                self.stage = "ready"
                self.anchors, self.scale = raw.ankles, raw.torso_length
                self.top_hip_height = raw.hip_height
                self.tracked_sides = raw.visible_sides
                self.guidance = "준비됐습니다. 제자리에서 스쿼트를 시작하세요."
            else:
                self.standing_since = None
            return raw, False

        if self.stage == "ready" and standing:
            self.top_hip_height = 0.9 * self.top_hip_height + 0.1 * raw.hip_height

        if self.stage == "ready" and not standing:
            self.stage, self.started_at = "descending", now
            self.standing_since = self.bottom_since = self.bottom_band_since = None
            self.attempt_max_hip_drop = 0.0
            self.guidance = "편안한 범위에서 무릎과 고관절을 굽혀주세요."

        if self.stage == "descending":
            self.attempt_max_hip_drop = max(
                self.attempt_max_hip_drop, raw.hip_height - self.top_hip_height
            )
            if bottom:
                if self.bottom_since is None:
                    self.bottom_since = now
                elif now - self.bottom_since >= t.bottom_confirm_sec:
                    self.stage = "bottom"
                    self.bottom_band_since, self.bottom_since = self.bottom_since, None
                    self.guidance = "굽힘을 확인했습니다. 다시 똑바로 서주세요."
            else:
                self.bottom_since = None
            if standing:
                if (self.started_at is not None and now - self.started_at >= t.min_active_sec
                        and self.attempt_max_hip_drop >= t.min_hip_drop * self.scale):
                    self.coaching_event = "이번 동작에서 충분히 내려간 장면을 확인하지 못했어요."
                self.stage = "ready"
                self.top_hip_height = raw.hip_height
                self.guidance = "이번 동작은 세지 않았습니다. 준비 자세에서 다시 시작하세요."
            return raw, False

        if self.stage == "bottom":
            if bottom and self.bottom_band_since is None:
                self.bottom_band_since = now
            if bottom and now - self.bottom_band_since > t.max_bottom_sec:
                self.invalidate("오래 앉아 있었습니다. 일어서서 다시 준비해주세요.")
                return raw, False
            if not bottom:
                self.bottom_band_since = None
            strict_bottom = (raw.knee_angle <= t.bottom_knee - t.bottom_dwell_margin and
                             raw.hip_angle <= t.bottom_hip)
            if strict_bottom:
                if self.bottom_since is None:
                    self.bottom_since = now
            else:
                self.bottom_since = None
            if standing:
                if self.standing_since is None:
                    self.standing_since = now
                # update() only runs on sampled frames (e.g. the app's 3fps analysis
                # rate), so a sample gap (dt) already at/beyond the confirm window is
                # itself the finest dwell this input could ever show -- a single
                # standing sample is as much confirmation as two would be. At a
                # fast/continuous feed (dt well under the confirm window, e.g. a live
                # camera stream) this still requires genuine multi-sample dwell,
                # unchanged from before. See squat-repetition-counting.md's
                # 2026-09-21 note for the real clips this was found on.
                if ((now - self.standing_since >= t.standing_confirm_sec
                     or dt >= t.standing_confirm_sec)
                        and now - self.started_at >= t.min_active_sec):
                    self.rep_count += 1
                    self.stage = "ready"
                    self.top_hip_height = raw.hip_height
                    self.anchors, self.scale = raw.ankles, raw.torso_length
                    self.tracked_sides = raw.visible_sides
                    self.standing_since = self.bottom_since = self.bottom_band_since = self.started_at = None
                    self.attempt_max_hip_drop = 0.0
                    self.coaching_event = "스쿼트 깊이를 확인하고 1회 기록했어요."
                    self.guidance = "서 있는 자세로 복귀했습니다. 다음 동작을 시작하세요."
                    return raw, True
            else:
                self.standing_since = None
        return raw, False
