import grpc
from concurrent import futures
import threading
import time # 분석 대기를 위해 추가
from fastapi import FastAPI
import uvicorn

import exercise_pb2
import exercise_pb2_grpc
from google.protobuf.timestamp_pb2 import Timestamp

app = FastAPI()

@app.get("/health")
def health_check():
    return {"status": "healthy", "service": "ShadowFit AI"}

# --- 추가된 부분: 스프링 서버로 결과를 보고하는 함수 ---
def report_analysis_result(session_id):
    """
    분석이 끝났음을 스프リング(8585포트 아님, 스프링 gRPC 서버 포트)으로 알림
    """
    time.sleep(2) # 실제 분석이 걸리는 시간이라고 가정 (Mock)

    # 스프링 서버의 gRPC 주소 (도커 환경이면 서비스 이름, 로컬이면 localhost)
    # 스프링의 gRPC 서버 포트번호를 확인하세요 (보통 9090 등 설정한 포트)
    channel = grpc.insecure_channel('backend:6565')
    stub = exercise_pb2_grpc.ExerciseServiceStub(channel)

    print(f"🚀 [AI -> Spring] 세션 {session_id} 분석 결과 전송 시작...")

    request = exercise_pb2.SessionCompleteRequest(
        session_id=session_id,
        total_reps=15,          # AI가 계산한 결과
        avg_sync_rate=88.5,     # AI가 계산한 결과
        calories_burned=120.0   # AI가 계산한 결과
    )

    try:
        response = stub.CompleteAnalysis(request)
        print(f"✅ [AI -> Spring] 보고 완료! 세션 상태: {response.status}")
    except grpc.RpcError as e:
        print(f"❌ [AI -> Spring] 보고 실패: {e.details()}")

# --------------------------------------------------

class ExerciseServicer(exercise_pb2_grpc.ExerciseServiceServicer):
    # 전역 혹은 클래스 변수로 현재 진행 중인 세션 관리 (선택 사항)
    active_sessions = set()

    def ExtractReferenceData(self, request, context):
        print(f"==== [유튜브 좌표 추출 요청 수신] ====")
        return exercise_pb2.ExtractResponse(success=True, exercise_id=request.exercise_id, extracted_poses=[])

    def StartAnalysis(self, request, context):
        print(f"==== [실행 단계 분석 시작] ====")
        print(f"세션 ID: {request.session_id} 분석 시작...")

        self.active_sessions.add(request.session_id)

        # 분석 스레드 시작
        threading.Thread(target=report_analysis_result, args=(request.session_id, self), daemon=True).start()

        now = Timestamp()
        now.GetCurrentTime()
        return exercise_pb2.AnalyzeResponse(
            success=True, session_id=request.session_id, exercise_id=request.exercise_id,
            start_time=now, status=exercise_pb2.SessionStatus.IN_PROGRESS
        )

    # ✅ [추가] 사용자가 중단했을 때 호출되는 메서드
    def StopAnalysis(self, request, context):
        session_id = request.session_id
        print(f"🛑 [Stop] 사용자에 의한 분석 중단 요청 수신 - 세션 ID: {session_id}")

        if session_id in self.active_sessions:
            self.active_sessions.remove(session_id)
            return exercise_pb2.StopResponse(success=True, message="분석이 성공적으로 중단되었습니다.", session_id=session_id)
        else:
            return exercise_pb2.StopResponse(success=False, message="진행 중인 세션을 찾을 수 없습니다.", session_id=session_id)

# --- 보고 함수 수정 (중단 여부 체크 로직) ---
def report_analysis_result(session_id, servicer_instance):
    time.sleep(2) # Mock 분석 시간

    # 🛑 중단된 세션인지 체크
    if session_id not in servicer_instance.active_sessions:
        print(f"⚠️ 세션 {session_id}는 이미 중단되어 보고를 취소합니다.")
        return

    channel = grpc.insecure_channel('backend:6565')
    stub = exercise_pb2_grpc.ExerciseServiceStub(channel)

    print(f"🚀 [AI -> Spring] 세션 {session_id} 분석 완료 보고 중...")

    request = exercise_pb2.SessionCompleteRequest(
        session_id=session_id,
        total_reps=15,
        avg_sync_rate=88.5,
        calories_burned=120.0
    )

    try:
        response = stub.CompleteAnalysis(request)
        print(f"✅ [AI -> Spring] 보고 완료! 세션 상태: {response.status}")
        # 보고 완료 후 세션 제거
        if session_id in servicer_instance.active_sessions:
            servicer_instance.active_sessions.remove(session_id)
    except grpc.RpcError as e:
        print(f"❌ [AI -> Spring] 보고 실패: {e.details()}")

def run_grpc_server():
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    exercise_pb2_grpc.add_ExerciseServiceServicer_to_server(ExerciseServicer(), server)
    server.add_insecure_port('[::]:8585')
    print("✅ ShadowFit AI gRPC Server 시작 (Port: 8585)...")
    server.start()
    server.wait_for_termination()

if __name__ == "__main__":
    grpc_thread = threading.Thread(target=run_grpc_server, daemon=True)
    grpc_thread.start()
    print("✅ ShadowFit AI FastAPI 시작 (Port: 8000)...")
    uvicorn.run(app, host="0.0.0.0", port=8000)