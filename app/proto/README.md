# app/proto — 계약 원본만 둔다

이 디렉터리에는 **`.proto` 원본만** 있다. 생성 산출물(`exercise_pb2.py`,
`exercise_pb2_grpc.py`)은 **`ai-server/` 루트**에 있다.

## 왜 갈라져 있나

생성기가 `-I app/proto` 로 불린다. 그래서 생성된 `exercise_pb2_grpc.py` 안의 import 가
**bare** 다:

```python
import exercise_pb2 as exercise__pb2
```

이 이름은 `sys.path` 루트에서만 해석된다. 컨테이너는 `WORKDIR=/app` 에 `ai-server` 를
통째로 넣으므로 그 루트가 산출물의 자리다. **위치가 취향이 아니라 import 규약의 결과**다.

여기에 사본을 두면 «있는데 안 쓰이는» 파일이 된다. 실제로 그런 상태였고(#132),
재생성할 때 한쪽만 갱신하면 *빌드는 성공하는데 실행 코드는 옛 계약을 쓰는* 함정이 생긴다.
`943e2c2` 에서 그 사본을 지웠다.

## 재생성

```bash
cd ai-server && ./scripts/gen_proto.sh
```

손으로 `protoc` 를 부르지 말 것 — 옵션(`-I`)이 위 규약을 정하므로, 다르게 부르면
산출물이 다른 곳을 가리킨다.

## backend 와의 동기화

`backend/src/main/proto/exercise.proto` 와 **내용이 같아야 한다.** 갈리면 런타임
직렬화 오류가 난다. `.github/workflows/proto-sync-check.yml` 이 PR 에서 두 원본을
비교해 막는다.

> ⚠️ 그 워크플로는 **`.proto` 원본만** 본다. 생성 산출물이 원본과 어긋난 경우
> (`.proto` 는 고쳤는데 재생성을 안 한 경우)는 **잡지 못한다.**
