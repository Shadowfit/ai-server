"""MediaPipe Pose 감지 모듈.

한글 경로에서 MediaPipe C++ 바이너리가 모델 파일을 로드하지 못하는 문제를
ASCII 경로 junction으로 우회한다.
"""

import os
import subprocess
import tempfile
import threading

import numpy as np

from app.config import settings
from app.models.pose import Landmark


def _ensure_ascii_mediapipe():
    """MediaPipe 패키지가 non-ASCII 경로에 있으면 junction을 생성하고
    solution_base.__file__을 패치하여 모델 로드 경로를 우회한다."""
    import mediapipe.python.solution_base as sb

    sb_path = os.path.abspath(sb.__file__)
    try:
        sb_path.encode("ascii")
        return  # ASCII 경로면 우회 불필요
    except UnicodeEncodeError:
        pass

    import mediapipe as mp
    mp_root = os.path.dirname(mp.__file__)

    # root_path = __file__에서 3단계 상위
    # 원래: site-packages/mediapipe/python/solution_base.py → root = site-packages
    # 필요: parent_dir/mediapipe/ 가 junction이면 됨
    parent_dir = os.path.join(tempfile.gettempdir(), "shadowfit_mp_root")
    junction_path = os.path.join(parent_dir, "mediapipe")

    if not os.path.exists(parent_dir):
        os.makedirs(parent_dir, exist_ok=True)

    if not os.path.exists(junction_path):
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", junction_path, mp_root],
            capture_output=True,
        )

    if not os.path.exists(junction_path):
        raise RuntimeError(
            "MediaPipe junction 생성 실패. "
            "프로젝트를 ASCII 경로(예: C:\\projects\\shadowfit)로 이동하세요."
        )

    # __file__을 parent_dir/mediapipe/python/solution_base.py 로 설정
    # → 3단계 올라가면 parent_dir → root_path/mediapipe/modules/... 로 접근 가능
    fake_sb_path = os.path.join(
        junction_path, "python", "solution_base.py"
    )
    sb.__file__ = fake_sb_path


# 모듈 로드 시 패치 적용
_ensure_ascii_mediapipe()

import mediapipe as mp

mp_pose = mp.solutions.pose


class PoseDetector:
    """MediaPipe Pose를 이용한 관절 감지기."""

    def __init__(self):
        self._pose = mp_pose.Pose(
            static_image_mode=False,
            model_complexity=settings.POSE_MODEL_COMPLEXITY,
            min_detection_confidence=settings.POSE_MIN_DETECTION_CONFIDENCE,
            min_tracking_confidence=settings.POSE_MIN_TRACKING_CONFIDENCE,
        )

    def detect(self, image_rgb: np.ndarray) -> list[Landmark] | None:
        """RGB 이미지에서 33개 관절 랜드마크를 감지한다.

        Returns:
            감지 성공 시 Landmark 리스트, 실패 시 None
        """
        results = self._pose.process(image_rgb)
        if not results.pose_landmarks:
            return None

        landmarks = []
        for i, lm in enumerate(results.pose_landmarks.landmark):
            landmarks.append(
                Landmark(
                    index=i,
                    x=lm.x,
                    y=lm.y,
                    z=lm.z,
                    visibility=lm.visibility,
                )
            )
        return landmarks

    def close(self):
        self._pose.close()


# 스레드별 인스턴스 — MediaPipe Pose는 thread-safe 하지 않으므로 호출 스레드마다 분리.
_thread_local = threading.local()


def get_detector() -> PoseDetector:
    detector = getattr(_thread_local, "detector", None)
    if detector is None:
        detector = PoseDetector()
        _thread_local.detector = detector
    return detector


# ── 세션별 검출기 풀 (#164) ────────────────────────────────────────────────────
#
# 왜 «스레드 로컬» 이 아니라 «세션» 인가:
#   `_thread_local` 은 트래킹 상태를 «스레드» 에 붙인다. 그런데 FastAPI 는 요청마다 아무 idle
#   스레드나 집어가므로, 그 스레드가 직전에 본 것이 다른 세션이면 트래킹이 깨진다.
#   실측(loadtest/results/thread-collision-2026-08-11/): 실사용 3fps 에서 충돌률 44~78%,
#   검출률 33~55% (세션 전용이면 96%) — **손실 41~63%p.**
#   그리고 «바쁠수록 안전하고 한가할수록 위험» 하다. 3fps 가 실사용 값이라 평시에 계속 샜다.
#
# 왜 «풀» 인가(무제한 dict 가 아니라):
#   검출기 1개 = **98.7MB**(M2 실측, loadtest/results/detector-memory-2026-08-11/).
#   세션마다 무제한으로 만들면 메모리가 세션 수에 비례해 늘고, 지금까지 «스레드 수» 가
#   저절로 씌워주던 뚜껑이 사라진다. 풀 크기가 곧 **동시 활성 세션 상한**이다.
#
# 왜 락이 필요한가:
#   M1(loadtest/results/detector-portability-2026-08-11/)은 **순차 호출**만 검증했다.
#   클라에 백프레셔가 없어(exercise.tsx:195) 같은 세션 프레임이 겹칠 수 있고, 겹쳐서 같은
#   PoseDetector 를 동시에 부르면 지금보다 나쁘다. 그래서 세션당 락으로 직렬화한다.

_BASE_RSS_MB = 100.5      # 모델 로드 후·검출기 0개 (M2 실측)
_PER_DETECTOR_MB = 98.7   # 검출기 1개당 (M2 실측, 첫 추론 시 지연 할당)


def _cgroup_memory_limit_mb() -> float | None:
    """이 «컨테이너» 에 허용된 메모리(MB). 한도가 없으면 None.

    호스트 전체가 아니라 내 몫을 봐야 한다 — 한도가 없으면 AI 가 MySQL 몫까지 자기 것으로
    착각하고 상한을 계산한다.
    """
    for path, unlimited in (("/sys/fs/cgroup/memory.max", "max"),                    # v2
                            ("/sys/fs/cgroup/memory/memory.limit_in_bytes", None)):  # v1
        try:
            with open(path) as f:
                raw = f.read().strip()
        except OSError:
            continue
        if raw == unlimited:
            return None
        try:
            v = int(raw)
        except ValueError:
            continue
        # v1 은 «한도 없음» 을 아주 큰 수로 표현한다. 1TB 넘으면 한도가 아니라고 본다.
        if v <= 0 or v > (1 << 40):
            return None
        return v / 1024 / 1024
    return None


def memory_ceiling() -> int | None:
    """이 컨테이너 메모리로 가능한 검출기 «상한». 한도가 없으면 None.

    ⚠️ 검출기«만» 계산한다. 프레임 버퍼·base64 임시·파이썬 힙·gRPC 는 **미측정**이라 안 뺐다.
       그러니 컨테이너 한도 자체에 여유를 두라 — 여기서 임의의 «여유분» 을 빼면 그게 또
       근거 없는 기준값이 된다.
    """
    limit = _cgroup_memory_limit_mb()
    if limit is None:
        return None
    return max(1, int((limit - _BASE_RSS_MB) / _PER_DETECTOR_MB))


class _Lease:
    """`with` 안에서만 검출기를 쓴다 — 세션 락으로 직렬화한다."""

    __slots__ = ("_detector", "_lock")

    def __init__(self, detector: "PoseDetector", lock):
        self._detector = detector
        self._lock = lock

    def __enter__(self) -> "PoseDetector":
        if self._lock is not None:
            self._lock.acquire()
        return self._detector

    def __exit__(self, *exc):
        if self._lock is not None:
            self._lock.release()
        return False


class DetectorPool:
    """세션 → 검출기. 크기가 곧 동시 활성 세션 상한이다."""

    def __init__(self, capacity: int):
        self._capacity = capacity
        self._guard = threading.Lock()
        self._detectors: dict[int, PoseDetector] = {}
        self._locks: dict[int, threading.Lock] = {}

    @property
    def capacity(self) -> int:
        return self._capacity

    def acquire(self, session_id: int) -> bool:
        """세션에 자리를 배정한다. 이미 있으면 True(재부착·중복 호출), 자리가 없으면 False.

        검출기는 여기서 «만들지만» 메모리는 첫 추론에서 붙는다(M2: 생성 0.1MB / 추론 98.5MB).
        즉 시작만 하고 프레임을 안 보내는 세션은 자리만 차지하고 메모리는 안 먹는다.
        """
        with self._guard:
            if session_id in self._detectors:
                return True
            if len(self._detectors) >= self._capacity:
                return False
            self._detectors[session_id] = PoseDetector()
            self._locks[session_id] = threading.Lock()
            return True

    def lease(self, session_id: int) -> _Lease | None:
        with self._guard:
            det = self._detectors.get(session_id)
            lock = self._locks.get(session_id)
        return None if det is None else _Lease(det, lock)

    def release(self, session_id: int) -> bool:
        """자리를 반납하고 `close()` 한다. M2 에서 회수율 100% 를 확인했다."""
        with self._guard:
            det = self._detectors.pop(session_id, None)
            self._locks.pop(session_id, None)
        if det is None:
            return False
        det.close()
        return True

    def status(self) -> tuple[int, int]:
        with self._guard:
            return len(self._detectors), self._capacity

    def shutdown(self) -> int:
        """남아 있는 검출기를 전부 닫는다. 반환값 = 닫은 개수(= 종료 시점의 활성 세션 수).

        프로세스가 죽으면 OS 가 회수하므로 «누수» 는 아니다. 그래도 명시적으로 닫는 이유:
        진행 중 세션이 몇 개인 채로 내려갔는지가 **로그에 남는다.** 무중단 배포가 없는 지금
        (배포 대상 0대) 그 숫자가 곧 «배포 때 몇 명이 끊겼나» 다.
        """
        with self._guard:
            dets = list(self._detectors.values())
            self._detectors.clear()
            self._locks.clear()
        for d in dets:
            try:
                d.close()
            except Exception:                      # 종료 경로다 — 하나 실패해도 나머지는 닫는다
                pass
        return len(dets)


_pool: DetectorPool | None = None
_pool_guard = threading.Lock()


def get_pool() -> DetectorPool:
    """풀 크기 결정: 설정값이 있으면 그 값, 없으면(0) 메모리 상한. 상한을 넘으면 클램프한다."""
    global _pool
    if _pool is not None:
        return _pool
    with _pool_guard:
        if _pool is not None:
            return _pool
        import logging

        log = logging.getLogger(__name__)
        ceiling = memory_ceiling()
        want = settings.POSE_DETECTOR_POOL_SIZE

        if want <= 0:
            if ceiling is None:
                raise RuntimeError(
                    "🔴 풀 크기를 정할 수 없다. 컨테이너에 메모리 한도가 없고 "
                    "POSE_DETECTOR_POOL_SIZE 도 안 줬다. 둘 중 하나는 있어야 한다 — "
                    "근거 없이 기본값을 박으면 그게 출처 없는 기준값이 된다."
                )
            size = ceiling
            log.info("검출기 풀 크기 = %d (컨테이너 메모리 한도에서 유도, 검출기 %.1fMB/개)",
                     size, _PER_DETECTOR_MB)
        elif ceiling is not None and want > ceiling:
            size = ceiling
            log.warning("🔴 POSE_DETECTOR_POOL_SIZE=%d 는 메모리 상한 %d 를 넘는다 → %d 로 낮춘다. "
                        "더 받으려면 컨테이너 메모리 한도를 올릴 것.", want, ceiling, size)
        else:
            size = want
            log.info("검출기 풀 크기 = %d (설정값, 메모리 상한 %s)", size, ceiling)

        log.warning("⚠️ 이 상한은 «검출기만» 계산한 값이다. 프레임 버퍼·파이썬 힙은 미측정이라 "
                    "포함되지 않았다. 컨테이너 한도에 여유를 둘 것.")
        _pool = DetectorPool(size)
        return _pool


def lease_detector(session_id: int | None):
    """세션이 있으면 그 세션 전용 검출기, 없으면 기존 stateless 경로(스레드 로컬).

    세션이 있는데 자리가 없으면 None — 호출부가 거절해야 한다.
    """
    if session_id is None:
        return _Lease(get_detector(), None)     # stateless: 세션이 없으니 섞일 것도 없다
    return get_pool().lease(session_id)
