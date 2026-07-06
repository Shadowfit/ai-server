# ShadowFit AI Server

관절 좌표 기반 AI 자세 분석 피트니스 앱 **ShadowFit**의 자세 분석 서버입니다. MediaPipe로 운동 영상에서 관절 좌표를 추출하고, DTW(Dynamic Time Warping)로 기준 동작과 비교해 동기화율을 산출합니다.

> 이 레포는 [Shadowfit/init](https://github.com/Shadowfit/init) 모노레포의 `ai-server/` 모듈이 자동으로 미러링된 것입니다. 프론트엔드·백엔드를 포함한 전체 구조와 설계 문서는 모노레포 쪽을 참고하세요.

## 기술 스택

- **FastAPI**, Uvicorn
- **MediaPipe** (관절 좌표 추출), OpenCV
- **DTW** (`dtaidistance`) 기반 동작 유사도 비교
- gRPC (Backend와 양방향 통신), Docker

## 실행 방법

```bash
cp .env.example .env   # 값 채우기
pip install -r requirements.txt
uvicorn app.main:app --reload
```

FastAPI(HTTP)와 gRPC 서버가 같은 프로세스에서 백그라운드 스레드로 함께 뜹니다.

## 주요 기능

- MediaPipe로 영상에서 관절 좌표를 실시간 추출
- DTW로 사용자 동작과 스타일별 기준 동작을 비교해 동기화율/구간별 정확도 산출
- Backend ↔ AI Server gRPC 통신 (분석 요청 수신 → 결과 콜백 전송)
- 내부 API 토큰 기반 인증 미들웨어(`InternalAuthMiddleware`)로 Backend 외 호출 차단

## 관련 문서

Backend와의 gRPC 결합 구조, 프로토콜 변경 시 동기화 방법 등은 모노레포의 [`docs/architecture/`](https://github.com/Shadowfit/init/tree/main/docs/architecture)를 참고하세요.
