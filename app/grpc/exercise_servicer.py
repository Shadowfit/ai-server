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


def _extract_reference_poses_from_video(path: str) -> list:
    """기준 영상 → **점수가 가장 높은 rep 1회**의 프레임별 좌표 (#192).

    🔴 **영상 전체를 넣지 않는다.** 이 좌표는 DTW 대조의 «정답지» 라, 서 있는 구간·촬영
       전후가 섞이면 사용자의 rep 이 그것들과 비교된다. 실제 rep 하나가 정답지다.

    🔴 **여러 rep 을 평균한 «대표 시퀀스» 는 쓸 수 없다.** `reference_builder` 는 «각도» 를
       평균하는데 이 표(`exercise_references`)는 «랜드마크» 를 저장하고 AI 가 읽어서 각도로
       바꾼다. 각도 평균은 랜드마크로 되돌릴 수 없다. 그래서 한 rep 을 그대로 쓴다 —
       `V4__seed_squat_reference.sql` 이 같은 규칙으로 만들어졌으므로 두 경로가 같은 것을 낸다.

    🔴 **이 함수는 결정적이지 않다** ([#224](https://github.com/Shadowfit/init/issues/224)).
       같은 영상·같은 코드인데 실행마다 결과가 갈린다 — 2026-08-16 실측에서 **37↔36프레임,
       score 105.53↔102.87, 최저무릎 95.4°↔96.7°**. 원인 미규명(트래킹 상태·CPU 부하·rep
       경계 판정 — 셋 다 안 갈랐다).

       **그래서 재추출은 «같은 정답지를 다시 만드는 일» 이 아니다.** `saveReferencePoses` 가
       기존 행을 교체하므로(#220) 달라진 값이 즉시 전면 적용된다. 관리자가 «영상은 그대로인데
       한 번 더 눌렀다» 로 채점 기준이 미묘하게 바뀐다는 뜻이다. 크기는 작지만(±1.3 score)
       **«같은 입력에 같은 출력» 이 성립하지 않는다는 사실 자체**가, 나중에 「정답지를
       바꿨나?」를 되짚을 때 근거를 없앤다.
    """
    from app.api.endpoints.pose import _landmarks_to_json
    from app.core.reference_builder import _segment_reps
    from app.core.video_processor import analyze_video

    result = analyze_video(path, "squat")
    segments = _segment_reps(result.frames)
    if not segments:
        raise ValueError("기준 영상에서 유효한 스쿼트 반복을 찾지 못했다")

    best = max(segments, key=lambda r: r.score)
    frames = [
        f for f in result.frames
        if best.start_frame_index <= f.frame_index <= best.end_frame_index and f.squat_metrics
    ]
    if not frames:
        raise ValueError("선택된 rep 구간에 유효 프레임이 없다")

    t0 = frames[0].timestamp
    logger.info(
        "[#192] rep %d개 중 최고점 선택 — score=%.2f · %d프레임 · 최저무릎 %.1f°",
        len(segments), best.score, len(frames), best.min_knee_angle,
    )
    return [
        exercise_pb2.PoseDataRequest(
            timestamp_sec=round(f.timestamp - t0, 3),
            joint_coordinates=_landmarks_to_json(f.landmarks),
        )
        for f in frames
    ]


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

        # 소유권 비밀값을 세션 상태에 보관한다 (#187 안 (d)). POST /pose 가 동봉한 값과
        # 여기를 대조한다. proto3 라 «없음» 이 빈 문자열로 오므로 None 으로 되돌린다 —
        # 빈 문자열을 그대로 두면 «빈 값을 보낸 요청» 과 «값이 없는 세션» 이 같아진다.
        get_registry().create(
            session_id=session_id,
            exercise_id=exercise_id,
            reference_angles=reference_angles,
            exercise_type=exercise_type,
            persona=persona,
            session_nonce=request.session_nonce or None,
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
            session_nonce=request.session_nonce or None,
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

        # 「되고 있는데 0」을 운영 중에 잡는 자리다 (#267 곁가지). 위 로그는 «상한에 걸렸나» 만
        # 답하고, 상한을 통과한 뒤 가시성에서 떨어진 프레임은 «수락» 으로 세어져 보이지 않는다.
        # 판정에 들어간 프레임이 하나도 없으면 리포트는 전 필드 0 으로 끝나는데, 그때 원인이
        # 「사람이 안 왔다」가 아니라 「하체가 프레임 밖이었다」라는 것을 여기서만 알 수 있다.
        #
        # 🔴 **판정 0 이면 무조건 찍는다.** 「가시성 스킵이 있을 때만」으로 걸면 정작 제일 나쁜
        #    경우를 놓친다 — 사람을 아예 못 찾은 세션은 `pose.py:80`(NO_POSE)이 `accept_frame`
        #    **앞에서** 반환하므로 세 카운터가 전부 0 이고, 그러면 위 #143 로그도 이 로그도
        #    안 찍혀 **StopAnalysis 가 프레임 유입에 대해 아무 말도 안 한다.** 리포트만 전 필드
        #    0 으로 나오고 왜인지는 어디에도 안 남는다 — #196 이 겪은 것이 정확히 그 상태다.
        if state.needs_intake_warning:
            logger.warning(
                "[#267] 판정에 들어간 프레임 %d개 (session=%s) — 수락 %d · 가시성 스킵 %d · 상한 드롭 %d%s",
                state.judged_frame_count,
                session_id,
                state.accepted_frame_count,
                state.visibility_skip_count,
                state.dropped_frame_count,
                (
                    " 🔴 한 프레임도 판정되지 않았다"
                    " (수락까지 0 이면 사람을 못 찾은 것이다 — NO_POSE 는 이 숫자에 안 잡힌다)"
                    if judged <= 0
                    else ""
                ),
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
        """[Spring → FastAPI] 기준 영상 → 기준 좌표(정답지) 추출 (#192).

        `youtube_url` 필드는 **컨테이너 안에서 읽을 수 있는 영상 파일 경로**로 해석한다.

        🔴 **HTTP(S) URL 은 거부한다.** 유튜브 다운로드는 ToS 상 금지이고, 이 프로젝트는
           그 리스크 수용 여부를 **아직 결정하지 않았다**
           (`docs/decisions/youtube-coordinate-harvest.md` §4-2·§7). 결정 전에 코드가 먼저
           내려받기 시작하면 그 미결정이 조용히 없어진다. 필드 이름은 proto 호환 때문에
           그대로 두되 **의미만 좁힌다.**

        저장은 이 응답이 아니라 **Spring 역호출**로 한다 — 같은 이름의 RPC 를 Spring 도
        서버로 구현하고 있고(`ExerciseGrpcService.extractReferenceData`), 그쪽이
        `saveReferencePoses` 로 DB 에 넣는다. 비어 있던 것은 이쪽 절반뿐이었다.
        """
        url = request.youtube_url or ""
        logger.info(
            "[Spring → AI] ExtractReferenceData 수신 (exercise=%s, source=%s)",
            request.exercise_id, url,
        )

        if url.startswith("http://") or url.startswith("https://"):
            logger.error(
                "[#192] 원격 URL 은 지원하지 않는다 — 유튜브 다운로드는 미결정 항목이다. "
                "컨테이너에서 읽을 수 있는 파일 경로를 줄 것 (받은 값: %s)", url,
            )
            return exercise_pb2.ExtractResponse(
                success=False, exercise_id=request.exercise_id, extracted_poses=[]
            )

        try:
            poses = _extract_reference_poses_from_video(url)
        except Exception as e:  # noqa: BLE001 — 사유를 그대로 로그에 남긴다
            logger.error("[#192] 기준 좌표 추출 실패 (%s): %s", url, e)
            return exercise_pb2.ExtractResponse(
                success=False, exercise_id=request.exercise_id, extracted_poses=[]
            )

        ok = spring_client.send_reference_poses(request.exercise_id, poses)
        return exercise_pb2.ExtractResponse(
            success=ok, exercise_id=request.exercise_id, extracted_poses=poses
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