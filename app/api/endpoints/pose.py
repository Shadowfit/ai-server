"""실시간 포즈 감지 API.

session_id가 함께 오면 누적 분석 + rep 감지를 수행하고,
rep 1회가 완성될 때마다 Spring에 PoseData 묶음을 콜백한다.
"""

import json
import logging
import secrets
import time

import cv2

from fastapi import APIRouter

import exercise_pb2
from app.core.analyzer_registry import get_analyzer
from app.core.angle_calculator import extract_angles
from app.core.mediapipe_detector import get_detector, lease_detector
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

router = APIRouter(prefix="/pose", tags=["포즈 감지"])

# 분석기 레지스트리는 app.core.analyzer_registry 로 옮겼다(이슈 #147). gRPC servicer 도 같은
# 표를 봐야 하는데, 그게 HTTP 엔드포인트 모듈에 있으면 참조 방향이 거꾸로가 된다.


def _landmarks_to_json(landmarks: list[Landmark]) -> str:
    return json.dumps(
        [
            {"index": lm.index, "x": lm.x, "y": lm.y, "z": lm.z, "visibility": lm.visibility}
            for lm in landmarks
        ]
    )


@router.post("", response_model=PoseResponse)
def detect_pose(req: PoseRequest):
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
        return PoseResponse(
            success=False,
            skip_reason=PoseSkipReason.NO_LEASE,
            message=f"세션 {req.session_id}에 배정된 분석기가 없습니다 (StartAnalysis 먼저 호출 필요)",
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

    # 유입 속도 상한 (#143 ㄱ-2). 상태머신에 넣기 **전** 에 자른다 — 판정 상수가 전부 프레임
    # 개수라, 클라가 빨라지면 실효 시간이 짧아져 정상 rep 이 «휴식» 으로 버려진다.
    #
    # 랜드마크는 그대로 돌려준다. 여기서 막는 것은 «판정에 들어가는 프레임» 이지 클라의 스켈레톤
    # 오버레이가 아니다. 즉 화면은 클라가 보내는 속도 그대로 부드럽고, 판정만 상한을 탄다.
    # MediaPipe 추론 자체를 아끼는 것은 별건이다(#92) — 그건 유입 «양» 의 문제이고 여기는 «속도» 다.
    if not accept_frame(state, received_at):
        if state.dropped_frame_count == 1:
            # 세션당 한 번만 남긴다. 드롭은 매 프레임 일어날 수 있어서 그대로 두면 로그가 잠긴다.
            # 누적 수치는 StopAnalysis 의 요약 로그가 담당한다.
            logger.warning(
                "[#143] 프레임 유입이 상한(%.0fms)을 넘어 초과분을 드롭한다 (session=%s). "
                "클라 전송 간격이 규약(exercise.tsx intervalMs=330)보다 빨라졌다는 뜻이다 — "
                "판정 상수 4/15/60 은 3fps 에서만 검증돼 있다.",
                MIN_FRAME_INTERVAL_SEC * 1000,
                req.session_id,
            )
        # 🔴 success=False 다 (이슈 #267). 이건 서버가 «의도적으로» 자른 것이라 세션이 건강해도
        # 나오지만, 그래도 **판정에는 안 들어갔다.** «정상 동작인가» 는 skip_reason 이 답한다.
        # landmarks 는 그대로 채운다 — 막는 것은 판정이지 클라의 스켈레톤 오버레이가 아니다.
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
        # 이 자리가 #196 통주행이 속은 바로 그 자리다 — landmarks 는 들어 있어서 «검출 30/31» 로
        # 세면 정상으로 보이는데, 판정에 들어간 프레임은 0 이었다. 하체가 프레임 밖이면 계속
        # 이 갈래로 떨어지고 리포트가 전 필드 0 으로 끝난다.
        # ⚠️ 원자적이지 않다. 같은 세션 프레임이 겹치면(위 :67 주석) 둘이 같은 값을 읽어 증가
        #    하나가 사라진다 — 그러면 요약의 `judged` 가 과대평가되고 «판정 0» 표시가 묻힐 수
        #    있다. 형제인 `accepted_frame_count`·`dropped_frame_count` 도 같은 결함이고,
        #    뿌리(세션 상태의 비원자적 read-modify-write)는 #162 다. 여기서 락을 하나 더
        #    만들면 그 이슈가 정할 «어디에 락을 둘 것인가» 를 앞질러 정하게 된다.
        state.visibility_skip_count += 1
        return PoseResponse(
            success=False,
            skip_reason=PoseSkipReason.LOW_VISIBILITY,
            landmarks=landmarks,
            message="가시성 부족으로 분석 스킵",
            rep_count=state.rep_count,
        )

    # 프레임 시각은 **서버가 만든다** (이슈 #156). 예전에는 두 갈래였고 둘 다 틀렸다:
    #   req.timestamp_sec       클라의 Date.now()/1000 = epoch. 리포트 시각이 "29770991:08" 이 됐다
    #   float(state.frame_index) 누락 시 fallback. 개수가 초 자리에 들어가 3fps 면 정확히 3배로
    #                           «그럴듯하게» 틀렸다 — epoch 보다 오히려 나빴다
    # 이제 origin 이 하나다. req.timestamp_sec 은 더 읽지 않는다.
    frame = PerRepFrame(
        timestamp_sec=elapsed_sec(state, received_at),
        joint_coordinates=_landmarks_to_json(landmarks),
        angles=angles,
        smoothed_knee_angle=smoothed_knee_angle,
    )
    state.current_rep_frames.append(frame)

    if rep_event is None:
        return PoseResponse(
            success=True,
            landmarks=landmarks,
            angles=angles,
            rep_count=state.rep_count,
        )

    # rep 1회 완성 → Spring에 그 rep의 PoseData 묶음 콜백
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
        for f in state.current_rep_frames
    ]
    spring_client.report_pose_data_batch(state.session_id, pose_data_list)

    # 누적 요약 보관 + 현재 rep 버퍼 비우기
    from app.grpc.session_state import CompletedRep

    state.completed_reps.append(
        CompletedRep(
            rep_number=rep_event.rep_number,
            sync_rate=rep_event.sync_rate,
            frames=list(state.current_rep_frames),
            feedback_message=rep_event.feedback_message,
        )
    )
    state.current_rep_frames.clear()

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