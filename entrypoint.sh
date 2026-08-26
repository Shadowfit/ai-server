#!/bin/bash
# 워커 N개를 SO_REUSEPORT 공유 포트가 아니라 각자 다른 포트로 띄운다.
# 이유(2026-08-26 실측): --workers 3 로 포트를 공유시키면 커널이 프레임 HTTP 요청을
# 세션과 무관하게 아무 워커에나 분산시켜, 세션 상태(get_registry)가 없는 워커로 가는
# 프레임이 NO_LEASE 로 거절됐다(6건 중 4건, 67%). Spring 이 세션 시작 시 워커 인덱스를
# 프론트에 알려주려면 워커가 애초에 "다른 주소"여야 한다 — 같은 주소를 공유하면 알려줄 게 없다.
#
# 워커 수는 AI_WORKER_COUNT 환경변수(docker-compose 의 shadowfit-ai 서비스) 하나로 정한다.
# Spring 쪽(ExerciseAnalysisService.java 의 aiChannelPoolSize)도 같은 compose 변수
# (${AI_WORKER_COUNT})를 읽으므로 여기서 값을 바꾸는 것만으로 둘이 같이 움직인다
# (docs/decisions/ai-channel-pool-hardening.md).
#
# 포트는 base(HTTP 8000·gRPC 8585) + 워커 인덱스로 산술 유도한다 — 목록을 손으로 안 늘린다.
#
# 🔴 nginx-ai(default.conf)는 이 변수를 못 읽는다(정적 설정 파일이라 반복문이 없음).
# 대신 base+인덱스 공식을 그대로 믿고 워커 0~9 자리를 미리 다 열어뒀다 — AI_WORKER_COUNT 가
# 10 을 넘지 않는 한 nginx 는 손댈 필요가 없다. 넘긴다면 nginx-ai/default.conf 의 map 블록에
# 그만큼 줄을 추가해야 한다(이 파일의 base 포트 공식과 반드시 맞춰서).
set -e

WORKER_COUNT="${AI_WORKER_COUNT:-3}"
HTTP_BASE_PORT=8000
GRPC_BASE_PORT=8585

for i in $(seq 0 $((WORKER_COUNT - 1))); do
  AI_GRPC_PORT="$((GRPC_BASE_PORT + i))" AI_WORKER_COUNT="$WORKER_COUNT" \
    uvicorn app.main:app --host 0.0.0.0 --port "$((HTTP_BASE_PORT + i))" &
done

# 하나라도 죽으면 컨테이너 전체를 내린다 — 일부만 살아있는 상태를 정상으로 보이게
# 두지 않기 위해서다(#430 의 「거짓 healthy」와 같은 원칙).
wait -n
exit $?
