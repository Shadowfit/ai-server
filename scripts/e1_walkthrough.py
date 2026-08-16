"""E1 통주행 — 「시연이 처음부터 끝까지 한 번에 도는가」를 한 명령으로 확인한다 (이슈 #196).

왜 스크립트인가
---------------------------------------------------------------------------
#196 의 통주행은 손으로 돌린 것이고, 이슈 스스로 «E1 이 이걸로 통과한다 를 회귀로 고정할
자리가 필요하다» 고 적었다. 그 자리가 여기다. 프론트(RN)가 하는 호출을 그대로 흉내 낸다 —
앱→Spring(HTTP)과 앱→AI(POST /api/v1/pose, 분기 H2 직결) 두 갈래이고, AI→Spring gRPC 콜백은
건드리지 않는다(저절로 일어나야 하는 구간이라 그것이 관측 대상이다).

무엇을 판정하는가
---------------------------------------------------------------------------
#196 이 «배관은 통과했는데 내용이 비었다» 였다. HTTP 는 전 구간 200 이었는데 rep 이 0회라
rep → 콜백 → pose_data 적재 → 리포트 선계산이 한 번도 안 돌았다. 그래서 이 스크립트는
**200 을 세지 않고 rep 과 리포트 값을 센다.** totalReps 가 0이면 실패로 끝낸다.

🔴 가시성 미달 프레임도 200 + success=true 로 온다(pose.py:127-132). 「검출 30/31」로 세면
정상으로 보이지만 판정에 들어간 프레임은 0일 수 있다 — #196 이 지적한 함정이라 여기서는
**AI 응답의 rep_number 증가**를 따로 센다.

입력 영상
---------------------------------------------------------------------------
하체가 화면에 들어오고 실제로 스쿼트를 하는 영상이어야 한다. 저장소의
`demo_videos/demo_squat.mp4` 는 **못 쓴다**(얼굴만 잡힌 실패 테이크, 가시성 0.24 — #196 원인 ①).
영상은 저장소에 두지 않는다(라이선스). 경로를 인자로 준다.

  cd ai-server
  PYTHONPATH=. .venv/Scripts/python.exe scripts/e1_walkthrough.py \
      --video ~/Downloads/<squat>.mp4 \
      --ai-token "$(docker exec shadowfit-ai printenv AI_PUBLIC_TOKEN)"

선행: docker compose 3서비스가 떠 있고, exercise_references 에 정답지가 있어야 한다
(V4 시드. 0행이면 rep 이 살아도 sync_rate 가 0 으로 떨어진다 — #192).
"""

from __future__ import annotations

import argparse
import base64
import sys
import time

import cv2
import httpx


def log(step: str, detail: str = "") -> None:
    print(f"[E1] {step:<34} {detail}")


def main() -> int:
    p = argparse.ArgumentParser(description="E1 통주행 드라이버 (#196)")
    p.add_argument("--video", required=True, help="스쿼트 영상 경로 (하체가 보여야 한다)")
    p.add_argument("--spring", default="http://localhost:8080")
    p.add_argument("--ai", default="http://localhost:8000")
    p.add_argument("--ai-token", required=True, help="AI_PUBLIC_TOKEN")
    p.add_argument("--exercise-id", type=int, default=1)
    p.add_argument("--fps", type=float, default=3.0, help="전송 프레임률 (앱과 같은 3fps)")
    args = p.parse_args()

    # 매 판 새 계정을 쓴다 — 같은 계정을 재사용하면 «이미 진행 중인 세션»(W005)에 걸려
    # 두 번째 판부터 다른 것을 재게 된다.
    stamp = int(time.time())
    email = f"e1-{stamp}@test.local"
    username = f"e1runner{stamp}"
    password = "E1passw0rd!"

    http = httpx.Client(timeout=30.0)

    # ── ① 가입 · 로그인 ──────────────────────────────────────────────────────
    r = http.post(f"{args.spring}/member/signup",
                  json={"username": username, "email": email,
                        "password": password, "sex": "MALE"})
    if r.status_code != 200:
        log("signup 실패", f"{r.status_code} {r.text[:200]}")
        return 1
    log("signup", "200")

    r = http.post(f"{args.spring}/member/login", json={"email": email, "password": password})
    if r.status_code != 200:
        log("login 실패", f"{r.status_code} {r.text[:200]}")
        return 1
    token = r.json()["accessToken"]
    auth = {"Authorization": f"Bearer {token}"}
    log("login", "200")

    # ── ②-0 온보딩 ─────────────────────────────────────────────────────────
    # 빼먹으면 세션 시작이 400 이다. ExerciseAnalysisService.startAnalysis 가 preferredUrl 을
    # 먼저 검사하기 때문이다(VideoRequestDto 주석 — 이슈 #178 이 그 순서를 적어뒀다).
    # 즉 «온보딩을 마친 사용자» 가 E1 의 전제다.
    r = http.patch(f"{args.spring}/member/onboarding/{email}",
                   json={"selectedPersona": "BEGINNER", "workoutLevel": "BEGINNER",
                         "height": 175.0, "weight": 70.0,
                         "preferredUrl": "https://example.com/squat"},
                   headers=auth)
    if r.status_code != 200:
        log("온보딩 실패", f"{r.status_code} {r.text[:200]}")
        return 1
    log("온보딩", "200")

    # ── ② 세션 시작 (여기서 Spring → AI gRPC StartAnalysis 가 일어난다) ──────
    r = http.post(f"{args.spring}/exercises/sessions",
                  json={"exerciseId": args.exercise_id}, headers=auth)
    if r.status_code not in (200, 202):
        log("세션 시작 실패", f"{r.status_code} {r.text[:200]}")
        return 1
    session_id = r.json()["sessionId"]
    log("세션 시작", f"{r.status_code} sessionId={session_id}")

    # ── ②-1 AI 세션 배정 대기 ───────────────────────────────────────────────
    # 세션 시작은 202 다 — Spring 이 받았다는 뜻이지 AI 가 배정을 끝냈다는 뜻이 아니다.
    # 배정 전에 프레임을 보내면 AI 는 200 + success=false 로 답한다("배정된 분석기가 없습니다").
    # 🔴 status 만 보면 정상으로 보인다. #196 이 지적한 함정이 정확히 이것이고, 이 드라이버도
    #    처음엔 여기 걸려 28프레임을 헛보냈다.
    probe = {"image": "", "exercise_type": "squat", "session_id": session_id}
    ai_headers = {"Authorization": f"Bearer {args.ai_token}"}

    # ── ③ 프레임 유입 ───────────────────────────────────────────────────────
    cap = cv2.VideoCapture(args.video)
    if not cap.isOpened():
        log("영상 열기 실패", args.video)
        return 1

    src_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    step = max(1, int(round(src_fps / args.fps)))
    log("영상", f"{args.video} ({src_fps:.1f}fps → {step}프레임마다 전송)")

    sent = detected = judged = 0
    max_rep = 0
    idx = 0
    skipped_msgs: dict[str, int] = {}
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if idx % step:
            idx += 1
            continue
        idx += 1

        ok, buf = cv2.imencode(".jpg", frame)
        if not ok:
            continue
        payload = {
            "image": base64.b64encode(buf.tobytes()).decode(),
            "exercise_type": "squat",
            "session_id": session_id,
        }
        # 첫 프레임은 AI 배정이 끝날 때까지 재시도한다(위 ②-1 참고).
        attempts = 12 if sent == 0 else 1
        for attempt in range(attempts):
            rr = http.post(f"{args.ai}/api/v1/pose", json=payload, headers=ai_headers)
            if rr.status_code != 200:
                log("프레임 전송 실패", f"{rr.status_code} {rr.text[:160]}")
                return 1
            body = rr.json()
            if body.get("success"):
                break
            if attempt == 0:
                log("AI 배정 대기", str(body.get("message"))[:90])
            time.sleep(1.0)
        sent += 1

        # 🔴 200 은 아무것도 보장하지 않는다. success=false 면 그 프레임은 판정에 안 들어갔다.
        if not body.get("success"):
            skipped_msgs[str(body.get("message"))[:60]] =                 skipped_msgs.get(str(body.get("message"))[:60], 0) + 1
            continue
        if body.get("landmarks"):
            detected += 1
        if body.get("angles"):
            judged += 1
        rep = body.get("rep_count") or 0
        if rep > max_rep:
            max_rep = rep
            log("rep 완성", f"rep_count={rep} (프레임 {sent}번째)")
    cap.release()

    # 「검출 N/N」이 아니라 «판정에 들어간 프레임» 을 따로 센다 — 가시성 미달·속도 상한 프레임은
    # landmarks 는 들어 있고 angles 가 없다. 둘을 같은 숫자로 세면 #196 처럼 오독한다.
    log("프레임 유입", f"전송 {sent} · 랜드마크 {detected} · 판정에 들어감 {judged} · rep {max_rep}회")
    for msg, n in skipped_msgs.items():
        log("  스킵", f"{n}회 — {msg}")

    # ── ④ 세션 종료 ─────────────────────────────────────────────────────────
    r = http.patch(f"{args.spring}/sessions/{session_id}/end", headers=auth)
    log("세션 종료", f"{r.status_code}")

    # ── ⑤ 리포트 ────────────────────────────────────────────────────────────
    # 종료 PATCH 는 «클라가 끝냈다» 까지다. 리포트는 그 뒤 아웃박스 → AI → CompleteAnalysis
    # 콜백 → precomputeReport 로 만들어지므로 시차가 있다. 즉시 조회하면 404 다.
    # 그 사슬이 도는지가 E1 의 뒷구간이므로 기다려서 확인한다.
    report = None
    for attempt in range(20):
        r = http.get(f"{args.spring}/reports/session/{session_id}", headers=auth)
        if r.status_code == 200:
            report = r.json()
            log("리포트", f"200 (종료 후 {attempt + 1}초)")
            break
        time.sleep(1.0)
    if report is None:
        log("리포트 미생성", "20초 기다려도 404 — 종료 통보→AI→CompleteAnalysis 사슬이 안 돌았다")
        return 1
    for key in ("totalReps", "avgSyncRate", "workoutMinutes", "caloriesBurned"):
        print(f"        {key:<18} {report.get(key)}")
    print(f"        worstSection       {report.get('worstSection')}")
    print(f"        repTrend           {len(report.get('repTrend') or [])}건")

    # ── 판정 ────────────────────────────────────────────────────────────────
    # 200 을 세지 않는다. #196 은 «전 구간 200 인데 전부 0» 이었다.
    problems = []
    if max_rep == 0:
        problems.append("AI 가 rep 을 한 번도 완성하지 못했다 (영상이 못 쓰는 것이거나 #217)")
    if not report.get("totalReps"):
        problems.append("리포트 totalReps 가 0 이다 (rep→콜백→적재 구간이 안 돌았다)")
    if not report.get("avgSyncRate"):
        problems.append("avgSyncRate 가 0 이다 (정답지가 비었을 수 있다 — #192)")

    print()
    if problems:
        log("판정", "🔴 E1 미통과")
        for x in problems:
            print(f"        - {x}")
        return 1
    log("판정", "✅ E1 통과 — 배관과 내용이 함께 돌았다")
    return 0


if __name__ == "__main__":
    sys.exit(main())
