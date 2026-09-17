"""ExtractReferenceData 가 «접수» 만 하고 즉시 돌아온다 (2026-09-17, 관리자 mp4 업로드).

무엇을 고정하나:
    1. 원격 URL·없는 파일은 **즉시** success=False — 백그라운드로 안 넘어간다.
    2. 있는 파일은 **추출을 기다리지 않고** success=True 를 돌려주고, 추출·역호출은
       백그라운드 풀에서 그 뒤에 일어난다.

왜 이 파일이 필요한가:
    예전엔 추출을 끝내고 응답했다. Spring 은 이 RPC 에 5초 데드라인을 걸어
    (`ExerciseAnalysisService.GRPC_CALL_TIMEOUT_SECONDS`) 데모 영상조차 DEADLINE_EXCEEDED 로
    끝났고, 그게 서킷브레이커 실패로 집계돼 업로드 5번이면 그 채널의 라이브 세션이 거부됐다.
    «응답이 추출 시간과 무관하다» 는 성질을 여기서 지킨다 — 추출 대역이 응답 뒤에 풀리는
    Event 를 기다리게 해서, 핸들러가 추출을 기다렸다면 테스트가 데드락 대신 타임아웃으로 실패한다.
"""

from __future__ import annotations

import threading

import exercise_pb2
from app.grpc import exercise_servicer as servicer_mod
from app.grpc.callback_pool import CallbackPool
from app.grpc.exercise_servicer import ExerciseServicer


class _Ctx:
    def invocation_metadata(self):
        return ()


def _fresh_pool(monkeypatch) -> CallbackPool:
    """모듈 싱글턴 대신 테스트마다 새 풀 — 이전 테스트의 큐가 섞이지 않게."""
    pool = CallbackPool(1, name="test-reference-extraction")
    monkeypatch.setattr(servicer_mod, "get_reference_extraction_pool", lambda: pool)
    return pool


def test_remote_url_is_rejected_immediately(monkeypatch):
    pool = _fresh_pool(monkeypatch)
    resp = ExerciseServicer().ExtractReferenceData(
        exercise_pb2.ExtractRequest(exercise_id=1, youtube_url="https://youtu.be/abc"), _Ctx()
    )
    assert resp.success is False
    assert pool.pending() == 0 and pool.in_flight() == 0


def test_missing_file_is_rejected_immediately(monkeypatch, tmp_path):
    pool = _fresh_pool(monkeypatch)
    resp = ExerciseServicer().ExtractReferenceData(
        exercise_pb2.ExtractRequest(exercise_id=1, youtube_url=str(tmp_path / "nope.mp4")), _Ctx()
    )
    assert resp.success is False
    assert pool.pending() == 0 and pool.in_flight() == 0


def test_existing_file_is_accepted_before_extraction_finishes(monkeypatch, tmp_path):
    pool = _fresh_pool(monkeypatch)
    video = tmp_path / "ref.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypisom")

    release = threading.Event()   # 핸들러가 돌아온 **뒤에** 풀린다
    sent = threading.Event()
    seen: dict = {}

    def _fake_extract(path):
        # 핸들러가 이 함수를 기다렸다면 release 는 영영 안 풀리고 아래 wait 가 타임아웃으로 실패한다.
        assert release.wait(timeout=5), "핸들러가 추출을 동기로 기다렸다"
        seen["path"] = path
        return [exercise_pb2.PoseDataRequest(timestamp_sec=0.0, joint_coordinates="[]")]

    def _fake_send(exercise_id, poses):
        seen["exercise_id"] = exercise_id
        seen["n"] = len(poses)
        sent.set()
        return True

    monkeypatch.setattr(servicer_mod, "_extract_reference_poses_from_video", _fake_extract)
    monkeypatch.setattr(servicer_mod.spring_client, "send_reference_poses", _fake_send)

    resp = ExerciseServicer().ExtractReferenceData(
        exercise_pb2.ExtractRequest(exercise_id=7, youtube_url=str(video)), _Ctx()
    )

    assert resp.success is True
    assert resp.exercise_id == 7
    assert len(resp.extracted_poses) == 0
    assert not sent.is_set(), "응답 전에 역호출이 나갔다 — 동기 처리다"

    release.set()
    assert sent.wait(timeout=5), "백그라운드 추출·역호출이 안 일어났다"
    assert seen == {"path": str(video), "exercise_id": 7, "n": 1}
