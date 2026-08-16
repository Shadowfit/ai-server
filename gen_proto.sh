#!/usr/bin/env bash
# gRPC 산출물 재생성 (이슈 #132).
#
# 왜 이 스크립트가 필요한가
# ---------------------------------------------------------------------------
# 이전에는 산출물이 **두 벌** 있었다.
#
#     ai-server/exercise_pb2.py            ← 실제로 로드되던 쪽
#     ai-server/app/proto/exercise_pb2.py  ← .proto 원본 옆인데 아무도 안 쓰던 쪽
#
# 둘 다 git 에 있었고 내용이 같아서 «어느 쪽을 고쳐야 하나» 가 코드로 답이 안 됐다.
# 재생성하기 자연스러운 위치(.proto 옆)와 실제로 실행되는 위치(루트)가 달라서,
# app/proto/ 쪽만 다시 만들면 «필드를 넣었는데 런타임엔 없는» 상태가 된다.
#
# 왜 루트로 내보내는가 (app/proto/ 가 아니라)
# ---------------------------------------------------------------------------
# grpc_tools 가 만드는 exercise_pb2_grpc.py 안에 `import exercise_pb2` 라는
# **최상위 import** 가 박힌다. 이걸 패키지(app/proto/) 안에 두면 그 import 가 깨진다 —
# 되살리려면 생성 결과를 후처리하거나 sys.path 를 건드려야 하는데, 앱 코드
# (exercise_servicer.py·spring_client.py·server.py·pose.py)가 이미 최상위 import 를
# 쓰고 Dockerfile 이 WORKDIR /app + cwd 를 sys.path 앞에 두므로 루트가 그 규약과 맞는다.
#
# 즉 «원본은 app/proto/exercise.proto, 산출물은 ai-server 루트» 가 이 프로젝트의 규약이고,
# 이 스크립트가 그 규약을 실행 가능한 형태로 고정한다.
#
# 사용법:  cd ai-server && ./gen_proto.sh
set -euo pipefail

cd "$(dirname "$0")"

PY="${PYTHON:-.venv/Scripts/python.exe}"
[ -x "$PY" ] || PY="${PYTHON:-python}"

echo "재생성: app/proto/exercise.proto → ai-server/ (루트)"
"$PY" -m grpc_tools.protoc \
    --proto_path=app/proto \
    --python_out=. \
    --grpc_python_out=. \
    app/proto/exercise.proto

echo
echo "생성된 파일:"
ls -1 exercise_pb2.py exercise_pb2_grpc.py

echo
echo "⚠️  backend/src/main/proto/exercise.proto 도 같은 내용이어야 한다."
echo "    Spring 쪽은 gradle protobuf 플러그인이 빌드 때 알아서 생성한다."
if ! diff -q app/proto/exercise.proto ../backend/src/main/proto/exercise.proto >/dev/null; then
    echo "🔴 두 .proto 가 다르다 — 계약이 갈라졌다. 맞추고 다시 실행할 것."
    exit 1
fi
echo "✅ 두 .proto 동일"
