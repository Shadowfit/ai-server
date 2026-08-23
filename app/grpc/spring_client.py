"""Spring 백엔드로 콜백을 보내는 gRPC 클라이언트.

ExerciseServicer가 rep 완성·세션 종료 시점에 호출한다.
"""

from __future__ import annotations

import logging
import threading
import time

import grpc

import exercise_pb2
import exercise_pb2_grpc
from app.config import settings
from app.grpc.correlation import correlation_metadata

logger = logging.getLogger(__name__)

_channel: grpc.Channel | None = None
_stub: exercise_pb2_grpc.ExerciseServiceStub | None = None
_lock = threading.Lock()

# CompleteAnalysis 콜백이 실패하면 세션 결과가 영구 유실되므로 재시도한다.
# 3회 시도, 시도 사이 1s → 3s 백오프 (총 worst-case 4초). 최종 실패 시
# ERROR 로그만 남기고 포기 — 장기 장애 복구는 별도 영구 큐가 필요하다.
_COMPLETE_MAX_ATTEMPTS = 3
_COMPLETE_BACKOFF_SECONDS = (1.0, 3.0)

# SavePoseDataBatch 도 실패하면 rep 하나 분량이 통째로 사라진다 (#188). 값은 위 CompleteAnalysis
# 계약을 그대로 복제한 것이다 — 유실 빈도가 미측정이라(#151) 새 숫자를 고를 근거가 없다.
# 🔴 «다시 던져도 되는 실패» 와 «영구 실패» 를 가른다 (#209 · #276 ③).
#
# 예전에는 grpc.RpcError 면 무엇이든 3회를 던졌다. 그런데 서버가 상태코드를 안 갈라줘서
# (전부 INTERNAL) 가를 방법이 없었던 것이기도 하다 — 그 절반이 2026-08-23 에 고쳐졌다.
# 이제 Spring 은 이렇게 답한다:
#   ABORTED            = 데드락 재시도 상한 소진. 잠시 뒤면 대개 성공한다 → 던진다
#   UNAVAILABLE        = 서버가 없거나 내려갔다 → 던진다
#   DEADLINE_EXCEEDED  = 예산 초과 → 던진다 (상위 예산이 있으면 그쪽이 자른다)
#   RESOURCE_EXHAUSTED = 지금은 과부하 → 던진다
#   NOT_FOUND          = 세션이 사라졌다 → **던지지 않는다.** 3회를 더 던져도 사라진 세션은 안 돌아온다
#   INVALID_ARGUMENT · FAILED_PRECONDITION · PERMISSION_DENIED · UNAUTHENTICATED = 우리 잘못 → 안 던진다
#
# 안 던지는 쪽이 중요한 이유: 영구 실패를 3회 던지면 **worst-case 4초 동안 이 워커가 붙잡히고**
# 그만큼 프레임 유입이 밀린다(아래 report_pose_data_batch 주석의 그 대가다).
_RETRYABLE_CODES = frozenset(
    {
        grpc.StatusCode.ABORTED,
        grpc.StatusCode.UNAVAILABLE,
        grpc.StatusCode.DEADLINE_EXCEEDED,
        grpc.StatusCode.RESOURCE_EXHAUSTED,
    }
)


def _is_retryable(e: grpc.RpcError) -> bool:
    """상태코드가 «다시 던져도 되는» 부류인가. 코드를 못 읽으면 보수적으로 재시도한다."""
    code = e.code() if hasattr(e, "code") else None
    if code is None:
        return True
    return code in _RETRYABLE_CODES


_POSE_BATCH_MAX_ATTEMPTS = 3
_POSE_BATCH_BACKOFF_SECONDS = (1.0, 3.0)


def auth_metadata() -> tuple[tuple[str, str], ...]:
    return (("authorization", f"Bearer {settings.INTERNAL_API_TOKEN}"),)


def call_metadata() -> tuple[tuple[str, str], ...]:
    """인증 토큰 + correlation id. Spring 쪽 서버 인터셉터가 id 를 꺼내 자기 로그에 찍는다."""
    return auth_metadata() + correlation_metadata()


def get_stub() -> exercise_pb2_grpc.ExerciseServiceStub:
    global _channel, _stub
    with _lock:
        if _stub is None:
            _channel = grpc.insecure_channel(settings.BACKEND_GRPC_ADDRESS)
            _stub = exercise_pb2_grpc.ExerciseServiceStub(_channel)
            logger.info("Spring gRPC 채널 생성: %s", settings.BACKEND_GRPC_ADDRESS)
        return _stub


def report_pose_data_batch(
    session_id: int, pose_data_list: list[exercise_pb2.PoseDataRequest]
) -> None:
    """rep 1회 완성 시 Spring에 PoseData 묶음 전송. 실패 시 백오프로 재시도.

    재시도가 없던 동안 이 경로는 실패하면 **rep 하나 분량이 통째로 사라졌다**(#188).
    세 콜백 중 유일하게 재전송도 수신측 멱등도 없던 자리다.

    값(3회 · 1s → 3s)은 새로 정한 상수가 아니라 **같은 채널·같은 방향인 CompleteAnalysis 의
    계약을 그대로 복제**한 것이다. 유실 빈도가 아직 측정된 적이 없어(#151 — AI 계측 0줄)
    지금 새 숫자를 고르면 근거 없는 값이 된다.

    ⚠️ CompleteAnalysis 와 다른 점: 저건 **세션당 1회**지만 이건 **rep 마다** 온다. 재시도가
    끝까지 가면 worst-case 4초 동안 이 워커가 붙잡히고, 그만큼 프레임 유입이 밀린다.
    CompleteAnalysis 에는 없던 대가다 — 유입 지연이 관측되면 #206(gRPC 예산 미전파)과 묶어
    다시 본다.

    중복은 수신측이 흡수한다 — Spring 이 (session_id, rep_number, timestamp_sec, created_at)
    유니크 키로 멱등을 건다(docs/decisions/pose-batch-idempotency-implementation.md).
    재전송에도 created_at 이 같아야 하므로 그 값은 **AI 가 보내지 않고 Spring 이 세션 시작
    시각에서 가져온다** — 이 함수가 시각을 만들지 않는 것이 설계다.
    """
    request = exercise_pb2.PoseDataBatchRequest(
        session_id=session_id,
        pose_data=pose_data_list,
    )

    # 메타데이터를 루프 밖에서 한 번만 만든다 — 안에서 만들면 매 attempt 마다 새 correlation id
    # 가 발급돼 같은 배치의 시도들이 서로 안 묶인다(report_complete_analysis 와 같은 이유).
    metadata = call_metadata()

    for attempt in range(1, _POSE_BATCH_MAX_ATTEMPTS + 1):
        try:
            response = get_stub().SavePoseDataBatch(request, metadata=metadata)
            logger.info(
                "[AI → Spring] PoseData 배치 전송 (session=%s, count=%d, success=%s, attempt=%d)",
                session_id,
                len(pose_data_list),
                response.success,
                attempt,
            )
            return
        except grpc.RpcError as e:
            if not _is_retryable(e):
                # 영구 실패다 — 더 던져도 결과가 같고, 그동안 이 워커가 붙잡힌다 (#209 · #276 ③).
                logger.error(
                    "[AI → Spring] PoseData 배치 거절 (session=%s, code=%s) — 재시도하지 않는다: %s",
                    session_id,
                    e.code(),
                    e.details(),
                )
                return
            if attempt == _POSE_BATCH_MAX_ATTEMPTS:
                logger.error(
                    "[AI → Spring] PoseData 배치 전송 실패 (session=%s, count=%d, %d회 시도): %s",
                    session_id,
                    len(pose_data_list),
                    attempt,
                    e.details(),
                )
                return
            backoff = _POSE_BATCH_BACKOFF_SECONDS[attempt - 1]
            logger.warning(
                "[AI → Spring] PoseData 배치 전송 실패 (session=%s, attempt=%d) — %.1fs 후 재시도: %s",
                session_id,
                attempt,
                backoff,
                e.details(),
            )
            time.sleep(backoff)


def report_complete_analysis(
    session_id: int,
    total_reps: int,
    avg_sync_rate: float,
    max_sync_rate: float = 0.0,
    min_sync_rate: float = 0.0,
    calories_burned: float = 0.0,
) -> None:
    """최종 분석 결과를 Spring에 콜백. 실패 시 지수 백오프로 재시도."""
    request = exercise_pb2.SessionCompleteRequest(
        session_id=session_id,
        total_reps=total_reps,
        avg_sync_rate=avg_sync_rate,
        max_sync_rate=max_sync_rate,
        min_sync_rate=min_sync_rate,
        calories_burned=calories_burned,
    )

    # 재시도 루프 **밖에서** 한 번만 만든다 — 안에서 만들면 컨텍스트에 id 가 없을 때
    # 매 attempt 마다 새 id 가 발급돼 같은 CompleteAnalysis 의 시도들이 서로 안 묶인다.
    metadata = call_metadata()

    for attempt in range(1, _COMPLETE_MAX_ATTEMPTS + 1):
        try:
            response = get_stub().CompleteAnalysis(request, metadata=metadata)
            logger.info(
                "[AI → Spring] CompleteAnalysis 성공 (session=%s, status=%s, attempt=%d)",
                session_id,
                response.status,
                attempt,
            )
            return
        except grpc.RpcError as e:
            if not _is_retryable(e):
                # 영구 실패다 — 더 던져도 결과가 같고, 그동안 이 워커가 붙잡힌다 (#209 · #276 ③).
                logger.error(
                    "[AI → Spring] 분석 완료 콜백 거절 (session=%s, code=%s) — 재시도하지 않는다: %s",
                    session_id,
                    e.code(),
                    e.details(),
                )
                return
            if attempt >= _COMPLETE_MAX_ATTEMPTS:
                logger.error(
                    "[AI → Spring] CompleteAnalysis 최종 실패 (session=%s, attempts=%d): %s",
                    session_id,
                    attempt,
                    e.details(),
                )
                return
            wait = _COMPLETE_BACKOFF_SECONDS[attempt - 1]
            logger.warning(
                "[AI → Spring] CompleteAnalysis 실패 — %.1fs 후 재시도 (session=%s, %d/%d): %s",
                wait,
                session_id,
                attempt,
                _COMPLETE_MAX_ATTEMPTS,
                e.details(),
            )
            time.sleep(wait)

def send_reference_poses(
    exercise_id: int, poses: list[exercise_pb2.PoseDataRequest]
) -> bool:
    """추출한 기준 좌표(정답지)를 Spring 에 보내 저장시킨다 (#192).

    🔴 **`ExtractReferenceData` 는 양방향으로 같은 이름이다.** Spring→AI 는 «추출해라» 는
       트리거이고, AI→Spring 은 «추출했다, 저장해라» 다. Spring 쪽 수신부는 이미 완성돼
       있다(`ExerciseGrpcService.extractReferenceData` → `PoseDataService.saveReferencePoses`).
       비어 있던 것은 이쪽 절반뿐이었다.
    """
    try:
        request = exercise_pb2.ExtractRequest(
            exercise_id=exercise_id,
            extracted_poses=poses,
        )
        response = get_stub().ExtractReferenceData(request, metadata=call_metadata())
        logger.info(
            "[AI → Spring] 기준 좌표 전송 (exercise=%s, count=%d, success=%s)",
            exercise_id,
            len(poses),
            response.success,
        )
        return response.success
    except grpc.RpcError as e:
        # 이 경로는 원래 재시도가 없다. 대신 **상태코드를 남긴다** — 서버가 코드를 갈라주게 된
        # 뒤로는(#209) 「우리 잘못인가 서버가 아픈가」가 로그 한 줄로 갈린다.
        logger.error(
            "[AI → Spring] 기준 좌표 전송 실패 (code=%s): %s", e.code(), e.details()
        )
        return False
