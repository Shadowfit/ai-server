# 스쿼트 데모 가이드

## 20kg 바벨 스쿼트 샘플

샘플 영상:

```text
C:\Users\hojin\Desktop\shadowfit\squat.mp4
```

이 영상은 20kg 바벨을 짊어진 스쿼트 샘플입니다. 현재 분석기는 영상에서 포즈 랜드마크와 관절 각도를 추출하며, 바벨 중량 자체를 계산에 직접 반영하지는 않습니다. 대신 샘플 파일명과 reference JSON 이름에 `20kg_bar`를 붙여 데이터 구분이 가능하도록 사용합니다.

분석 결과 요약:

- 감지된 반복 수: 10회
- 현재 단계: `transition`
- 최저 무릎 각도: `34.39`
- 평균 상체 기울기: `23.35`
- 품질 점수: `100`
- 유효 프레임 비율: `0.97`
- 피드백: `Stable squat pattern detected. This video is good for a live demo.`

전체 분석 결과는 루트의 `squat_analysis.json`에 저장했습니다.

## 빠른 실행

AI 서버 디렉터리에서 실행합니다.

```bash
cd init/ai-server
```

로컬 샘플 영상을 분석합니다.

```bash
DEBUG=false ../../.venv/Scripts/python.exe -m scripts.run_squat_demo ../../squat.mp4 --output ../../squat_analysis.json
```

Windows PowerShell에서 환경변수를 따로 지정해야 하면 아래처럼 실행합니다.

```powershell
$env:DEBUG='false'
..\..\.venv\Scripts\python.exe -m scripts.run_squat_demo ..\..\squat.mp4 --output ..\..\squat_analysis.json
```

API로도 동일하게 분석할 수 있습니다.

```bash
curl -X POST "http://127.0.0.1:8000/api/v1/video/analyze" ^
  -F "file=@../../squat.mp4" ^
  -F "exercise_type=squat"
```

## 기준 자세 생성

20kg 바벨 스쿼트 영상을 기준 자세 샘플로 쓰려면 아래 명령을 실행합니다.

```powershell
$env:DEBUG='false'
..\..\.venv\Scripts\python.exe -m scripts.generate_reference_squat ..\..\squat.mp4 --output reference_data\squat_20kg_bar_reference.json
```

현재 생성된 기준 파일:

```text
init/ai-server/reference_data/squat_20kg_bar_reference.json
```

생성 결과:

- 감지된 총 반복 수: 7회
- 기준 생성에 사용한 반복 수: 5회
- 정규화 길이: 30 프레임

`reference_data/squat_20kg_bar_reference.json`은 이후 DTW 비교의 기준 시퀀스로 사용할 수 있습니다.

## 출력 데이터

분석 응답에는 아래 정보가 포함됩니다.

- `frames[].squat_metrics`: 무릎 각도, 골반 각도, 상체 기울기, 골반 높이, 동작 단계, 반복 횟수
- `squat_analysis.reps_detected`: 감지된 스쿼트 반복 수
- `squat_analysis.current_phase`: 현재 동작 단계
- `squat_analysis.quality_score`: 자세 품질 점수
- `squat_analysis.feedback`: 교정 피드백 문구

## 촬영 가이드

- 각도: 카메라는 몸 기준 90도 측면에 두고, 전신이 한 화면에 보이게 촬영합니다.
- 높이: 카메라 높이는 골반 높이 정도가 가장 안정적입니다.
- 거리: 카메라와 2~3m 정도 거리를 두고 머리부터 발끝까지 화면 안에 들어오게 맞춥니다.
- 조명: 앞쪽이나 측면에서 밝게 비춰 관절이 또렷하게 보이게 합니다.
- 복장: 골반, 무릎, 발목 라인이 잘 보이도록 몸선이 드러나는 옷이 유리합니다.
- 거울 주의: 거울이나 반사체가 프레임에 들어오면 사람을 중복 인식할 수 있어 가능하면 제외합니다.
- 동작 속도: 너무 빠르게 하지 말고 2~3초 정도 천천히 내려갔다 올라오는 동작으로 촬영합니다.
