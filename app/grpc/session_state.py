"""sessionId별 in-memory 운동 분석 상태.

운동 세션이 진행되는 동안 reference 각도 시퀀스, 누적된 user 프레임,
rep 카운터·상태를 thread-safe하게 관리한다. StartAnalysis에서 생성되고
StopAnalysis 또는 CompleteAnalysis 콜백 직후 제거된다.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field

from app.models.pose import Landmark

# rep 하나에 담길 수 있는 최대 프레임 수 (이슈 #91).
#
# rep 은 길어야 몇 초다. 값은 "일어날 수 있는 최대 fps × 넉넉한 rep 길이"로 잡는다 —
# 현재 클라는 ~3fps(frontend exercise.tsx intervalMs=330)지만 config 의
# VIDEO_PROCESS_FPS 는 10 이고 실시간 POST 빈도가 코드로 강제돼 있지 않아
# (ai-load-budget.md §4.1) 위로 열려 있다. 10fps × 6초 = 60.
#
# 오판 방향은 안전한 쪽이다. 크게 잡으면 rep 이 아닌 프레임이 조금 섞일 뿐이지만,
# 작게 잡으면 진짜 rep 의 앞부분이 잘려 대표 프레임 선택(가장 깊게 앉은 순간)이
# 하강 구간을 못 보게 된다.
MAX_REP_FRAMES = 60

# 실시간 프레임 «수락» 상한 (이슈 #143 · ㄱ-2 안).
#
# 왜 필요한가: rep 판정 상수 3개(MIN_REP_FRAMES 4 · MAX_BOTTOM_FRAMES 15 · MAX_REP_FRAMES 60)가
# 전부 «프레임 개수» 로 시간을 인코딩하는데, 개수를 초로 되돌리는 fps 가 코드로 고정돼 있지
# 않다. 클라가 빨라지면 세 상수의 실효 시간이 전부 짧아지고, 오판 방향이 안전하지 않다 —
# #143 측정: 10fps 에서 바닥 체류 0.5초짜리 «정상» 스쿼트가 rep 0 으로 사라진다
# (3fps 에서는 체류 4.3초까지 버틴다).
#
# 값의 근거: 상수 4/15/60 이 검증된 유일한 지점이 현재 클라의 3fps 다
# (frontend exercise.tsx 의 intervalMs=330). 새 약속을 만들지 않으려고 그 지점을 상한으로 쓴다.
#
# 왜 330 이 아니라 300 인가: 330 으로 두면 클라 간격과 «같아져» 경계값이 된다. 스케줄러·네트워크
# 지터로 개별 도착 간격이 330 을 밑돌 때마다 규약을 지키는 클라의 프레임이 버려진다. 한 칸 아래인
# 300ms(3.33fps)에서 bottom 예산은 15/3.33 = 4.5초로 3fps(5.0초) 대비 0.5초만 줄고, 측정된
# «bottom 구간 ≈ 체류 + 1.1초» 기준으로 체류 3.4초까지 허용한다 — 주석이 근거로 든 실제 체류
# ~1초의 3배다.
#
# ⚠️ 이 상한은 «지금 도는 값을 보존» 하는 것이지 3fps 가 옳다는 근거가 아니다. 세 상수의 값
#    자체는 여전히 미검증이고(#143 §5-3), fps 를 올리려면 그 재검증이 선행한다.
MIN_FRAME_INTERVAL_SEC = 0.300


@dataclass
class PerRepFrame:
    timestamp_sec: float
    joint_coordinates: str  # JSON 직렬화된 landmark
    angles: list[float]

    # 좌우 무릎각 평균을 최근 3프레임으로 평활한 값. 작을수록 깊게 앉은 것이다.
    #
    # rep 안에서 sync_rate 는 상수라(rep 단위 채점 후 프레임마다 복제) 어느 프레임이
    # "가장 나빴나"를 sync_rate 로는 고를 수 없다. 그런데 joint_coordinates 는 프레임마다
    # 다르므로, 어느 프레임을 남기느냐가 리포트에 어떤 자세가 그려지는지를 결정한다.
    # 그 선택 기준이 이 값이다(decisions/worst-section-rep-resolution.md §4-ㄹ).
    #
    # 정의를 상태 머신과 일치시켰다 — rep 경계를 판정하는 값(_extract_raw_metrics 의 좌우 평균을
    # 3프레임 평활)과 같은 값이라야, "이 rep 의 바닥"과 "가장 깊은 프레임"이 같은 근거를 갖는다.
    # 원시값이 아니라 평활값인 것은 의도적이다: 랜드마크가 한 프레임 튀면 그 프레임이 대표로
    # 뽑혀 이상한 뼈대가 리포트에 그려진다. 대가로 최소점이 1~2프레임 밀리지만 다운샘플(R≈5)에 묻힌다.
    smoothed_knee_angle: float = 0.0


@dataclass
class CompletedRep:
    rep_number: int
    sync_rate: float
    frames: list[PerRepFrame]
    feedback_message: str = ""


@dataclass
class SessionState:
    session_id: int
    exercise_id: int
    exercise_type: str = "squat"
    persona: str = "BEGINNER"
    reference_angles: list[list[float]] = field(default_factory=list)

    # 진행 중인 rep에 누적되는 프레임들.
    #
    # 상한을 두는 이유(이슈 #91): 이 버퍼는 rep이 완성될 때만 비워진다. rep 사이에 흘러든
    # 프레임 — 세트 사이 휴식, 세트 중간에 멈칫하는 순간 — 이 계속 쌓이다가 다음 rep의
    # 배치에 통째로 실려 나가고, 그 프레임들이 다음 rep의 rep_number를 달고 저장된다.
    # 상한을 두면 오래된 것부터 밀려나 rep 직전 프레임만 남는다. 그게 실제로 그 rep을
    # 이루는 프레임이다.
    #
    # 클라가 휴식 중 전송을 멈추면(#92) 평상시 유입은 사라지지만, 앱은 '예정된' 휴식만
    # 알기 때문에 예정 없는 멈춤에는 여전히 프레임이 흐른다. 그래서 이 상한은 #92와
    # 무관하게 필요하다 — 서버가 클라의 협조에 기대지 않는 하한이다.
    current_rep_frames: deque[PerRepFrame] = field(
        default_factory=lambda: deque(maxlen=MAX_REP_FRAMES)
    )

    # 분석기 내부 상태 (StreamingSquatAnalyzer가 관리)
    rep_count: int = 0
    rep_state: str = "waiting_for_standing"
    last_rep_frame_index: int = -10_000
    # bottom 구간에서 **실제로 100° 아래에 있던** 프레임 수. 앉아서 쉬는 것과 스쿼트를 가르는
    # 축이다 (이슈 #93).
    #
    # 진입 프레임 인덱스가 아니라 카운터인 이유(이슈 #159): 진입~이탈 프레임 차이로 재면 이탈
    # 임계가 150° 라서 **상승 중 100~150° 구간이 통째로 「바닥 체류」에 포함된다.** 그러면 하강이
    # 느린 사용자일수록 체류 예산이 줄어, 속도를 안 보려고 만든 상수가 속도에 의존하게 된다.
    # 측정: 3fps·체류 0.5초 고정에서 하강 5.1초부터 정상 rep 이 사라졌다.
    bottom_frame_count: int = 0
    frame_index: int = 0
    previous_smoothed_knee: float | None = None
    recent_raw_knees: list[float] = field(default_factory=list)

    # 완료된 rep 요약 (StopAnalysis 시 평균 계산용)
    completed_reps: list[CompletedRep] = field(default_factory=list)

    # --- 유입 속도 상한 (#143 ㄱ-2) ---
    #
    # 다음 프레임을 받을 수 있는 가장 이른 시각 (time.monotonic 기준). None 이면 아직 한 장도
    # 안 받았다는 뜻이다. 벽시계가 아니라 monotonic 인 것은 의도적이다 — 이 판정의 신뢰 경계를
    # 서버 안에서 닫는 것이 ㄱ-2 를 고른 이유이고, 벽시계는 NTP 보정으로 뒤로 갈 수 있다.
    next_frame_deadline: float | None = None

    # 드롭률 관측 (#143 · #151). AI 쪽에는 메트릭 익스포터가 아예 없어서(#151) 우선 카운터 +
    # 세션 종료 로그로 시작한다. 이 값이 있어야 «클라가 빨라졌다» 는 사실 자체를 알아챌 수 있다 —
    # 없으면 드롭은 조용하고, 그건 상한을 안 건 것과 관측 면에서 같다.
    accepted_frame_count: int = 0
    dropped_frame_count: int = 0

    # --- 프레임 시각의 기준점 (#156) ---
    #
    # timestamp_sec 은 «세션 시작 기준 경과 초» 여야 한다 — 계약서·엔티티 주석·리포트 포맷이
    # 전부 그렇게 적혀 있다. 그런데 예전에는 클라가 준 Date.now()/1000(epoch)을 그대로 흘려보내
    # 리포트의 시각 표시가 "29770991:08" 같은 값이 됐다.
    #
    # 이제 서버가 만든다. 기준은 **첫 수락 프레임의 도착 시각**(time.monotonic)이고, 클라 시계는
    # 아예 안 본다 — 벽시계가 아니라 단조시계라 NTP 보정에도 뒤로 가지 않는다.
    first_frame_mono: float | None = None

    # 재부착 보정. AI 는 상태를 잃고 새로 만들어질 수 있는데, 그러면 위 기준이 «재부착 시점» 이
    # 되어 경과가 0 부터 다시 시작한다. Spring 이 session.start_time 으로부터의 경과를 실어 보내면
    # (ReattachRequest.elapsed_sec) 여기에 담아 더한다. initial_rep_count 가 rep 축에서 하는 일을
    # 시간 축에서 하는 값이다.
    elapsed_offset_sec: float = 0.0


def elapsed_sec(state: SessionState, now: float) -> float:
    """이 프레임의 «세션 시작 기준 경과 초» (이슈 #156).

    Args:
        state: 대상 세션. 첫 호출에서 기준점이 여기에 박힌다.
        now: 프레임 도착 시각 (`time.monotonic()`). 상한 판정이 쓰는 값과 **같은 것**을 넘겨야
            한다 — 둘이 다른 시점을 보면 «수락한 프레임의 시각» 이 아니게 된다.

    기준을 세션 «생성» 이 아니라 **첫 프레임 도착**으로 잡는 이유: StartAnalysis 와 첫 프레임
    사이에는 사용자가 자세를 잡는 시간이 있고, 그건 운동 시간이 아니다. 리포트가 표시하는 것은
    "운동 중 언제" 이므로 첫 프레임이 0 인 편이 읽는 사람의 기대에 맞는다.

    ⚠️ 그래서 재부착 세션의 0 은 «세션 시작» 이 아니라 «재부착 후 첫 프레임» 이다. 그 차이를
    메우는 것이 `elapsed_offset_sec` 이고, 값은 Spring 이 준다(ReattachRequest.elapsed_sec).
    """
    if state.first_frame_mono is None:
        state.first_frame_mono = now
    return round(state.elapsed_offset_sec + (now - state.first_frame_mono), 3)


def accept_frame(state: SessionState, now: float) -> bool:
    """유입 속도 상한을 넘지 않는 프레임만 True (#143 ㄱ-2).

    Args:
        state: 대상 세션. 수락/드롭 카운터와 데드라인이 여기서 갱신된다.
        now: `time.monotonic()` 값. 테스트가 시간을 주입할 수 있게 인자로 받는다.

    데드라인을 **«직전 데드라인 + 간격»** 으로 밀고 «수락 시각 + 간격» 으로 밀지 않는 이유:
    후자는 지터를 누적한다. 늦게 도착한 프레임이 기준을 그만큼 뒤로 밀고, 다음 프레임은 그
    밀린 기준과 비교되므로, **평균적으로 규약을 지키는 클라도 프레임의 1/3 가량을 잃는다.**
    전자는 장기 평균만 제한하므로 지터가 그대로 통과한다.

    대신 «밀린 크레딧» 을 버리는 처리가 따라온다 — 클라가 상한보다 느리게 보내는 동안 데드라인이
    현재 시각보다 뒤처지면 그 차이가 곧 크레딧이 되고, 그대로 두면 클라가 쉬었다 재개하는 순간
    쌓인 만큼이 한꺼번에 통과한다. 그게 정확히 이 상한이 막으려던 상황이다.
    """
    deadline = state.next_frame_deadline

    if deadline is not None and now < deadline:
        state.dropped_frame_count += 1
        return False

    if deadline is None:
        next_deadline = now + MIN_FRAME_INTERVAL_SEC
    else:
        next_deadline = deadline + MIN_FRAME_INTERVAL_SEC
        if next_deadline < now:
            next_deadline = now + MIN_FRAME_INTERVAL_SEC

    state.next_frame_deadline = next_deadline
    state.accepted_frame_count += 1
    return True


class SessionStateRegistry:
    """sessionId → SessionState 매핑. 모든 접근은 Lock 하에 수행."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[int, SessionState] = {}

    def create(
        self,
        session_id: int,
        exercise_id: int,
        reference_angles: list[list[float]],
        exercise_type: str = "squat",
        persona: str = "BEGINNER",
        initial_rep_count: int = 0,
    ) -> SessionState:
        with self._lock:
            state = SessionState(
                session_id=session_id,
                exercise_id=exercise_id,
                exercise_type=exercise_type,
                persona=persona,
                reference_angles=reference_angles,
                rep_count=initial_rep_count,
            )
            self._sessions[session_id] = state
            return state

    def create_if_absent(
        self,
        session_id: int,
        exercise_id: int,
        reference_angles: list[list[float]],
        exercise_type: str = "squat",
        persona: str = "BEGINNER",
        initial_rep_count: int = 0,
    ) -> tuple[SessionState, bool]:
        """재부착 전용. 상태가 이미 있으면 **보존하고** 그대로 돌려준다.

        create() 는 같은 id 로 다시 부르면 기존 상태를 덮어쓴다. 재부착 경로에서 그러면 중복 호출·
        네트워크 재시도·빠른 이탈 후 복귀 때 진행 중이던 rep 과 스무딩 이력을 통째로 버리게 된다 —
        정작 재부착이 필요 없는 경우에 피해를 주는 셈이다.

        확인과 생성이 한 Lock 안에 있어야 한다. get() 후 create() 로 나누면 그 사이에 다른 요청이
        상태를 만들 수 있고, 그러면 덮어쓰기를 막으려던 가드가 그대로 뚫린다.

        Returns:
            (상태, already_active) — already_active 가 True 면 아무것도 만들지 않았다는 뜻이다.
        """
        with self._lock:
            existing = self._sessions.get(session_id)
            if existing is not None:
                return existing, True

            state = SessionState(
                session_id=session_id,
                exercise_id=exercise_id,
                exercise_type=exercise_type,
                persona=persona,
                reference_angles=reference_angles,
                rep_count=initial_rep_count,
            )
            self._sessions[session_id] = state
            return state, False

    def get(self, session_id: int) -> SessionState | None:
        with self._lock:
            return self._sessions.get(session_id)

    def remove(self, session_id: int) -> SessionState | None:
        with self._lock:
            return self._sessions.pop(session_id, None)

    def exists(self, session_id: int) -> bool:
        with self._lock:
            return session_id in self._sessions


# 프로세스 전역 싱글톤
_registry = SessionStateRegistry()


def get_registry() -> SessionStateRegistry:
    return _registry