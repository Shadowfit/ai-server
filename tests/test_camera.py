import cv2
import mediapipe as mp
import numpy as np

# 이게 네가 원하던 그 solutions!
mp_pose = mp.solutions.pose
mp_drawing = mp.solutions.drawing_utils

# 포즈 추정 객체 생성
pose = mp_pose.Pose(
    static_image_mode=False,
    model_complexity=1,
    min_detection_confidence=0.5,
    min_tracking_confidence=0.5
)

def calculate_angle(a,b,c) :
    a = np.array(a)
    b = np.array(b)
    c = np.array(c)

    radians = np.arctan2(c[1]-b[1], c[0]-b[0]) - np.arctan2(a[1]-b[1], a[0]-b[0])
    angle = np.abs(radians*180.0/np.pi)
    
    if angle > 180.0:
        angle = 360-angle
    return angle

cap = cv2.VideoCapture(0)
print("HOPT - solutions 버전 가동! (종료: ESC)")

while cap.isOpened():
    success, image = cap.read()
    if not success: break

    # 처리 속도를 위해 RGB 변환
    image_rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    results = pose.process(image_rgb)
    if results.pose_landmarks:
        h, w, _ = image.shape # 현재 화면의 실시간 높이와 너비를 가져옴
        landmarks = results.pose_landmarks.landmark

        # 1. 오른쪽 다리 좌표 추출
        hip = [landmarks[mp_pose.PoseLandmark.RIGHT_HIP.value].x, landmarks[mp_pose.PoseLandmark.RIGHT_HIP.value].y]
        knee = [landmarks[mp_pose.PoseLandmark.RIGHT_KNEE.value].x, landmarks[mp_pose.PoseLandmark.RIGHT_KNEE.value].y]
        ankle = [landmarks[mp_pose.PoseLandmark.RIGHT_ANKLE.value].x, landmarks[mp_pose.PoseLandmark.RIGHT_ANKLE.value].y]

        # 2. 각도 계산
        knee_angle = calculate_angle(hip, knee, ankle)

        # 3. [핵심] 무릎의 실제 화면상 좌표 계산 (0.x 값에 실제 픽셀 곱하기)
        knee_text_pos = (int(knee[0] * w), int(knee[1] * h))

        # 4. 무릎 옆에 각도 표시 (화면 크기에 맞춰 글자 크기도 조절 가능)
        cv2.putText(image, f"{int(knee_angle)} deg", 
                    knee_text_pos, 
                    cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 255, 255), 2, cv2.LINE_AA)

        # 4. (추가 꿀팁) 엉덩이가 충분히 내려갔는지 체크하는 로직
        if knee_angle < 100: # 100도 이하로 내려가면
            cv2.putText(image, "GOOD DEPTH!", (50, 100), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 3)
        # 관절과 선(Skeleton) 그리기 - 이게 solutions의 사기적인 편의성이지!
        if results.pose_landmarks:
            mp_drawing.draw_landmarks(
                image, 
                results.pose_landmarks, 
                mp_pose.POSE_CONNECTIONS,
                mp_drawing.DrawingSpec(color=(0, 255, 0), thickness=2, circle_radius=2), # 점 설정
                mp_drawing.DrawingSpec(color=(0, 0, 255), thickness=2, circle_radius=2)  # 선 설정
            )
        # while문 안쪽, cv2.imshow 하기 직전에 추가
    # 화면 중앙에 가이드 박스 그리기 (전체화면 대응)
        cv2.rectangle(image, (int(w*0.2), int(h*0.2)), (int(w*0.8), int(h*0.9)), (255, 255, 255), 1)
        cv2.putText(image, "ALIGN YOUR BODY HERE", (int(w*0.3), int(h*0.15)), 
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 1)
        cv2.imshow('HOPT - Legacy Solutions', image)
        if cv2.waitKey(5) & 0xFF == 27: break

cap.release()
cv2.destroyAllWindows()