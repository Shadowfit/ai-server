"""sessionId별 in-memory 운동 분석 상태.

운동 세션이 진행되는 동안 reference 각도 시퀀스, 누적된 user 프레임,
rep 카운터·상태를 thread-safe하게 관리한다. StartAnalysis에서 생성되고
StopAnalysis 또는 CompleteAnalysis 콜백 직후 제거된다.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field

from app.models.pose import Landmark

# rep 하나에 담길 수 있는 최대 프레임 수 (이슈 #91).
#
# 🔴 이 값은 시간 상수가 아니다 — 제약 두 개를 혼자 진다 (이슈 #160).
#
#   상한 방향: 세션당 메모리를 막아야 하니 **작아야** 한다 (#91 이 이 값을 만든 이유).
#             deque(maxlen=) 로 쓰이므로 이 수가 곧 세션당 보관 프레임 상한이다.
#   하한 방향: rep 의 하강 구간을 담아야 하니 **커야** 한다. 잘리면 대표 프레임 선택
#             (가장 깊게 앉은 순간)이 하강을 못 보고 고른다.
#
# fps 가 오르면 둘이 충돌한다. 그래서 «지금 무엇이 구속하고 있는가» 를 같이 적어 둔다.
#
# 지금 구속하는 것: **없다(하한 쪽 여유가 매우 크다).** 아래 MIN_FRAME_INTERVAL_SEC 이
# 유입을 3.33fps 로 묶으므로 60 프레임 = 18초 분량이고, rep 하나가 18초일 리 없다.
#
# ⚠️ 값 60 의 원래 근거는 «10fps × 6초» 였는데, 그 10fps 는 #143 이 유입 상한을 넣으면서
#    (cac535a) 더 이상 도달할 수 없는 값이 됐다. 즉 **현재 유효 근거는 3.33fps × 18초**이고,
#    이 여유는 전적으로 그 유입 상한에 기대고 있다. MIN_FRAME_INTERVAL_SEC 을 풀거나
#    올리면 이 상수의 하한 쪽이 먼저 조인다 — 그때는 60 을 재검증할 것.
#
# 오판 방향은 안전한 쪽이다. 크게 잡으면 rep 이 아닌 프레임이 조금 섞일 뿐이지만,
# 작게 잡으면 진짜 rep 의 앞부분이 잘려 대표 프레임 선택이 하강 구간을 못 보게 된다.
# 다만 «크게 잡는 쪽» 의 대가가 메모리라는 것이 위 상한 방향이다 — 공짜가 아니다.
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


# 종료한 세션 id 를 얼마나 기억할 것인가 (#191).
#
# 아웃박스는 at-least-once 라 같은 StopAnalysis 가 두 번 올 수 있다. 두 번째가 왔을 때
# «이미 처리했다» 와 «세션을 정말 잃었다» 를 구분하려면 첫 처리를 기억하고 있어야 한다.
#
# 값의 근거 — Spring 이 회수분을 다시 보낼 수 있는 가장 이른 시점이다:
#   lease 60s   (backend outbox.publisher.lock-timeout-seconds, 기본값)
#   + 폴링 1s   (outbox.publisher.poll-interval-ms, 기본값)
#   + 데드라인 5s (ExerciseAnalysisService.GRPC_CALL_TIMEOUT_SECONDS)
#   = 66s
#
# ⚠️ 이 값은 **Spring 설정의 복사본**이다. 위 셋 중 하나라도 바뀌면 여기도 바꿔야 한다.
#    단일 출처로 만들려면 Spring 이 «이 요청은 회수분일 수 있다» 를 요청에 실어 보내야 하는데,
#    그건 proto 계약 변경이고 지금은 생성 산출물이 두 벌인 상태(#132)라 그 위에 얹지 않았다.
#
#    어긋났을 때의 결말은 안전한 쪽이다: 창이 짧으면 두 번째 호출이 그냥 success=False 로
#    떨어지고, 그건 이 변경 이전의 동작이다(Spring 이 회수분이면 세션을 안 건드린다, #152).
#    즉 «조용히 틀리는» 게 아니라 «조용히 원래대로» 다.
STOPPED_SESSION_RETENTION_SEC = 66.0


class SessionStateRegistry:
    """sessionId → SessionState 매핑. 모든 접근은 Lock 하에 수행."""

    def __init__(self, retention_sec: float = STOPPED_SESSION_RETENTION_SEC) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[int, SessionState] = {}
        # sessionId → 종료 처리한 시각(monotonic). remove() 가 채우고 was_recently_stopped()
        # 가 읽는다. 값은 int+float 둘뿐이라 세션당 비용이 사실상 없다 — 검출기(98.7MB)를
        # 붙들고 있는 것과는 다른 이야기다.
        self._recently_stopped: dict[int, float] = {}
        self._retention_sec = retention_sec

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

    def remove(self, session_id: int, now: float | None = None) -> SessionState | None:
        """상태를 꺼내고, **꺼냈다는 사실**을 보유 기간 동안 남긴다 (#191).

        Args:
            now: `time.monotonic()` 값. 테스트가 시간을 주입할 수 있게 인자로 받는다
                 (`accept_frame` 과 같은 방식).
        """
        stamp = time.monotonic() if now is None else now
        with self._lock:
            state = self._sessions.pop(session_id, None)
            # 🔴 **실제로 꺼냈을 때만** 기록한다.
            #
            # 처음엔 «없었어도 기록해두면 재송신 창을 넓게 덮는다» 고 적었는데 정반대였다.
            # 호출부(StopAnalysis)가 remove() 직후 같은 id 로 was_recently_stopped() 를 묻기
            # 때문에, 없어도 기록하면 그 물음이 **항상 True** 가 되어 (나) 분기가 도달 불가가
            # 된다. 한 번도 없던 세션의 첫 중단 요청까지 «이미 처리됨» 으로 답하게 되고,
            # 그러면 Spring 은 정말 유실된 세션을 SENT 로 종결한 뒤 빠른 실패를 영영 안 탄다 —
            # 고치려던 것보다 나쁜 상태다. CodeRabbit 이 PR #172 리뷰에서 잡았다.
            #
            # 중복 중단은 여기 안 들어오므로 최초 종료 시각이 그대로 유지된다. 보유 기간은
            # «언제 실제로 끝났나» 부터 세는 게 맞다 — 재송신이 올 때마다 갱신하면 창이
            # 무한정 밀린다.
            if state is not None:
                self._recently_stopped[session_id] = stamp
            self._prune_stopped(stamp)
            return state

    def was_recently_stopped(self, session_id: int, now: float | None = None) -> bool:
        """보유 기간 안에 종료 처리된 세션인가 — «이미 처리됨» 과 «정말 잃음» 의 구분점."""
        stamp = time.monotonic() if now is None else now
        with self._lock:
            self._prune_stopped(stamp)
            return session_id in self._recently_stopped

    def _prune_stopped(self, now: float) -> None:
        """만료분 정리. 호출부가 Lock 을 잡고 있어야 한다.

        쓰기·읽기 양쪽에서 부른다. 읽기에서도 정리하지 않으면, 세션이 더 안 들어오는 동안
        만료된 id 가 남아 «보유 기간이 지났는데 True» 가 나온다.
        """
        cutoff = now - self._retention_sec
        expired = [sid for sid, at in self._recently_stopped.items() if at < cutoff]
        for sid in expired:
            del self._recently_stopped[sid]

    def exists(self, session_id: int) -> bool:
        with self._lock:
            return session_id in self._sessions


# 프로세스 전역 싱글톤
_registry = SessionStateRegistry()


def get_registry() -> SessionStateRegistry:
    return _registry