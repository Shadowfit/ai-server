"""MediaPipe Pose 랜드마크 인덱스 및 운동별 관절 설정."""

# MediaPipe Pose 33개 랜드마크 인덱스
LANDMARK = {
    "NOSE": 0,
    "LEFT_SHOULDER": 11,
    "RIGHT_SHOULDER": 12,
    "LEFT_ELBOW": 13,
    "RIGHT_ELBOW": 14,
    "LEFT_WRIST": 15,
    "RIGHT_WRIST": 16,
    "LEFT_HIP": 23,
    "RIGHT_HIP": 24,
    "LEFT_KNEE": 25,
    "RIGHT_KNEE": 26,
    "LEFT_ANKLE": 27,
    "RIGHT_ANKLE": 28,
}

# 운동별 주요 관절 각도 정의
# 각 항목: (관절A, 피벗, 관절B) → 피벗을 기준으로 A-피벗-B 각도 계산
EXERCISE_ANGLES = {
    "squat": [
        ("LEFT_HIP", "LEFT_KNEE", "LEFT_ANKLE"),      # 왼쪽 무릎 각도
        ("RIGHT_HIP", "RIGHT_KNEE", "RIGHT_ANKLE"),    # 오른쪽 무릎 각도
        ("LEFT_SHOULDER", "LEFT_HIP", "LEFT_KNEE"),    # 왼쪽 엉덩이 각도
        ("RIGHT_SHOULDER", "RIGHT_HIP", "RIGHT_KNEE"), # 오른쪽 엉덩이 각도
    ],
    "deadlift": [
        ("LEFT_SHOULDER", "LEFT_HIP", "LEFT_KNEE"),    # 왼쪽 힙 힌지
        ("RIGHT_SHOULDER", "RIGHT_HIP", "RIGHT_KNEE"), # 오른쪽 힙 힌지
        ("LEFT_HIP", "LEFT_KNEE", "LEFT_ANKLE"),       # 왼쪽 무릎 각도
        ("RIGHT_HIP", "RIGHT_KNEE", "RIGHT_ANKLE"),    # 오른쪽 무릎 각도
    ],
    "pullup": [
        ("LEFT_SHOULDER", "LEFT_ELBOW", "LEFT_WRIST"),   # 왼쪽 팔꿈치 각도
        ("RIGHT_SHOULDER", "RIGHT_ELBOW", "RIGHT_WRIST"), # 오른쪽 팔꿈치 각도
        ("LEFT_HIP", "LEFT_SHOULDER", "LEFT_ELBOW"),      # 왼쪽 어깨 각도
        ("RIGHT_HIP", "RIGHT_SHOULDER", "RIGHT_ELBOW"),   # 오른쪽 어깨 각도
    ],
}

# 동기화율 임계값 (페르소나별)
SYNC_THRESHOLDS = {
    "BEGINNER": 0.60,
    "ADVANCED": 0.85,
    "DIET": 0.70,
    "REHAB": 0.50,
}
