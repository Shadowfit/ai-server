import grpc
from concurrent import futures
import threading
from fastapi import FastAPI
import uvicorn

# 기존에 생성된 gRPC 파일들 import
import exercise_pb2
import exercise_pb2_grpc
from google.protobuf.timestamp_pb2 import Timestamp

# 1. FastAPI 앱 설정 (8000번 포트용)
app = FastAPI()

@app.get("/health")
def health_check():
    return {"status": "healthy", "service": "ShadowFit AI"}

# 2. gRPC 서비스 로직 (기존 내용 그대로)
class ExerciseServicer(exercise_pb2_grpc.ExerciseServiceServicer):
    def ExtractReferenceData(self, request, context):
        print(f"==== [유튜브 좌표 추출 요청 수신] ====")
        mock_poses = [
            exercise_pb2.PoseDataRequest(
                timestamp_sec=0.1,
                joint_coordinates='{"nose": [0.5, 0.5]}'
            )
        ]
        return exercise_pb2.ExtractResponse(success=True, exercise_id=request.exercise_id, extracted_poses=mock_poses)

    def StartAnalysis(self, request, context):
        print(f"==== [실행 단계 분석 시작] ====")
        print(f"세션 ID: {request.session_id}, 수신된 기준 좌표: {len(request.reference_poses)}개")
        now = Timestamp()
        now.GetCurrentTime()
        return exercise_pb2.AnalyzeResponse(
            success=True, session_id=request.session_id, exercise_id=request.exercise_id,
            start_time=now, status=exercise_pb2.SessionStatus.IN_PROGRESS
        )

    def CompleteAnalysis(self, request, context):
        print(f"==== [분석 완료 및 통계 전송] ====")
        return exercise_pb2.SessionCompleteResponse(
            session_id=request.session_id, status=exercise_pb2.SessionStatus.COMPLETED,
            total_reps=request.total_reps, avg_sync_rate=85.5, calories_burned=120.0
        )

# 3. gRPC 서버 실행 함수 (8585번 포트용)
def run_grpc_server():
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    exercise_pb2_grpc.add_ExerciseServiceServicer_to_server(ExerciseServicer(), server)
    server.add_insecure_port('[::]:8585')
    print("✅ ShadowFit AI gRPC Server 시작 (Port: 8585)...")
    server.start()
    server.wait_for_termination()

# 4. 메인 실행부 (8000번과 8585번 동시 실행)
if __name__ == "__main__":
    # gRPC를 백그라운드 스레드에서 실행
    grpc_thread = threading.Thread(target=run_grpc_server, daemon=True)
    grpc_thread.start()

    # FastAPI를 메인 스레드에서 실행
    print("✅ ShadowFit AI FastAPI 시작 (Port: 8000)...")
    uvicorn.run(app, host="0.0.0.0", port=8000)