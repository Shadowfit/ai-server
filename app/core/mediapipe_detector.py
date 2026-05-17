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
