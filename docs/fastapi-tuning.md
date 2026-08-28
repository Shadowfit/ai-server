# FastAPI 튜닝 참고 — Spring/Tomcat 대응표

Spring/Tomcat 운영에서 보는 축(로컬캐시·tcpdump·connection pool 설정·thread 수치·Netty)이
ai-server(FastAPI)에서는 뭐에 대응하는지, 그리고 이 repo에 실제로 있는지 정리한다. 지식
매핑이 목적이라 여기 있는 "현황"은 전부 실제 코드/문서를 확인하고 적었다.

같은 내용을 시각적으로 정리한 버전:
https://claude.ai/code/artifact/39023cdd-30e3-448a-881b-6aea11b03a44

## 1. 한눈에 — 대응표

| Spring/Tomcat | FastAPI/Python 대응 | ai-server 현황 |
|---|---|---|
| Caffeine 로컬캐시 | `functools.lru_cache` / `cachetools` | **있음** — §2 |
| tcpdump | 동일(OS 레벨, 프레임워크 무관) | 그대로 적용, §5 |
| HikariCP connection pool | SQLAlchemy pool / `httpx.Limits` / gRPC channel pool | **해당 없음**(아웃바운드 DB·HTTP 클라이언트 자체가 없음), §3 |
| Tomcat max-threads | uvicorn 워커 수 + starlette 내부 threadpool | **있음**, 단 "스레드"가 아니라 "프로세스" 축이 정답이었다 — §4 |
| Netty(Tomcat 대신 선택) | uvicorn(uvloop) 자체가 그 계열 | FastAPI+uvicorn = 이미 ASGI/이벤트루프 모델, §6 |

---

## 2. 로컬 캐시

`app/core/reference_store.py:16` — `@lru_cache(maxsize=1)`로 스쿼트 동기화 기준 데이터셋을
프로세스 메모리에 올려둔다. Caffeine 같은 TTL/사이즈 정책이 필요해지면 `cachetools.TTLCache`가
대응품.

⚠️ ai-server는 워커를 **프로세스**로 쪼갠다(§4) — 같은 포트를 공유하는 스레드풀이 아니라
워커마다 별도 프로세스·별도 포트(`entrypoint.sh`). 그 말은 `lru_cache`도 **워커마다 따로**
채워진다는 뜻: 워커 A가 채운 캐시를 워커 B는 못 본다. 지금 캐시 대상(정적 참조 데이터셋,
`maxsize=1`)은 어차피 전부 프로세스마다 다시 로드돼도 무해하지만, 세션별로 채워지는 캐시를
나중에 추가한다면 이 분리를 감안해야 한다 — 프로세스 간 공유가 필요해지는 순간 로컬 캐시로는
안 되고 Redis 같은 외부 캐시로 넘어간다(운동별 스타일 기준 캐싱 설계에서 이미 같은 결론:
`docs/decisions/reference-style-and-caching.md`).

## 3. Connection pool

Spring의 `application.yml` HikariCP 설정에 대응하는 게 FastAPI 쪽엔 **없다** — 확인해보니
ai-server는 아웃바운드 DB 클라이언트도, 아웃바운드 HTTP 클라이언트도 실제로 안 쓴다
(`requirements.txt`의 `httpx`는 `fastapi.testclient`용, 주석에도 "운영에서도 활용 가능"이라고만
적혀 있고 실제 사용처는 없음 — `app/` 안에 `httpx.` 호출 0건).

이 repo에서 실제로 존재하는 "풀"은 두 개, 둘 다 HikariCP와는 다른 자리에 있다:

- **gRPC 서버 스레드풀** — `app/grpc/server.py:83`, `futures.ThreadPoolExecutor(max_workers=10)`.
  Spring→AI 요청을 처리하는 gRPC 서버 쪽 워커 스레드 수. 🔴 **근거·실측이 아무 데도 없는 순수
  매직넘버다.** `POSE_DETECTOR_POOL_SIZE`(메모리 한도에서 유도 공식 있음)나
  `AI_WORKER_COUNT=3`(2026-08-24 EC2 스윕 실측, §4)과 달리 이 `10`은 어디서도 정당화된 적이
  없다. 리스크는 낮다 — `exercise_servicer.py`의 `StartAnalysis`/`ReattachAnalysis`/
  `StopAnalysis`/`ExtractReferenceData`는 세션 제어용 저빈도 경로고, 프레임당 실제 추론은
  별도인 HTTP `POST /pose`(uvicorn 이벤트루프, §4의 GIL/N=3 실측 대상)를 탄다. 즉 §4의 실측은
  이 스레드풀엔 적용된 적이 없고, 지금은 병목이 아니라서 안 드러날 뿐 "10이 맞다"고 검증된
  것도 아니다. 이슈: [#593](https://github.com/Shadowfit/init/issues/593).
- **MediaPipe 검출기 풀** — `app/core/mediapipe_detector.py`, `POSE_DETECTOR_POOL_SIZE`(설정값,
  컨테이너 메모리 한도에서 유도 — 검출기 1개당 98.7MB 실측, 예: 2026-08-17 판에서 풀=201).
  이게 개념적으로 HikariCP의 "미리 만들어둔 재사용 객체 풀"에 제일 가깝다 — DB 커넥션 대신
  MediaPipe 검출기 인스턴스를 풀링한다.
- **Spring↔AI gRPC channel pool**은 AI 쪽이 아니라 Spring 쪽에 있다 —
  `ExerciseAnalysisService.java`의 `aiChannelPoolSize`, `docs/decisions/ai-channel-pool-hardening.md`.
  워커 수(`AI_WORKER_COUNT`)와 같은 compose 변수를 공유해서 둘이 같이 움직인다.

### 3-1. 왜 동기 gRPC API인가

`grpc.server(ThreadPoolExecutor(...))`(동기 API)를 썼다 — `grpc.aio`(비동기 API)도 대안이었다.
동기 API를 고른 근거는 이미 코드에 있다. `app/api/endpoints/pose.py:106-111`이 HTTP
`/pose` 핸들러를 `async def`가 아니라 `def`로 둔 이유를 이렇게 적었다:

> "MediaPipe 추론, OpenCV 변환, Spring 콜백 gRPC가 모두 동기 블로킹이라 `async def`로 두면
> 이벤트 루프를 점유해 다른 요청을 굶긴다. FastAPI는 `def` 핸들러를 자동으로 threadpool에서
> 실행하므로 그대로 두면 된다."

같은 논리가 gRPC 서버에도 그대로 적용된다 — `exercise_servicer.py`의 핸들러들도
`get_pool()`(MediaPipe 검출기)이나 레퍼런스 영상 파일 I/O처럼 동기 블로킹 호출을 한다.

**"써야 한다(강제)"보다는 "쓰는 게 자연스럽다(선택)"가 정확하다.** `grpc.aio`를 썼어도 동작은
했을 것 — 대신 그 블로킹 호출들을 손으로 `run_in_executor`/`asyncio.to_thread`로 감싸야
했을 것이다. 이 감싸기는 공짜가 아니다:

- **매 hop마다 고정비가 든다** — 콜러블을 스레드풀 큐에 넣고, 스레드가 끝나면
  `call_soon_threadsafe`류로 이벤트 루프에 재개 신호를 보내야 한다(루프가 `epoll_wait` 등에서
  블로킹 중이면 self-pipe/소켓으로 깨워야 함). 동기 API는 이 왕복이 없다 — RPC가 처음부터
  그 스레드에서 시작해 끝날 때까지 그 스레드 안이라, 이벤트 루프에 알릴 일 자체가 없다.
- **감싸는 횟수만큼 왕복이 늘어난다** — 핸들러 하나 안에서 블로킹 호출이 여러 개면, 세심하게
  하나로 묶지 않는 한 왕복도 여러 번 나갈 수 있다.

⚠️ 이 오버헤드의 절대 크기는 이 repo에서 측정한 적 없다 — **추측이지 실측이 아니다.** 다만
방향은 근거가 있다: `grpc.aio`를 썼다면 그 왕복이 `/pose`가 이미 쓰는 **같은 이벤트 루프**를
타게 된다. `docs/decisions/per-process-ceiling-cause.md`(§4)가 "이벤트 루프 지연(축 2)"을 GIL
후보 중 하나로 직접 재야 했을 만큼 그 루프는 이미 조사 대상이었다 — gRPC 세션 제어 호출까지
같은 루프에 얹었다면 그 조사가 더 복잡해졌을 것이다. 지금 구조(별도 스레드 + 별도 스레드풀)는
gRPC를 그 루프에서 완전히 떼어놔서, 이미 스크루티니 대상이던 자원에 부하를 안 얹는다는 이점이
있다 — 단, 이건 코드·문서 근거로 짜맞춘 추론이고 실측으로 검증된 결론은 아니다.

## 4. Thread 수치 — 실측으로 "스레드가 아니라 프로세스"로 닫힌 질문

Tomcat의 `max-threads`처럼 "숫자 하나 올리면 처리량이 는다"는 감각이 FastAPI/Python엔
그대로 안 옮겨간다. 이 repo가 EC2 실측으로 직접 검증한 결론:

- **원인은 GIL이다** (2026-08-24, `c7i.4xlarge` 물리 8코어/16 vCPU 박스). 프로세스 하나가
  16 vCPU 중 9.5 vCPU를 못 넘는 천장의 정체는 GIL 직렬화 — 순수 파이썬 경로(이벤트 루프·
  요청 처리 글루 코드)가 정확히 **1.04 vCPU**에서 막히고, MediaPipe 추론 자체는 C++ 안에서
  GIL을 놓아 **~8.5 vCPU**까지 병렬로 쓴다. GIL 점유율 직접 프로브로 잰 값(`rho ≥ 0.939`).
  근거: `docs/decisions/per-process-ceiling-cause.md` §8,
  `loadtest/results/ceiling-cause-stage2-2026-08-24/README.md`.
  - ⚠️ 6일 전(2026-08-17) R6는 간접 측정(스레드 vs 프로세스 처리량 비율)으로 "GIL 아님"이라고
    결론 냈었다(`loadtest/results/ai-scaling-aws-2026-08-17/README.md`). 08-24의 직접 측정(GIL
    대기시간 프로브)이 이를 뒤집었다 — 간접 지표만으로 GIL 여부를 판단하면 틀릴 수 있다는
    실사례.
- **그래서 처방은 "스레드 늘리기"가 아니라 "프로세스로 쪼개기"다.** `entrypoint.sh`가 하는 게
  정확히 이거 — uvicorn 워커를 스레드가 아니라 **프로세스**로 띄우고(`AI_WORKER_COUNT`),
  각자 다른 포트를 쓴다. 같은 포트를 SO_REUSEPORT로 공유하지 않는 이유도 남아있다: 세션
  상태가 워커별로 있어서, 커널이 아무 워커에나 요청을 뿌리면 세션 없는 워커가 요청을
  거절한다(entrypoint.sh 주석, 2026-08-26 실측: 6건 중 4건 NO_LEASE).
- **워커 수 N=3이 이 박스에서 최적**이었다(2026-08-24 N 스윕): 3→4는 처리량 +1.24%뿐, 판 간
  산포(6.36%) 안에 묻힘. 두 번째 천장(14.5 vCPU)의 정체는 GIL이 아니라 **박스 포화**(N=3에
  이미 박스의 98%, N=4에 100%) — 더 큰 박스에서만 풀린다.
  근거: `loadtest/results/proc-count-sweep-2026-08-24/README.md`.
  ⚠️ N=3은 **이 박스 크기에서의 실측값**이지 보편 상수가 아니다 — 다른 vCPU 수의 박스라면
  다시 재야 한다([[feedback_no_arbitrary_threshold_values]] 원칙: 실측 없이 다른 박스에 그대로
  옮기지 말 것).
- **아직 안 잰 것** (정직하게 열려 있음): 스티키 라우팅 자체의 비용(`ai-sticky-routing.md`),
  RAM이 N을 제약하는지(프로세스 오버헤드는 N에 비례하나 아직 크기 미측정), N>4, 더 큰
  박스에서의 재현.

gRPC 서버 스레드풀(`max_workers=10`, §3)과 starlette의 내부 threadpool(sync 함수를
`anyio.to_thread`로 오프로드할 때 쓰는 스레드, `app/observability/frame_path.py:470`에서
`current_default_thread_limiter()`로 조회)은 위 GIL 결론과는 다른 층위 — "동시에 몇 개의 sync
블로킹 호출을 받아줄까"를 정하는 것으로, "CPU 바운드 작업을 병렬로 더 돌릴까"와는 다른 질문.
CPU 바운드(추론)는 스레드 수를 올려도 GIL에 막히니 프로세스 축(N)이 답이고, I/O 바운드
블로킹 호출은 스레드 축이 여전히 유효하다.

## 5. tcpdump / 패킷 분석

OS 레벨 도구라 FastAPI 전용 대응품은 없다 — Tomcat이든 uvicorn이든 TCP 계층은 동일하게
보인다. 이 repo의 토폴로지에서 짚어둘 것:

- 프론트→AI 프레임 경로는 nginx-ai가 앞단에 있고(`nginx-ai/default.conf`), `X-AI-Worker`
  헤더 값으로 워커별 포트(8000~8009)에 정적 map 라우팅한다. 패킷 레벨에서 이상이 있으면
  "어느 워커 포트로 갔는지"부터 nginx 로그/헤더로 좁히는 게 tcpdump보다 싸다.
- 상관관계 추적이 필요하면 tcpdump보다 먼저 correlation-id 기반 로깅이 이미 있다
  (`CorrelationServerInterceptor`, gRPC 쪽; `docs/decisions/observability-correlation-id.md`).
  패킷 캡처는 그걸로도 안 잡히는 TCP 레벨 문제(재전송·RST·핸드셰이크 지연)에만 쓰는 게 맞다.

## 6. Netty 대신 씀 → 이미 uvicorn이 그 역할

Tomcat(스레드-per-요청, blocking I/O, Servlet/WSGI 계열) vs Netty(이벤트루프, non-blocking)
대비는, FastAPI 생태계에서는 Flask+Gunicorn(WSGI) vs **FastAPI+uvicorn(ASGI, uvloop 기반
이벤트루프)** 대비로 그대로 옮겨간다. 즉 "FastAPI용 Netty가 따로 있냐"가 아니라, FastAPI를
uvicorn으로 띄우는 순간 이미 Netty와 같은 이벤트루프 모델을 쓰고 있는 것 — §4에서 본 GIL
천장은 그 이벤트루프 자체가 파이썬 인터프리터 안에서 도는 데서 오는 문제지, Tomcat식
스레드-per-요청 모델로 돌아가서 생기는 문제가 아니다.

## 7. 참고

- `ai-server/entrypoint.sh` — 워커=프로세스, 포트 분리, `AI_WORKER_COUNT`
- `nginx-ai/default.conf` — 워커별 정적 라우팅 map
- `app/core/reference_store.py`, `app/grpc/server.py`, `app/core/mediapipe_detector.py`,
  `app/observability/frame_path.py`
- `docs/decisions/per-process-ceiling-cause.md` — GIL 원인 규명 설계·결과
- `docs/decisions/experiment-inventory.md` (AI 축 1번) — 정본 요약
- `docs/decisions/ai-channel-pool-hardening.md` — Spring 쪽 gRPC channel pool
- `docs/decisions/reference-style-and-caching.md` — 로컬 캐시가 언제 Redis로 넘어가는지
- `loadtest/results/ceiling-cause-stage2-2026-08-24/README.md`,
  `loadtest/results/proc-count-sweep-2026-08-24/README.md`,
  `loadtest/results/ai-scaling-aws-2026-08-17/README.md`
- [#593](https://github.com/Shadowfit/init/issues/593) — gRPC 스레드풀 `max_workers=10` 미검증 (§3)
