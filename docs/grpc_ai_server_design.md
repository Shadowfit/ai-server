# FastAPI(AI 서버) 최종 설계 명세

## 1. `AuthInterceptor` 클래스

역할: Spring에서 들어오는 모든 gRPC 요청의 인증 토큰을 검사하는 보안 담당 컴포넌트입니다.

### 핵심 책임

- gRPC 메타데이터 헤더의 `Authorization` 값을 읽습니다.
- Spring과 사전에 합의한 `INTERNAL_API_TOKEN`과 일치하는지 검증합니다.
- 토큰이 일치하면 요청을 정상적으로 통과시킵니다.
- 토큰이 없거나 일치하지 않으면 `UNAUTHENTICATED` 오류를 반환합니다.

### 주요 메서드

#### `intercept_service(continuation, handler_call_details)`

처리 흐름:

1. 요청 메타데이터에서 `Authorization` 헤더를 추출합니다.
2. `Bearer {INTERNAL_API_TOKEN}` 형식 또는 합의된 내부 토큰 형식과 비교합니다.
3. 검증 성공 시 원래 서비스 핸들러로 요청을 전달합니다.
4. 검증 실패 시 `grpc.StatusCode.UNAUTHENTICATED`를 반환합니다.

### 구현 포인트

- 실제 토큰 값은 로그에 남기지 않습니다.
- 모든 내부 gRPC 메서드에 공통 적용합니다.
- 서비스 비즈니스 로직보다 먼저 동작해야 합니다.

---

## 2. `ExerciseServicer` 클래스

역할: Spring의 요청을 받아 실제 AI 분석 로직으로 연결하는 gRPC 서버 인터페이스입니다.

이 클래스는 통신 진입점 역할을 맡고, 무거운 분석은 `PoseAnalysisEngine`에 위임합니다.

### 주요 메서드

#### `ExtractReferenceData(request, context)`

역할: 기준 영상으로부터 정석 자세 데이터를 추출합니다.

처리 흐름:

1. Spring으로부터 유튜브 URL 또는 다운로드 가능한 영상 URL을 전달받습니다.
2. 영상을 임시 파일로 다운로드합니다.
3. `PoseAnalysisEngine` 또는 기준 자세 추출 로직을 실행합니다.
4. 전체 좌표 또는 기준 자세 시퀀스를 추출합니다.
5. 결과를 Spring이 사용할 응답 형식으로 변환해 반환합니다.

권장 재사용 로직:

- `app/core/video_processor.py`
- `app/core/reference_builder.py`

#### `StartAnalysis(request, context)`

역할: 운동 분석을 비동기로 시작합니다.

처리 흐름:

1. Spring으로부터 `session_id`와 `reference_poses`를 전달받습니다.
2. 동일한 `session_id`가 이미 실행 중인지 확인합니다.
3. 세션 정보를 메모리에 등록합니다.
4. 백그라운드 스레드에서 `PoseAnalysisEngine.run_analysis()`를 실행합니다.
5. gRPC 응답은 즉시 "분석 시작됨" 상태로 반환합니다.

핵심 포인트:

- 메서드는 블로킹되면 안 됩니다.
- 장시간 실행되는 분석은 `session_id` 기준으로 추적 가능해야 합니다.

#### `StopAnalysis(request, context)`

역할: 실행 중인 분석 루프를 강제로 종료합니다.

처리 흐름:

1. `session_id`를 전달받습니다.
2. 세션 레지스트리에서 해당 세션을 찾습니다.
3. stop flag 또는 cancellation event를 설정합니다.
4. 종료 요청 성공 여부를 응답합니다.

핵심 포인트:

- 세션 조회와 종료 처리는 thread-safe 해야 합니다.

---

## 3. `PoseAnalysisEngine` 클래스

역할: 실제 AI 분석을 수행하고, 동시에 Spring(6565 포트)으로 중간 결과와 최종 결과를 전송하는 엔진입니다.

즉, 이 클래스는 분석 실행자이면서 Spring으로 직접 데이터를 보내는 gRPC 클라이언트 역할도 담당합니다.

### 핵심 책임

- MediaPipe Pose 기반 랜드마크 감지
- 관절 각도 계산
- 기준 자세와 사용자 자세 비교
- DTW 기반 일치율 계산
- 일정 프레임마다 중간 좌표 전송
- 운동 종료 시 최종 통계 전송

### 주요 메서드

#### `run_analysis(session_id, ...)`

역할: 운동 분석 메인 루프를 실행합니다.

처리 흐름:

1. 카메라 또는 영상 입력 소스를 초기화합니다.
2. 프레임마다 `PoseDetector`로 포즈를 감지합니다.
3. `AngleCalculator` 또는 기존 각도 추출 로직으로 관절 각도를 계산합니다.
4. 기준 자세와 비교해 DTW 일치율 또는 싱크율을 계산합니다.
5. 반복 횟수, 단계, 품질 점수, 칼로리 등 통계를 누적합니다.
6. 20~30프레임 단위로 중간 좌표 데이터를 전송합니다.
7. 종료 조건 또는 중지 신호가 오면 최종 결과를 전송하고 루프를 종료합니다.

권장 재사용 로직:

- `app/core/mediapipe_detector.py`
- `app/core/angle_calculator.py`
- `app/core/dtw_calculator.py`
- `app/core/squat_analyzer.py`

#### `stream_intermediate_data(session_id, pose_batch)`

역할: 분석 도중 중간 좌표 데이터를 Spring으로 전송합니다.

처리 흐름:

1. Spring 콜백 서버에 연결할 gRPC client stub을 생성합니다.
2. 메타데이터 헤더에 내부 인증 토큰을 포함합니다.
3. Spring의 `SavePoseDataBatch`를 호출합니다.
4. 실패 시 로그와 재시도 정책을 적용합니다.

중요 사항:

- 호출 주기는 20~30프레임 단위를 권장합니다.
- 모든 호출에는 `internal_token` 또는 `Authorization` 헤더가 반드시 포함되어야 합니다.

예시 메타데이터:

```python
metadata = [("authorization", f"Bearer {settings.INTERNAL_API_TOKEN}")]
```

#### `finalize_analysis(session_id, stats)`

역할: 운동 종료 시 최종 통계를 Spring으로 전송합니다.

처리 흐름:

1. 총 반복 횟수, 칼로리, 품질 점수, 평균 싱크율 등 최종 통계를 구성합니다.
2. 메타데이터 헤더에 내부 인증 토큰을 포함합니다.
3. Spring의 `CompleteAnalysis`를 호출합니다.
4. 전송 완료 후 세션 자원을 정리하고 종료합니다.

중요 사항:

- 모든 호출에는 `internal_token` 또는 `Authorization` 헤더가 반드시 포함되어야 합니다.
- 최종 보고는 중복 전송 방지 전략을 고려해야 합니다.

---

## 4. `PoseDetector`와 `AngleCalculator`

역할: 기존에 보유한 순수 포즈 분석 로직입니다. 이 영역은 통신 계층과 분리된 상태로 유지해야 합니다.

### `PoseDetector`

위치:

- `app/core/mediapipe_detector.py`

책임:

- MediaPipe를 사용해 이미지에서 33개 랜드마크를 감지합니다.
- 후속 분석에 사용할 랜드마크 리스트를 반환합니다.

핵심 메서드:

```python
detect(image) -> list[Landmark] | None
```

### `AngleCalculator`

위치:

- `app/core/angle_calculator.py`

책임:

- 세 점 사이의 관절 각도를 계산합니다.
- 운동 분석 및 DTW 입력 생성의 기초 데이터를 제공합니다.

핵심 메서드:

```python
calculate_angle(a, b, c) -> float
```

---

## 전체 구조 요약

```text
Spring Boot
  -> gRPC 요청 + 내부 토큰
  -> AuthInterceptor
  -> ExerciseServicer
  -> PoseAnalysisEngine
     -> PoseDetector / AngleCalculator / DTW / SquatAnalyzer
     -> Spring:6565로 gRPC 콜백 전송
```

---

## 추가로 필요한 구성 요소

### 세션 관리

- `session_id` 기준 분석 세션 관리
- 중지 신호용 `stop_event`
- 중복 실행 방지
- thread-safe 세션 레지스트리

### 설정값

권장 추가 항목:

```python
INTERNAL_API_TOKEN: str
AI_GRPC_HOST: str
AI_GRPC_PORT: int
SPRING_GRPC_HOST: str
SPRING_GRPC_PORT: int = 6565
GRPC_CALLBACK_TIMEOUT_SEC: float
POSE_BATCH_SIZE: int = 20
```

### gRPC 인터페이스

AI 서버가 받아야 할 메서드:

- `ExtractReferenceData`
- `StartAnalysis`
- `StopAnalysis`

Spring으로 다시 호출해야 할 메서드:

- `SavePoseDataBatch`
- `CompleteAnalysis`

### 권장 모듈 구조

```text
app/
  grpc/
    auth_interceptor.py
    exercise_servicer.py
    spring_client.py
    session_registry.py
    server.py
  services/
    pose_analysis_engine.py
    reference_extraction_service.py
  core/
    mediapipe_detector.py
    angle_calculator.py
    dtw_calculator.py
    squat_analyzer.py
```

---

## 구현 우선순위

1. protobuf 정의
2. `AuthInterceptor` 구현
3. `ExerciseServicer` 구현
4. 세션 레지스트리 구현
5. `PoseAnalysisEngine` 구현
6. Spring 콜백 클라이언트 구현
7. 통합 테스트 추가

---

## 저장소 기준 메모

- 현재 저장소에는 포즈 감지, 각도 계산, DTW, 스쿼트 분석 로직이 이미 존재합니다.
- 따라서 새 설계의 핵심은 분석 로직을 새로 만드는 것이 아니라, gRPC 통신층과 세션 제어, Spring 콜백을 올바르게 연결하는 것입니다.
- 기존 FastAPI REST API는 로컬 디버깅과 보조 테스트 용도로 유지해도 괜찮습니다.
