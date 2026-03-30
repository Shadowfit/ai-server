"""DTW(Dynamic Time Warping) 동기화율 계산 모듈."""

import numpy as np
from dtaidistance import dtw

from app.config import settings


def compute_dtw_distance(
    seq_ref: list[list[float]],
    seq_user: list[list[float]],
) -> float:
    """두 관절 각도 시퀀스 간 DTW 거리를 계산한다.

    Args:
        seq_ref: 참고 영상의 각도 시퀀스 [[angle1, angle2, ...], ...]
        seq_user: 사용자의 각도 시퀀스 [[angle1, angle2, ...], ...]

    Returns:
        정규화된 DTW 거리 (0에 가까울수록 유사)
    """
    ref = np.array(seq_ref, dtype=np.float64)
    user = np.array(seq_user, dtype=np.float64)

    total_distance = 0.0
    num_joints = ref.shape[1]

    for j in range(num_joints):
        dist = dtw.distance(
            ref[:, j],
            user[:, j],
            window=settings.DTW_WINDOW_SIZE,
        )
        total_distance += dist

    # 관절 수와 시퀀스 길이로 정규화
    max_len = max(len(seq_ref), len(seq_user))
    normalized = total_distance / (num_joints * max_len + 1e-8)
    return float(normalized)


def compute_sync_rate(
    seq_ref: list[list[float]],
    seq_user: list[list[float]],
) -> float:
    """두 시퀀스의 동기화율(%)을 계산한다.

    Returns:
        동기화율 (0.0 ~ 100.0)
    """
    distance = compute_dtw_distance(seq_ref, seq_user)
    # 거리 → 유사도 변환 (시그모이드 기반)
    # 거리 0 → 100%, 거리가 클수록 0%에 수렴
    rate = 100.0 * np.exp(-distance / 30.0)
    return round(float(np.clip(rate, 0.0, 100.0)), 2)
