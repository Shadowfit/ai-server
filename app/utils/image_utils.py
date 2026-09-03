"""이미지 변환 유틸리티."""

import base64

import cv2
import numpy as np
import pybase64


def base64_to_image(b64_string: str) -> np.ndarray:
    """Base64 문자열을 OpenCV 이미지(BGR)로 변환.

    디코드에 pybase64(SIMD)를 쓴다 — stdlib 대비 2.47배 빠르고(c7i.4xlarge, 1MB
    페이로드 실측, docs/decisions/pose-frame-base64-cost.md §10) 정확도 대가가 없는
    드롭인 교체다. 다만 이 구간의 실제 비용 대부분은 JPEG 압축 해제(cv2.imdecode)라
    체감 절감은 작다(프레임당 ~0.5ms) — 같은 문서가 이미 밝힌 병목 순서는 안 바뀐다.
    """
    if "," in b64_string:
        b64_string = b64_string.split(",", 1)[1]
    img_bytes = pybase64.b64decode(b64_string)
    img_array = np.frombuffer(img_bytes, dtype=np.uint8)
    return cv2.imdecode(img_array, cv2.IMREAD_COLOR)


def image_to_base64(image: np.ndarray) -> str:
    """OpenCV 이미지를 Base64 문자열로 변환."""
    _, buffer = cv2.imencode(".jpg", image)
    return base64.b64encode(buffer).decode("utf-8")
