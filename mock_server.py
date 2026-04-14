import grpc
from concurrent import futures
import exercise_pb2
import exercise_pb2_grpc
from google.protobuf.timestamp_pb2 import Timestamp

class ExerciseServicer(exercise_pb2_grpc.ExerciseServiceServicer):
    def StartAnalysis(self, request, context):
        print(f"==== [분석 시작 요청 수신] ====")
        print(f"세션 ID: {request.session_id}")

        # 현재 시간을 gRPC Timestamp 형식으로 변환
        now = Timestamp()
        now.GetCurrentTime()

        return exercise_pb2.AnalyzeResponse(
            success=True,
            session_id=request.session_id,
            exercise_id=request.exercise_id,
            start_time=now,
            status=exercise_pb2.SessionStatus.IN_PROGRESS # Enum 사용
        )
    # 2. [추가] 세션 종료 요청 (테스트용으로 미리 만들어두기)
    def CompleteAnalysis(self, request, context):
        print(f"==== [세션 종료 및 결과 전송] ====")
        print(f"세션 {request.session_id} 분석 완료! 총 {request.total_reps}회 수행.")
        return exercise_pb2.SessionCompleteResponse(
            session_id=request.session_id,
            status=exercise_pb2.SessionStatus.COMPLETED
        )

def serve():
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    exercise_pb2_grpc.add_ExerciseServiceServicer_to_server(ExerciseServicer(), server)
    server.add_insecure_port('[::]:8000') # 스프링에서 설정한 포트와 맞춰야 함
    print("Test gRPC Server 시작 (Port: 8000)...")
    server.start()
    server.wait_for_termination()

if __name__ == '__main__':
    serve()