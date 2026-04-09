# 스쿼트 데모 가이드

## 빠른 실행 순서

1. 측면 스쿼트 영상을 촬영합니다.

```bash
python -m scripts.record_demo_video --seconds 10
```

2. 촬영한 영상을 분석합니다.

```bash
python -m scripts.run_squat_demo demo_videos/demo_squat.mp4 --output demo_videos/demo_squat_analysis.json
```

3. 실시간 시연 화면이 필요하면 아래 스크립트를 실행합니다.

```bash
python -m scripts.live_squat_demo
```

4. 기준 자세 JSON이 필요하면 아래 스크립트로 생성합니다.

```bash
python -m scripts.generate_reference_squat demo_videos/guide_squat.mp4 --output reference_data/squat_reference.json
```

5. API로도 동일하게 분석할 수 있습니다.

```bash
curl -X POST "http://127.0.0.1:8000/api/v1/video/analyze" ^
  -F "file=@demo_videos/demo_squat.mp4" ^
  -F "exercise_type=squat"
```

응답에는 아래 정보가 포함됩니다.

- `frames[].squat_metrics`: 무릎 각도, 골반 각도, 상체 기울기, 동작 단계, 반복 횟수
- `squat_analysis.reps_detected`: 감지된 스쿼트 횟수
- `squat_analysis.current_phase`: 현재 동작 단계
- `squat_analysis.quality_score`: 자세 품질 점수
- `squat_analysis.feedback`: 교정 피드백 문구

실시간 시연 화면에서는 아래 항목을 바로 확인할 수 있습니다.

- 관절 스켈레톤 오버레이
- 평균 무릎 각도
- 현재 스쿼트 단계
- 반복 횟수
- 대표 피드백 1개

## 기준 자세 생성 로직

- 기준 영상은 측면에서 3~5회 정도의 안정적인 스쿼트를 촬영하는 것을 권장합니다.
- 서버는 기준 영상에서 반복 구간을 자동으로 분리합니다.
- 분리된 각 반복에서 무릎 각도, 골반 각도 등 각도 시퀀스를 추출합니다.
- 각 반복의 깊이, 상체 안정성, 프레임 수를 기준으로 점수를 매겨 품질이 좋은 반복을 우선 선택합니다.
- 선택된 반복들은 길이를 동일하게 맞춘 뒤 평균 시퀀스로 합쳐 대표 기준 자세를 생성합니다.
- 최종 결과는 `reference_data/squat_reference.json` 형태의 JSON으로 저장되며, 이후 DTW 비교의 기준 시퀀스로 사용할 수 있습니다.

## 프론트 연동 기준

- `POST /api/v1/sync`
  - `sync_rate >= 70`: `visual_cue.zone = green`, `haptic_cue.pattern = off`
  - `40 <= sync_rate < 70`: `visual_cue.zone = orange`, `haptic_cue.pattern = light_repeat`
  - `sync_rate < 40`: `visual_cue.zone = red`, `visual_cue.flashing = true`, `visual_cue.animation = twinkle_flash`, `haptic_cue.pattern = warning_repeat`
- `GET /api/v1/sync/onboarding-guide`
  - 온보딩 Step 4용 촬영 가이드 4개 항목을 반환합니다: 각도, 거리, 조명, 거울 주의

## 촬영 가이드

- 각도: 카메라는 몸 옆 90도 측면에 두고, 전신이 한 평면에서 보이도록 촬영합니다.
- 높이: 카메라 높이는 골반 높이 정도에 맞추는 것이 가장 안정적입니다.
- 거리: 카메라와 2~3m 정도 거리를 두고 머리부터 발끝까지 화면 안에 모두 들어오게 맞춥니다.
- 조명: 역광을 피하고 정면 또는 측면에서 밝게 비춰 관절이 또렷하게 보이게 합니다.
- 복장: 골반, 무릎, 발목 라인이 잘 보이도록 몸선이 드러나는 옷이 유리합니다.
- 거울 주의: 거울이나 반사체가 프레임에 들어오면 사람을 중복 인식할 수 있어 가능한 한 피합니다.
- 동작 속도: 너무 빠르게 하지 말고 2~3회 정도 천천히 내려갔다 올라오는 동작으로 촬영합니다.

## 출력 데이터 기준

캡스톤 데모 기준으로 출력 데이터는 아래처럼 3단계로 봅니다.

- 원시 데이터: 정규화된 관절 좌표 `x`, `y`, `z`, `visibility`
- 중간 특징값: `knee_angle`, `hip_angle`, `torso_tilt`, `hip_height`
- 최종 결과: `reps_detected`, `current_phase`, `quality_score`, `feedback`
