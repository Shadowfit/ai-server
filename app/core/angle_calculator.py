"""관절 각도 계산 모듈."""

import numpy as np

from app.models.pose import Landmark
from app.utils.constants import EXERCISE_ANGLES, LANDMARK


def calculate_angle(a: Landmark, b: Landmark, c: Landmark) -> float:
    """세 관절 좌표(a, b=피벗, c)로 각도를 계산한다.

    Returns:
        각도 (0~180도)
    """
    vec_ba = np.array([a.x - b.x, a.y - b.y, a.z - b.z])
    vec_bc = np.array([c.x - b.x, c.y - b.y, c.z - b.z])

    cosine = np.dot(vec_ba, vec_bc) / (
        np.linalg.norm(vec_ba) * np.linalg.norm(vec_bc) + 1e-8
    )
    cosine = np.clip(cosine, -1.0, 1.0)
    return float(np.degrees(np.arccos(cosine)))


def extract_angles(
    landmarks: list[Landmark], exercise_type: str
) -> list[float]:
    """랜드마크에서 운동 유형에 해당하는 관절 각도들을 추출한다."""
    angle_defs = EXERCISE_ANGLES.get(exercise_type)
    if not angle_defs:
        raise ValueError(f"지원하지 않는 운동: {exercise_type}")

    lm_map = {lm.index: lm for lm in landmarks}
    angles = []
    for name_a, name_b, name_c in angle_defs:
        a = lm_map[LANDMARK[name_a]]
        b = lm_map[LANDMARK[name_b]]
        c = lm_map[LANDMARK[name_c]]
        angles.append(round(calculate_angle(a, b, c), 2))
    return angles
