import grpc
from concurrent import futures
import exercise_pb2
import exercise_pb2_grpc

class ExerciseServicer(exercise_pb2_grpc.ExerciseServiceServicer):
    def StartAnalysis(self, request, context):
        # 여기가 핵심! 스프링이 보낸 데이터를 출력해봅니다.
        print(f"==== [gRPC 요청 수신] ====")
        print(f"운동 ID: {request.exercise_id}")
        print(f"유튜브 ID: {request.youtube_id}")
        print(f"세션 ID: {request.session_id}")
        print(f"==========================")

        # 일단 성공했다고 응답만 보냄
        return exercise_pb2.AnalyzeResponse(success=True)

def serve():
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    exercise_pb2_grpc.add_ExerciseServiceServicer_to_server(ExerciseServicer(), server)
    server.add_insecure_port('[::]:8000') # 스프링에서 설정한 포트와 맞춰야 함
    print("Test gRPC Server 시작 (Port: 8000)...")
    server.start()
    server.wait_for_termination()

if __name__ == '__main__':
    serve()