"""ExerciseService gRPC servicer.

Spring → FastAPI 진입점:
- StartAnalysis: reference 좌표를 받아 세션 상태 초기화
- StopAnalysis: 누적 결과로 CompleteAnalysis 콜백
- ExtractReferenceData: YouTube 좌표 추출 (현재는 빈 응답 — 별도 작업으로 분리)
"""

from __future__ import annotations

import json
import logging
import threading
import time

import grpc
from google.protobuf.timestamp_pb2 import Timestamp

import exercise_pb2
import exercise_pb2_grpc
from app.core.analyzer_registry import resolve_exercise_type, supported_exercise_ids
from app.core.angle_calculator import extract_angles
from app.grpc import spring_client
from app.grpc.correlation import wrap as correlation_wrap
from app.core.mediapipe_detector import get_pool
from app.grpc.session_state import get_registry
from app.models.pose import Landmark

logger = logging.getLogger(__name__)


def _parse_reference_poses(
    reference_poses, exercise_type: str
) -> list[list[float]]:
    """Spring이 보낸 reference PoseDataRequest 리스트 → 각도 시퀀스로 변환."""
    sequences: list[list[float]] = []
    for ref in reference_poses:
        if not ref.joint_coordinates:
            continue
        try:
            raw = json.loads(ref.joint_coordinates)
            landmarks = [
                Landmark(
                    index=item["index"],
                    x=item["x"],
                    y=item["y"],
                    z=item.get("z", 0.0),
                    visibility=item.get("visibility", 1.0),
                )
                for item in raw
            ]
            sequences.append(extract_angles(landmarks, exercise_type))
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.warning("reference 좌표 파싱 실패: %s", e)
            continue
    return sequences


class ExerciseServicer(exercise_pb2_grpc.ExerciseServiceServicer):

    def StartAnalysis(self, request, context):
        """[Spring → FastAPI] 운동 분석 세션 시작."""
        session_id = request.session_id
        exercise_id = request.exercise_id
        persona = request.persona or "BEGINNER"
        logger.info(
            "[Spring → AI] StartAnalysis 수신 (session=%s, exercise=%s, persona=%s, reference_frames=%d)",
            session_id,
            exercise_id,
            persona,
            len(request.reference_poses),
        )

        # exercise_id → 분석기. 여기 없으면 이 종목은 분석할 수 없다(이슈 #147).
        #
        # 예전에는 exercise_type = "squat" 이 못박혀 있었다. 그러면 런지로 세션을 시작해도
        # 스쿼트 분석기가 돌아 **조용히 틀린 점수**가 나온다 — rep 카운팅이 무릎 각도를
        # 하드코딩해 세기 때문이다(squat_analyzer._extract_raw_metrics).
        exercise_type = resolve_exercise_type(exercise_id)
        if exercise_type is None:
            # abort 인 이유: Spring 의 onNext 는 success 필드를 보지 않고 세션 id 만 로깅한다
            # (ExerciseAnalysisService.java:244-247). 즉 success=False 로 거절하면 그대로
            # 삼켜져 세션이 IN_PROGRESS 로 남는다 — «명시적 실패» 라는 이 변경의 목적이
            # 무너진다. onError 경로는 markAsFailedIfStillInProgress 로 세션을 닫아준다.
            logger.error(
                "[Spring → AI] StartAnalysis 거절 — 분석기 없음 (session=%s, exercise=%s, 지원=%s)",
                session_id,
                exercise_id,
                supported_exercise_ids(),
            )
            context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                f"분석기가 없는 운동입니다 (exercise_id={exercise_id})",
            )

        reference_angles = _parse_reference_poses(
            request.reference_poses, exercise_type
        )

        if not reference_angles:
            logger.warning(
                "세션 %s에 reference 각도 시퀀스가 비어 있음 — sync_rate는 0으로 계산됨",
                session_id,
            )

        # 검출기 자리 확보 (#164). 풀 크기 = 동시 활성 세션 상한이고, 그 상한은 컨테이너
        # 메모리에서 유도된다(검출기 1개 = 98.7MB). 자리가 없으면 «받아놓고 느려지는» 대신
        # 여기서 거절한다 — 받아버리면 진행 중인 다른 세션까지 같이 나빠진다.
        used, cap = get_pool().status()
        if not get_pool().acquire(session_id):
            logger.warning(
                "세션 %s 거절 — 검출기 풀 소진 (%d/%d). 동시 세션 상한에 걸렸다. "
                "더 받으려면 컨테이너 메모리 한도(AI_MEM_LIMIT)를 올릴 것.",
                session_id, used, cap,
            )
            return exercise_pb2.AnalyzeResponse(
                success=False,
                session_id=session_id,
                exercise_id=exercise_id,
                status=exercise_pb2.SessionStatus.FAILED,
            )

        get_registry().create(
            session_id=session_id,
            exercise_id=exercise_id,
            reference_angles=reference_angles,
            exercise_type=exercise_type,
            persona=persona,
        )

        now = Timestamp()
        now.GetCurrentTime()
        return exercise_pb2.AnalyzeResponse(
            success=True,
            session_id=session_id,
            exercise_id=exercise_id,
            start_time=now,
            status=exercise_pb2.SessionStatus.IN_PROGRESS,
        )

    def ReattachAnalysis(self, request, context):
        """[Spring → FastAPI] 진행 중이던 세션의 분석 상태를 되살린다 (이슈 #59 2단계).

        StartAnalysis 와 분리한 이유는 **멱등 규칙이 정반대**라서다. StartAnalysis 는 새 세션이니
        상태를 새로 만드는 게 맞고, 재부착은 살아있는 상태를 절대 덮어쓰면 안 된다. 한 핸들러에
        분기로 섞으면 둘 중 하나는 조용히 틀린 동작을 하게 된다.

        되살리는 것 / 못 되살리는 것:
          - 되살림: rep 카운트(Spring 이 pose_data 의 MAX(rep_number) 로 계산해 주입),
                    기준 각도·persona·exercise (원래 Spring 이 DB 에서 읽어 넣어주던 것)
          - 못 되살림: rep_state, frame_index, 스무딩 이력, 진행 중이던 rep 의 프레임
            → 재개 직후 몇 프레임 동안 자세 판정이 흔들릴 수 있다. 감수하기로 한 결정이며
              (docs/decisions/session-resume-and-ai-state.md §4-0, 2026-07-31) 클라에는
              Spring 이 analyzerStateReset 으로 알린다.
        """
        session_id = request.session_id
        logger.info(
            "[Spring → AI] ReattachAnalysis 수신 (session=%s, exercise=%s, initial_rep_count=%d)",
            session_id,
            request.exercise_id,
            request.initial_rep_count,
        )

        # StartAnalysis 와 같은 판정이지만 **거절 방식이 다르다**(이슈 #147).
        #
        # 여기는 abort 가 아니라 success=False 다. 재부착은 Spring 이 응답을 실제로 읽고
        # W009(SESSION_REATTACH_UNAVAILABLE) 로 옮기는 경로가 이미 있어서다 — 바로 아래
        # «기준 좌표 복원 실패» 분기와 같은 형태다. StartAnalysis 쪽은 Spring 이 success 를
        # 안 읽어서 abort 가 아니면 삼켜진다.
        exercise_type = resolve_exercise_type(request.exercise_id)
        if exercise_type is None:
            logger.error(
                "세션 %s 재부착 실패 — 분석기 없음 (exercise=%s, 지원=%s)",
                session_id,
                request.exercise_id,
                supported_exercise_ids(),
            )
            return exercise_pb2.ReattachResponse(
                success=False,
                session_id=session_id,
                message="분석기가 없는 운동입니다.",
            )

        reference_angles = _parse_reference_poses(request.reference_poses, exercise_type)

        if not reference_angles:
            # 시작 경로는 경고만 하고 진행한다(sync_rate 0). 재부착은 다르게 취급한다 — 이미 rep 을
            # 쌓아둔 세션을 sync_rate 가 전부 0 으로 나오는 상태로 이어붙이면, 사용자는 이어진 줄
            # 알지만 뒷부분 기록만 조용히 망가진다. 차라리 실패로 돌려 새로 시작하게 한다.
            logger.error("세션 %s 재부착 실패 — 기준 각도 시퀀스가 비어 있음", session_id)
            return exercise_pb2.ReattachResponse(
                success=False,
                session_id=session_id,
                message="기준 좌표를 복원하지 못했습니다.",
            )

        # 재부착도 검출기가 있어야 프레임을 처리한다. 이미 있으면 acquire 가 True 를 돌려준다.
        if not get_pool().acquire(session_id):
            used, cap = get_pool().status()
            logger.warning("세션 %s 재부착 거절 — 검출기 풀 소진 (%d/%d)", session_id, used, cap)
            return exercise_pb2.ReattachResponse(
                success=False,
                session_id=session_id,
                message="동시 세션 상한에 걸렸습니다.",
            )

        state, already_active = get_registry().create_if_absent(
            session_id=session_id,
            exercise_id=request.exercise_id,
            reference_angles=reference_angles,
            exercise_type=exercise_type,
            persona=request.persona or "BEGINNER",
            initial_rep_count=request.initial_rep_count,
        )

        if already_active:
            logger.info(
                "세션 %s 는 이미 분석 중 — 상태 보존 (rep_count=%d). 중복 호출이거나 재시도다.",
                session_id,
                state.rep_count,
            )
        else:
            # 시간 축 이어붙이기 (이슈 #156). 새로 만든 상태의 프레임 시각은 «첫 프레임 도착» 이
            # 0 이므로, 그대로 두면 재부착 이후 프레임이 0 초부터 다시 시작해 리포트의 시각이
            # 뒤로 감는다. rep 축을 initial_rep_count 로 잇는 것과 같은 처리다.
            #
            # already_active 면 건드리지 않는다 — 그 세션은 기준점을 이미 갖고 있고, 여기서 덮으면
            # 중복 호출·재시도만으로 시각이 앞으로 튄다. create_if_absent 가 상태를 보존하는 이유와
            # 같은 이유다.
            state.elapsed_offset_sec = max(0.0, request.elapsed_sec)
            logger.info(
                "세션 %s 재부착 완료 — rep %d · 경과 %.1f초 부터 이어서 셈 "
                "(분석기 내부 상태는 초기화)",
                session_id,
                state.rep_count,
                state.elapsed_offset_sec,
            )

        return exercise_pb2.ReattachResponse(
            success=True,
            session_id=session_id,
            rep_count=state.rep_count,
            already_active=already_active,
            message="이미 분석 중" if already_active else "재부착 완료",
        )

    def StopAnalysis(self, request, context):
        """[Spring → FastAPI] 사용자 강제 중단. 누적 결과로 CompleteAnalysis 콜백."""
        session_id = request.session_id
        logger.info("[Spring → AI] StopAnalysis 수신 (session=%s)", session_id)

        registry = get_registry()
        state = registry.remove(session_id)
        # 검출기 반납 — 세션이 없었더라도 자리는 회수한다(상태와 풀이 어긋난 경우 대비).
        # close() 하면 메모리가 100% 회수된다(M2 실측). 안 하면 세션 회전마다 98.7MB 씩 샌다.
        get_pool().release(session_id)
        if state is None:
            # 아웃박스가 at-least-once 라 같은 StopAnalysis 가 두 번 올 수 있다(#152). 전에는
            # 두 번째가 무조건 success=False 였고, 그러면 Spring 은 «이미 처리됨»(가) 과
            # «세션을 정말 잃음»(나) 를 응답만으로 못 갈랐다 (#191).
            #
            # 이 분기가 실제로 되찾는 것 — 보유 기간(66초) 안에 온 재송신을 (가) 로 확정한다.
            # 그러면 Spring 은 SENT 로 기록하고 지표도 ok 로 오른다. 전에는 같은 상황이
            # TERMINAL_FAILED + session-missing-redelivery 였다 — 정상 처리된 건을 아웃박스가
            # 실패로 종결하고 있었다.
            #
            # ⚠️ 되찾지 **못하는** 것 — (나) 의 빠른 실패. 보유 기간을 넘겨 도착한 재송신은
            #    여기서도 success=False 가 되고, Spring 은 그게 «늦게 온 (가)» 인지 «진짜 (나)»
            #    인지 여전히 못 가른다. 그래서 Spring 의 possiblyRedelivered 보수 분기는
            #    그대로 있어야 한다 — 그걸 떼면 늦은 재송신이 정상 세션을 FAILED 로 뒤집는다.
            #    (나) 의 안전망은 계속 타임아웃 스케줄러다.
            if registry.was_recently_stopped(session_id):
                logger.info(
                    "[Spring → AI] StopAnalysis 재수신 — 이미 중단 처리된 세션 (session=%s)",
                    session_id,
                )
                return exercise_pb2.StopResponse(
                    success=True,
                    message="이미 중단 처리된 세션입니다(재송신).",
                    session_id=session_id,
                )
            logger.warning(
                "[Spring → AI] StopAnalysis — 보유 기간 내 종료 기록이 없는 세션 (session=%s)",
                session_id,
            )
            return exercise_pb2.StopResponse(
                success=False,
                message="진행 중인 세션을 찾을 수 없습니다.",
                session_id=session_id,
            )

        # 유입 속도 상한의 관측 지점 (#143 ㄱ-2). AI 쪽에는 메트릭 익스포터가 없어서(#151)
        # 지금은 세션 종료 로그가 유일한 창구다. 드롭이 0 이 아니면 «클라가 규약보다 빨리
        # 보내고 있다» 는 뜻이고, 그건 판정 상수 4/15/60 을 재검증할 근거가 된다.
        total_frames = state.accepted_frame_count + state.dropped_frame_count
        if total_frames:
            logger.info(
                "[#143] 프레임 수락/드롭 (session=%s): 수락 %d · 드롭 %d (%.1f%%)",
                session_id,
                state.accepted_frame_count,
                state.dropped_frame_count,
                state.dropped_frame_count * 100.0 / total_frames,
            )

        # 누적된 rep들로 최종 통계 산출 → 별도 스레드에서 Spring 콜백.
        # correlation_wrap 필수 — 새 스레드는 이 핸들러의 ContextVar 를 상속하지 않아서,
        # 감싸지 않으면 CompleteAnalysis 콜백이 자기를 촉발한 StopAnalysis 와 안 이어진다.
        # 이 콜백이 곧 타임아웃 스케줄러와 같은 세션을 두고 경쟁하는 쪽이라 추적이 끊기면 곤란하다.
        threading.Thread(
            target=correlation_wrap(_send_complete_analysis),
            args=(state,),
            daemon=True,
        ).start()

        return exercise_pb2.StopResponse(
            success=True,
            message="분석 중단 및 결과 보고 예약 완료.",
            session_id=session_id,
        )

    def ExtractReferenceData(self, request, context):
        """[Spring → FastAPI] YouTube URL → 기준 좌표 추출.

        실제 YouTube 다운로드/MediaPipe 추출은 별도 작업으로 분리.
        현재는 빈 응답을 돌려주어 인터페이스 호환만 유지한다.
        """
        logger.info(
            "[Spring → AI] ExtractReferenceData 수신 (exercise=%s, url=%s) — 미구현",
            request.exercise_id,
            request.youtube_url,
        )
        return exercise_pb2.ExtractResponse(
            success=True,
            exercise_id=request.exercise_id,
            extracted_poses=[],
        )


def _send_complete_analysis(state) -> None:
    """완료된 rep들의 통계를 모아 Spring에 CompleteAnalysis 호출."""
    # gRPC 서버 쓰레드와 다른 컨텍스트라서 작은 지연으로 race 회피
    time.sleep(0.1)

    reps = state.completed_reps

    # 총 횟수는 len(completed_reps) 가 아니라 rep_count 를 쓴다 (이슈 #59 2단계).
    # 재부착하면 completed_reps 는 재부착 이후 rep 만 담고 있지만 rep_count 는 Spring 이 주입한
    # 재부착 이전 rep 수에서 이어서 올라간다. len() 을 쓰면 이어하기 후 총 횟수가 초기화된다.
    # 재부착이 없었던 세션에서는 rep_count == len(completed_reps) 라 값이 달라지지 않는다.
    total_reps = state.rep_count

    if not reps:
        avg = max_v = min_v = 0.0
    else:
        rates = [r.sync_rate for r in reps]
        avg = round(sum(rates) / len(reps), 2)
        max_v = round(max(rates), 2)
        min_v = round(min(rates), 2)
        # ⚠️ 알려진 한계: 싱크 통계는 **재부착 이후 rep 만** 반영한다. 재부착 이전 rep 의 sync_rate 는
        # Spring 의 pose_data 에는 남아 있지만 AI 메모리에는 없다. 즉 재부착이 일어난 세션은
        # "총 횟수는 정확하고 평균 싱크는 후반 구간 기준"이 된다.
        # (docs/decisions/session-resume-and-ai-state.md §4-0)

    spring_client.report_complete_analysis(
        session_id=state.session_id,
        total_reps=total_reps,
        avg_sync_rate=avg,
        max_sync_rate=max_v,
        min_sync_rate=min_v,
        calories_burned=0.0,  # 칼로리 계산은 추후
    )