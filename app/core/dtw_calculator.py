"""DTW-based sync helpers plus frontend-facing cue classification."""

import numpy as np
from dtaidistance import dtw

from app.config import settings
from app.models.sync import SyncHapticCue, SyncVisualCue


def compute_dtw_distance(
    seq_ref: list[list[float]],
    seq_user: list[list[float]],
) -> float:
    """Compute normalized DTW distance across multi-angle sequences."""
    ref = np.array(seq_ref, dtype=np.float64)
    user = np.array(seq_user, dtype=np.float64)

    total_distance = 0.0
    num_joints = ref.shape[1]

    for joint_index in range(num_joints):
        dist = dtw.distance(
            ref[:, joint_index],
            user[:, joint_index],
            window=settings.DTW_WINDOW_SIZE,
        )
        total_distance += dist

    max_len = max(len(seq_ref), len(seq_user))
    normalized = total_distance / (num_joints * max_len + 1e-8)
    return float(normalized)


def compute_sync_rate(
    seq_ref: list[list[float]],
    seq_user: list[list[float]],
) -> float:
    """Convert DTW distance into a 0-100 sync rate."""
    distance = compute_dtw_distance(seq_ref, seq_user)
    rate = 100.0 * np.exp(-distance / 30.0)
    return round(float(np.clip(rate, 0.0, 100.0)), 2)


def classify_sync_visual_cue(sync_rate: float) -> SyncVisualCue:
    """Map sync rate to frontend visual guidance."""
    if sync_rate >= 70:
        return SyncVisualCue(
            zone="green",
            color="green",
            flashing=False,
            animation="steady",
            message="싱크로율 양호",
        )
    if sync_rate >= 40:
        return SyncVisualCue(
            zone="orange",
            color="orange",
            flashing=False,
            animation="steady",
            message="자세 보정 필요",
        )
    return SyncVisualCue(
        zone="red",
        color="red",
        flashing=True,
        animation="twinkle_flash",
        message="즉시 자세 수정 필요",
    )


def classify_sync_haptic_cue(sync_rate: float) -> SyncHapticCue:
    """Map sync rate to frontend haptic guidance."""
    if sync_rate < 40:
        return SyncHapticCue(
            enabled=True,
            pattern="warning_repeat",
            message="40% 미만 진입 시 강한 경고 진동",
        )
    if sync_rate < 70:
        return SyncHapticCue(
            enabled=True,
            pattern="light_repeat",
            message="40~70% 구간 유지 시 가벼운 반복 진동",
        )
    return SyncHapticCue(
        enabled=False,
        pattern="off",
        message="진동 없음",
    )
