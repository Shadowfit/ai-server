"""실시간 포즈 감지 API.

session_id가 함께 오면 누적 분석 + rep 감지를 수행하고,
rep 1회가 완성될 때마다 Spring에 PoseData 묶음을 콜백한다.
"""

import logging
import secrets
import time

import cv2
import orjson

from fastapi import APIRouter
from fastapi.responses import ORJSONResponse

import exercise_pb2
from app.config import settings
from app.core.analyzer_registry import get_analyzer
from app.core.angle_calculator import extract_angles
from app.core.mediapipe_detector import get_detector, lease_detector
from app.core.squat_analyzer import _torso_tilt_degrees
from app.grpc import spring_client
from app.grpc.session_state import (
    MIN_FRAME_INTERVAL_SEC,
    PerRepFrame,
    accept_frame,
    elapsed_sec,
    get_registry,
)
from app.models.pose import Landmark, PoseRequest, PoseResponse, PoseSkipReason
from app.observability import frame_path
from app.utils.image_utils import base64_to_image

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/pose", tags=["포즈 감지"], default_response_class=ORJSONResponse)

# 분석기 레지스트리는 app.core.analyzer_registry 로 옮겼다(이슈 #147). gRPC servicer 도 같은
# 표를 봐야 하는데, 그게 HTTP 엔드포인트 모듈에 있으면 참조 방향이 거꾸로가 된다.

# BACK_BENT(#193·#228) 판정 컷. squat_analyzer.analyze_squat_frames:228 의 기존 mean_torso 컷과
# 같은 값이다 — 새 임계값이 아니라 배치(영상 업로드) 경로가 이미 쓰는 "과도한 전방 기울임"
# 판정을 스트리밍 경로에 재사용한다. 정답지 대비 상대 판정이 아니라 절대값인 것은 의도적인
# 축소다(feedback-type-detector.md 의 "정답지 대비" 설계와는 다르다) — 판정 자체는
# squat_analyzer.py·exercise_servicer.py를 안 건드리고 이 파일 안에서 끝낸다(SessionState에
# pending_feedback_events 버퍼 하나는 재전송 때문에 추가돼 있다, 아래 flush_pending_feedback).
# 값이 어긋나면(둘 중 하나만 바뀌면) 두 경로가 다른 기준으로 "등 굽음"을 판정하게 된다는
# 뜻이니 같이 바꿀 것.
_BACK_BENT_TILT_THRESHOLD = 35.0


def _landmarks_to_json(landmarks: list[Landmark]) -> str:
    return orjson.dumps(
        [
            {"index": lm.index, "x": lm.x, "y": lm.y, "z": lm.z, "visibility": lm.visibility}
            for lm in landmarks
        ]
    ).decode()


def flush_pending_feedback(state) -> None:
    """`state.pending_feedback_events`를 오래된 것부터 1건씩 Spring에 보낸다.

    (#193 재전송, `docs/decisions/feedback-batch-retransmission.md` §7)

    한 번에 1건만 보내는 이유: `FeedbackBatchOutcome`은 배치 전체에 대해서만 나온다. batch를
    항상 1로 고정하면 "이 배치 안에서 어느 건이 무효인가"를 가릴 필요 자체가 없어진다 —
    방금 보낸 그 1건이 곧 거절당한 그 건이다.

    호출자(`pose.py`의 rep 완성 분기, `exercise_servicer.py`의 `StopAnalysis`)가 락 밖에서
    불러야 한다 — 매 시도가 gRPC 호출이라 락 안에서 돌리면 그 세션의 다음 프레임이 막힌다.
    """
    while state.pending_feedback_events:
        event = state.pending_feedback_events[0]
        outcome, _ = spring_client.report_feedback_batch(
            state.session_id,
            1,      # set_no — BT-NONE 호환 고정값. 세트 경계 개념이 아직 없다
            False,  # is_final — Spring 이 현재 안 읽는 필드(ExerciseGrpcService.reportFeedbackBatch)
            [event],
        )
        if outcome == spring_client.FeedbackBatchOutcome.OK:
            state.pending_feedback_events.pop(0)
            continue
        if outcome == spring_client.FeedbackBatchOutcome.SESSION_GONE:
            # 재시도해도 세션은 안 돌아온다 — 버퍼째 버린다.
            state.pending_feedback_events.clear()
            return
        if outcome == spring_client.FeedbackBatchOutcome.INVALID:
            # 우리 쪽 값 오류다. 같은 값을 또 보내도 똑같이 거절당하니 이 1건만 버리고 다음으로.
            state.pending_feedback_events.pop(0)
            continue
        # TRANSIENT — 버퍼는 유지하고 이번엔 여기서 그만 시도한다. 다음 rep 완성(또는
        # StopAnalysis의 마지막 flush)때 이 건부터 다시 시도된다.
        return


# 응답 생성 방식(팔). `ai-process-ceiling-cause.md` §11 — 기본은 현행(`model`)이다.
#
# 🔴 `response_model` 은 **데코레이터 시점에 굳는다.** 그래서 런타임 분기가 아니라 여기서
#    한 번 고른다 — 기동 후에는 못 바꾼다(판마다 재기동하는 rig 구조와 맞는다).
_RESPONSE_MODE = settings.RESPONSE_MODE
_RESPONSE_MODEL = PoseResponse if _RESPONSE_MODE == "model" else None

# 널 핸들러 팔. 🔴 기동 시 한 번 굳힌다 — 요청마다 settings 를 읽으면 그 조회 자체가
# 재려는 구간 안에 들어간다(`_RESPONSE_MODE` 와 같은 이유).
_NULL_HANDLER = settings.POSE_NULL_HANDLER


@router.post("", response_model=_RESPONSE_MODEL)
def detect_pose(req: PoseRequest):
    """반환 시각을 **한 자리에서** 찍기 위한 얇은 껍질.

    🔑 여기서 값을 돌려주는 순간 **threadpool 스레드가 반납되고, 그 뒤는 이벤트 루프**다
    (FastAPI 의 응답 직렬화). 그 경계를 안 찍으면 `post` 가 «앱 후처리» 와 «루프 직렬화» 를
    한 칸에 담아, 후보 ㄴ(단일 이벤트 루프)과 ㄷ(스레드풀 상한)이 안 갈린다
    (`docs/decisions/ai-process-ceiling-cause.md` §2-4).

    🔴 `try/finally` 인 이유 — 본문에 return 이 넷이고 예외 경로도 있다. 어느 쪽으로 끝나도
    스레드는 반납되므로 **짝이 맞아야 한다.**
    ⚠️ 계측이 꺼져 있으면 `mark_handler_out()` 은 즉시 반환한다(다른 `mark_*` 와 같다).
    """
    try:
        if _NULL_HANDLER:
            # 🔑 `mark_handler_in()` 은 **찍는다.** 널 팔에서 빼려는 것은 «계산» 이지
            #    «수신 경로» 가 아니다 — 이 표지가 없으면 `wait`(도착 → 워커에 실림)가
            #    통째로 사라지는데, 그게 이 팔에서 가장 보고 싶은 값이다(§4 wait 22.0%).
            frame_path.mark_handler_in()
            # 🔬 널 핸들러 팔 (`per-process-ceiling-cause.md` 축 3). 본문은 **이미 받았고**
            #    Pydantic 검증까지 끝났다 — 여기서 끊으면 그 뒤(디코딩·추론·분석·콜백)가
            #    통째로 빠진다. 「파이썬 계산을 다 빼도 프로세스당 천장이 남는가」가 질문이다.
            #    ⚠️ `mark_decoded()` 아래 표지는 **안 찍는다** — 안 한 일을 0ms 로 남기면
            #       구간 표가 「빨랐다」로 읽힌다. 빠진 구간은 비어 있어야 한다.
            return PoseResponse(
                success=False,
                skip_reason=PoseSkipReason.NULL_HANDLER,
                message="널 핸들러 팔 — 측정용",
            )
        result = _detect_pose(req)
        if _RESPONSE_MODE == "model":
            return result
        # 🔴 여기부터는 **측정용 팔**이다. `model_dump()` 를 핸들러 안에서 부르므로 그 몫이
        #    루프가 아니라 **이 스레드**에서 나간다 — `post_loop` 가 줄고 `post_app` 이
        #    그만큼 느는지가 §11 의 검산이다. «비용이 사라진 것» 으로 읽으면 안 된다.
        payload = result.model_dump(mode="json")
        if _RESPONSE_MODE == "dict":
            return payload
        return ORJSONResponse(content=payload)
    finally:
        frame_path.mark_handler_out()


def _detect_pose(req: PoseRequest):
    """Base64 이미지 → 관절 감지 + (선택) 세션 누적 분석.

    MediaPipe 추론, OpenCV 변환, Spring 콜백 gRPC가 모두 동기 블로킹이라
    `async def`로 두면 이벤트 루프를 점유해 다른 요청을 굶긴다. FastAPI는
    `def` 핸들러를 자동으로 threadpool에서 실행하므로 그대로 두면 된다.
    """
    # 유입 «도착» 시각. 반드시 디코딩·추론 **앞** 에서 찍는다 — 뒤에서 찍으면 상한이 재는 것이
    # 클라 전송 간격이 아니라 MediaPipe 추론까지 끝난 완료 간격이 된다. 그 경우 프레임당 처리가
    # 상한(300ms)을 넘는 기기에서는 클라가 아무리 빨리 보내도 전부 수락되고, 이 상한이 막으려던
    # rep 소실이 그대로 재발한다. `test_frame_rate_limit` 의 핸들러 회귀 테스트가 이 순서를 고정한다.
    received_at = time.monotonic()
    # 여기가 «워커에 실린» 순간이다. 요청 도착과의 차(`wait`)가 §12 가 물은 값이고, 계측이
    # 꺼져 있으면 이 호출들은 전부 즉시 반환한다(decisions/ai-receive-path-scaling.md §12).
    frame_path.mark_handler_in()

    image_bgr = base64_to_image(req.image)
    image_rgb = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)
    frame_path.mark_decoded()

    # 세션이 있으면 «그 세션 전용» 검출기를 쓴다(#164). 스레드 로컬이면 요청이 아무 스레드나
    # 집어가면서 직전에 본 다른 세션 때문에 트래킹이 깨진다 — 실사용 3fps 에서 검출률 손실
    # 41~63%p (loadtest/results/thread-collision-2026-08-11/).
    # with 블록은 세션 락이다. 클라 백프레셔가 없어 같은 세션 프레임이 겹칠 수 있는데,
    # 겹쳐서 같은 PoseDetector 를 동시에 부르면 지금보다 나쁘다.
    lease = lease_detector(req.session_id)
    if lease is None:
        # 풀에 자리가 없다 = 세션이 시작되지 않았거나 상한에 걸렸다.
        #
        # 🔴 응답은 아래 `state is None` 분기와 **똑같아야 한다** (#187 안 (d), #605).
        #    이 게이트는 registry 검사보다 먼저 도니, 여기서 구분되는 응답을 내면 그 원칙이
        #    두 번째 게이트에서만 지켜지고 첫 번째 게이트에서는 session_id 를 훑어 "지금 배정된
        #    분석기가 있는 세션"(≈ 진행 중)을 열거할 수 있게 된다 — #605 가 실제로 재현했다.
        #    구분은 서버 로그에만 남긴다.
        logger.warning(
            "세션 %s 에 배정된 분석기가 없다 (풀 상한 또는 미시작) — 응답은 «세션 없음» 과 같다 (#605)",
            req.session_id,
        )
        return PoseResponse(
            success=False,
            skip_reason=PoseSkipReason.SESSION_NOT_FOUND,
            message=f"세션 {req.session_id}가 시작되지 않았습니다 (StartAnalysis 먼저 호출 필요)",
        )
    with lease as detector:
        # 리스 획득까지가 후보 2순위(검출기 획득 경로, §10-2)다. 추론과 갈라서 잰다 —
        # 합쳐 재면 「추론이 비싸다」로 읽히고 그건 R6 가 이미 반증한 답이다.
        frame_path.mark_leased()
        landmarks = detector.detect(image_rgb)
    frame_path.mark_inferred()

    if not landmarks:
        return PoseResponse(
            success=False,
            skip_reason=PoseSkipReason.NO_POSE,
            message="포즈를 감지할 수 없습니다",
        )

    # 세션 미지정 — 기존 stateless 동작 (각도만 반환)
    if req.session_id is None:
        angles = extract_angles(landmarks, req.exercise_type)
        return PoseResponse(success=True, landmarks=landmarks, angles=angles)

    state = get_registry().get(req.session_id)
    if state is None:
        return PoseResponse(
            success=False,
            skip_reason=PoseSkipReason.SESSION_NOT_FOUND,
            message=f"세션 {req.session_id}가 시작되지 않았습니다 (StartAnalysis 먼저 호출 필요)",
        )

    # 세션 소유권 대조 (이슈 #187 안 (d)).
    #
    # 여기까지 온 요청은 «토큰이 맞다» 까지만 증명됐다. 그 토큰은 앱 번들에 들어가므로 사실상
    # 공개값이고, session_id 는 AUTO_INCREMENT 순차 정수라 추측된다 — 즉 이 대조가 없으면
    # 토큰을 뽑은 누구나 남의 세션에 프레임을 꽂을 수 있다(그리고 그 데이터는 Spring DB 까지 간다).
    #
    # 🔴 **거절 응답이 «세션 없음» 과 같은 모양이어야 한다.** 여기서 «세션은 있는데 네 것이
    #    아니다» 라고 답하면, 공격자가 session_id 를 훑어 **살아있는 세션을 열거**할 수 있다 —
    #    막으려던 것의 절반을 응답으로 되돌려주는 셈이다. 구분은 서버 로그에만 남긴다.
    #
    # 🟢 **2단계 — 강제.** 세션이 값을 갖고 있으면 요청도 맞는 값을 내야 한다. 1단계에서는
    #    «요청이 값을 안 보냄» 도 통과였는데, 그 창이 곧 이 방어의 부재였다.
    #
    # ⚠️ **세션에 보관값이 없으면(state.session_nonce is None) 여전히 통과한다.** 구멍처럼
    #    보이지만 **여기에 도달할 수 있는 세션이 사실상 없다.**
    #
    #    ① 배포 «후» 만들어진 세션은 항상 값을 갖는다(SessionService 가 무조건 발급한다).
    #    ② 배포 «전» 세션의 상태는 이 registry 가 프로세스 메모리라 **재배포로 통째로 날아간다**
    #       — 그런 요청은 이 검사에 닿기도 전에 위쪽 `state is None` 에서 떨어진다.
    #    ③ 그래서 실제로 이 분기가 열리는 경로는 **재부착 하나**다: Spring 이 session_nonce 가
    #       NULL 인 옛 행(V8 이전 세션)으로 ReattachAnalysis 를 보낼 때. 그 행들도 세션
    #       타임아웃이 걷어간다.
    #
    #    즉 막아서 얻을 것이 없고(공격자가 NULL 보관값 상태를 만들 수단이 없다), 막으면 옛
    #    세션을 되살리는 경로만 끊긴다.
    if state.session_nonce is not None:
        # compare_digest 는 str 을 받으므로 None 을 먼저 가른다. «안 보냄» 과 «틀림» 은
        # 아래에서 같은 응답으로 합쳐진다 — 클라 입장에서 구분할 이유가 없고, 구분하면
        # «이 세션은 nonce 를 요구한다» 는 사실이 새어 나간다.
        presented = req.session_nonce
        if presented is None or not secrets.compare_digest(presented, state.session_nonce):
            # 값은 절대 로그에 남기지 않는다 — 남기면 로그를 읽는 사람이 그 세션의 소유자가 된다.
            logger.warning(
                "세션 %s 소유권 대조 실패 — 프레임을 버린다 (#187). 응답은 «세션 없음» 과 같다",
                req.session_id,
            )
            return PoseResponse(
                success=False,
                skip_reason=PoseSkipReason.SESSION_NOT_FOUND,
                message=f"세션 {req.session_id}가 시작되지 않았습니다 (StartAnalysis 먼저 호출 필요)",
            )

    analyzer = get_analyzer(state.exercise_type)
    if analyzer is None:
        return PoseResponse(
            success=False,
            skip_reason=PoseSkipReason.UNSUPPORTED_EXERCISE,
            message=f"미지원 운동: {state.exercise_type}",
        )

    # 유입 속도 상한 (#143 ㄱ-2) 판정부터 rep 버퍼 조작까지 한 락으로 묶는다(#162). 세션당
    # 하나뿐인 락이라 다른 세션과는 안 걸리고, 같은 세션 동시 요청만 직렬화한다 — MediaPipe
    # 추론(위에서 이미 끝남)은 락 밖이라 무거운 계산은 안 묶인다. gRPC 콜백(Spring 전송)도
    # 락 밖에서 돈다 — 네트워크 호출을 락 안에 두면 그 세션의 다음 프레임이 그동안 전부 막힌다.
    with state.state_lock:
        # 상태머신에 넣기 **전** 에 자른다 — 판정 상수가 전부 프레임 개수라, 클라가 빨라지면
        # 실효 시간이 짧아져 정상 rep 이 «휴식» 으로 버려진다.
        #
        # 랜드마크는 그대로 돌려준다. 여기서 막는 것은 «판정에 들어가는 프레임» 이지 클라의
        # 스켈레톤 오버레이가 아니다. 즉 화면은 클라가 보내는 속도 그대로 부드럽고, 판정만
        # 상한을 탄다. MediaPipe 추론 자체를 아끼는 것은 별건이다(#92) — 그건 유입 «양» 의
        # 문제이고 여기는 «속도» 다.
        if not accept_frame(state, received_at):
            if state.dropped_frame_count == 1:
                # 세션당 한 번만 남긴다. 드롭은 매 프레임 일어날 수 있어서 그대로 두면 로그가
                # 잠긴다. 누적 수치는 StopAnalysis 의 요약 로그가 담당한다.
                logger.warning(
                    "[#143] 프레임 유입이 상한(%.0fms)을 넘어 초과분을 드롭한다 (session=%s). "
                    "클라 전송 간격이 규약(exercise.tsx intervalMs=330)보다 빨라졌다는 뜻이다 — "
                    "판정 상수 4/15/60 은 3fps 에서만 검증돼 있다.",
                    MIN_FRAME_INTERVAL_SEC * 1000,
                    req.session_id,
                )
            # 🔴 success=False 다 (이슈 #267). 이건 서버가 «의도적으로» 자른 것이라 세션이
            # 건강해도 나오지만, 그래도 **판정에는 안 들어갔다.** «정상 동작인가» 는
            # skip_reason 이 답한다. landmarks 는 그대로 채운다 — 막는 것은 판정이지 클라의
            # 스켈레톤 오버레이가 아니다.
            return PoseResponse(
                success=False,
                skip_reason=PoseSkipReason.RATE_LIMITED,
                landmarks=landmarks,
                message="유입 속도 상한 초과 — 분석 스킵",
                rep_count=state.rep_count,
            )

        angles, smoothed_knee_angle, rep_event = analyzer.process_frame(state, landmarks)

        if angles is None:
            # visibility 부족 — 프레임 스킵. 🔴 success=False 다 (이슈 #267).
            #
            # 이 자리가 #196 통주행이 속은 바로 그 자리다 — landmarks 는 들어 있어서 «검출
            # 30/31» 로 세면 정상으로 보이는데, 판정에 들어간 프레임은 0 이었다. 하체가
            # 프레임 밖이면 계속 이 갈래로 떨어지고 리포트가 전 필드 0 으로 끝난다.
            state.visibility_skip_count += 1
            return PoseResponse(
                success=False,
                skip_reason=PoseSkipReason.LOW_VISIBILITY,
                landmarks=landmarks,
                message="가시성 부족으로 분석 스킵",
                rep_count=state.rep_count,
            )

        # 프레임 시각은 **서버가 만든다** (이슈 #156). 예전에는 두 갈래였고 둘 다 틀렸다:
        #   req.timestamp_sec       클라의 Date.now()/1000 = epoch. 리포트 시각이
        #                           "29770991:08" 이 됐다
        #   float(state.frame_index) 누락 시 fallback. 개수가 초 자리에 들어가 3fps 면
        #                           정확히 3배로 «그럴듯하게» 틀렸다 — epoch 보다 오히려 나빴다
        # 이제 origin 이 하나다. req.timestamp_sec 은 더 읽지 않는다.
        frame = PerRepFrame(
            timestamp_sec=elapsed_sec(state, received_at),
            joint_coordinates=_landmarks_to_json(landmarks),
            angles=angles,
            smoothed_knee_angle=smoothed_knee_angle,
            landmarks=landmarks,
        )
        state.current_rep_frames.append(frame)

        if rep_event is None:
            return PoseResponse(
                success=True,
                landmarks=landmarks,
                angles=angles,
                rep_count=state.rep_count,
            )

        # rep 완성 — Spring 전송·로컬 요약에 쓸 프레임을 **한 번만** 스냅샷한다. 락을 풀고
        # gRPC 콜백을 부른 뒤 다시 잡아 completed_reps 에 넣을 때 current_rep_frames 를 다시
        # 읽으면, 그 사이 끼어든 다음 요청이 버퍼에 새 프레임을 넣어 두 기록(Spring 전송분과
        # 로컬 completed_reps)이 서로 달라질 수 있다 — 스냅샷 하나를 양쪽에 같이 쓴다.
        rep_frames_snapshot = list(state.current_rep_frames)

    # 락 밖 — Spring 전송(gRPC)은 다른 세션은 물론 이 세션의 다음 프레임도 막지 않는다.
    pose_data_list = [
        exercise_pb2.PoseDataRequest(
            timestamp_sec=f.timestamp_sec,
            joint_coordinates=f.joint_coordinates,
            sync_rate=rep_event.sync_rate,
            feedback_message=rep_event.feedback_message,
            # 재부착 시 Spring 이 MAX(rep_number) 로 rep 카운트를 복원하는 근거 (이슈 #59 2단계)
            rep_number=rep_event.rep_number,
            # sync_rate 는 rep 상수라 프레임을 구분하지 못한다. Spring 이 다운샘플에서 어느
            # 프레임을 남길지, 리포트에서 어느 프레임을 대표로 쓸지 고르는 기준이 이 값이다
            # (decisions/worst-section-rep-resolution.md §4-ㄹ). 작을수록 깊게 앉은 것.
            smoothed_knee_angle=f.smoothed_knee_angle,
        )
        for f in rep_frames_snapshot
    ]
    spring_client.report_pose_data_batch(state.session_id, pose_data_list)

    # BACK_BENT 감지 (#193·#228). 게이트는 이미 있는 3종 심각도를 그대로 쓴다 — "자세 양호"면
    # 애초에 유형을 안 만든다(새 임계값 0개). 🔄 2026-09-03 정정: 예전엔 여기서 joint_coordinates
    # (Spring 전송용으로 dumps해 둔 JSON)를 다시 loads해서 썼다("PerRepFrame에 새 필드를 안
    # 만들기 위해서"). 그런데 이 프레임을 만들 때 이미 감지된 원본 landmarks 객체가 있는데
    # 같은 요청 안에서 그걸 문자열로 만들었다가 도로 파싱하는 왕복이라, PerRepFrame에 필드
    # 하나(landmarks)를 얹어 그 왕복 자체를 없앴다.
    if rep_event.feedback_message != "자세 양호" and rep_frames_snapshot:
        tilts = []
        for f in rep_frames_snapshot:
            if not f.landmarks:
                continue
            lm_by_index = {lm.index: lm for lm in f.landmarks}
            tilts.append(_torso_tilt_degrees(lm_by_index))
        if tilts and (sum(tilts) / len(tilts)) > _BACK_BENT_TILT_THRESHOLD:
            state.pending_feedback_events.append(
                spring_client.PendingFeedbackEvent(
                    feedback_type="BACK_BENT",
                    rep_number=rep_event.rep_number,
                    sync_rate_at_trigger=rep_event.sync_rate,
                )
            )

    # 이번 rep에서 새로 생겼든 이전 rep에서 못 보내고 남았든, rep 완성마다 한 번 비워본다
    # (재전송, #193·docs/decisions/feedback-batch-retransmission.md §7).
    flush_pending_feedback(state)

    # 누적 요약 보관 + 현재 rep 버퍼 비우기 — 다시 락을 잡는다. clear() 가 스냅샷 이후 새로
    # 들어온 프레임까지 지우면 안 되므로, 통째로 비우지 않고 스냅샷한 만큼만 왼쪽에서 덜어낸다.
    from app.grpc.session_state import CompletedRep

    with state.state_lock:
        state.completed_reps.append(
            CompletedRep(
                rep_number=rep_event.rep_number,
                sync_rate=rep_event.sync_rate,
                frames=rep_frames_snapshot,
                feedback_message=rep_event.feedback_message,
            )
        )
        for _ in range(len(rep_frames_snapshot)):
            if not state.current_rep_frames:
                break
            state.current_rep_frames.popleft()

    logger.info(
        "세션 %s rep %d 완성 (sync_rate=%.2f)",
        state.session_id,
        rep_event.rep_number,
        rep_event.sync_rate,
    )

    return PoseResponse(
        success=True,
        landmarks=landmarks,
        angles=angles,
        rep_count=state.rep_count,
        rep_completed=True,
        sync_rate=rep_event.sync_rate,
    )