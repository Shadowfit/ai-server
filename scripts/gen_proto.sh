#!/usr/bin/env bash
# gRPC 생성 산출물을 만든다 (#132).
#
# 손으로 protoc 를 부르던 것을 코드로 박는다. 저장소 어디에도 protoc 호출이 없어서,
# 재생성 방법이 «아는 사람만 아는 것» 이었다.
#
# ── 산출물이 왜 ai-server/ 루트인가 ──────────────────────────────────────────
# 아래 protoc 호출이 `-I app/proto` 를 쓴다. 그러면 protoc 가 보는 파일 이름이
# `exercise.proto`(경로 없음)가 되고, 생성된 exercise_pb2_grpc.py 내부의 import 가
# **bare** 로 나온다:
#
#     import exercise_pb2 as exercise__pb2
#
# 이 이름은 sys.path 루트에서만 해석된다. 컨테이너는 WORKDIR=/app 에 ai-server 를
# 통째로 COPY 하므로 그 루트가 곧 ai-server/ 다. 즉 **산출물 위치는 취향이 아니라
# import 규약의 결과**다. app/proto/ 로 옮기려면 생성물 내부의 저 줄까지 후처리해야
# 하고, 재생성할 때마다 다시 깨진다.
#
# 🔴 예전에는 app/proto/ 에도 같은 산출물이 한 벌 더 있었다. 그런데 **그 사본은
#    로드되지 않았다** — 위 이유로 bare import 가 루트를 집었다. 재생성할 때 한쪽만
#    갱신하면 «빌드는 되는데 옛 계약이 도는» 상태가 된다. 943e2c2 에서 지웠다 (#132).
#
# ── 쓰는 법 ─────────────────────────────────────────────────────────────────
#   cd ai-server && ./scripts/gen_proto.sh
#
# grpcio-tools 가 필요하다. requirements.txt 에 있으므로 venv 를 쓴다(시스템 파이썬엔
# 없다). VENV_PY 로 인터프리터를 직접 넘길 수도 있다.
set -euo pipefail

cd "$(dirname "$0")/.."   # ai-server/

PROTO_DIR="app/proto"
PROTO_FILE="$PROTO_DIR/exercise.proto"
OUT_DIR="."

# venv 우선. 없으면 python 에 맡기고, grpc_tools 가 없으면 아래에서 걸린다.
if [ -n "${VENV_PY:-}" ]; then
    PY="$VENV_PY"
elif [ -x ".venv/Scripts/python.exe" ]; then   # Windows
    PY=".venv/Scripts/python.exe"
elif [ -x ".venv/bin/python" ]; then           # Linux/macOS
    PY=".venv/bin/python"
else
    PY="python"
fi

if ! "$PY" -c "import grpc_tools" 2>/dev/null; then
    echo "error: grpc_tools 가 없다 ($PY). requirements.txt 를 설치하거나 VENV_PY 로 인터프리터를 넘길 것." >&2
    exit 1
fi

echo "생성기: $("$PY" -c "import grpc_tools, google.protobuf as p; print('grpc_tools ok, protobuf', p.__version__)")"

"$PY" -m grpc_tools.protoc \
    -I "$PROTO_DIR" \
    --python_out="$OUT_DIR" \
    --grpc_python_out="$OUT_DIR" \
    "$PROTO_FILE"

echo "생성됨:"
ls -1 exercise_pb2.py exercise_pb2_grpc.py

# 생성물이 실제로 루트를 집는지 확인한다. 이 스크립트가 지키려는 것이 바로 그 규약이라,
# 「만들었다」가 아니라 「그 이름이 여기로 해석된다」를 봐야 한다.
"$PY" - <<'PY'
import importlib.util, sys
bad = False
for name in ("exercise_pb2", "exercise_pb2_grpc"):
    spec = importlib.util.find_spec(name)   # import 를 실행하지 않는다
    origin = spec.origin if spec else None
    print(f"  {name} -> {origin}")
    if origin is None:
        bad = True
if bad:
    sys.exit("error: 생성물이 sys.path 에서 안 잡힌다 — ai-server/ 에서 실행했는지 확인할 것")
PY

echo
# 🔴 두 .proto 가 갈리면 런타임 직렬화 오류가 난다. CI(.github/workflows/proto-sync-check.yml)가
#    PR 에서 막지만, 여기서 **먼저** 막는다 — 생성까지 끝내고 푸시한 뒤에 아는 것보다 낫다.
#
#    이 검사는 원래 ai-server/gen_proto.sh 에만 있었다. 진입점이 두 벌이고 안전장치가 서로
#    달라서(루트는 proto 드리프트를, 이쪽은 venv·import 규약을) «어느 쪽을 돌렸나» 가 보장을
#    갈랐다. 그 스크립트를 지우면서 이쪽으로 옮긴다 — 남기는 쪽이 둘 다 갖는다 (#319).
if ! diff -q app/proto/exercise.proto ../backend/src/main/proto/exercise.proto >/dev/null; then
    echo "🔴 두 .proto 가 다르다 — 계약이 갈라졌다. 맞추고 다시 실행할 것." >&2
    diff -u ../backend/src/main/proto/exercise.proto app/proto/exercise.proto >&2 || true
    exit 1
fi
echo "✅ backend/ai-server .proto 동일"
