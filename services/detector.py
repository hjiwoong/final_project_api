"""사람 수 카운트 (YOLO). mock ↔ real 폴백

- mock : 사진 내용을 보지 않고 지정한 수만큼 '검출'한 것처럼 반환 (개발·시연용)
- real : YOLO(best.pt) 로 사람 검출.
         실패하면 mode="mock-fallback" 으로 반환 → 라우터에서 '정상' 판정하지 않도록 막는다.
"""
import io
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # import 순서와 상관없이 .env 를 먼저 읽는다

# services/detector.py 기준 → 프로젝트 최상단 폴더
BASE_DIR = Path(__file__).resolve().parent.parent

MODE = os.getenv("DETECTOR_MODE", "mock").lower()
_model_env = os.getenv("YOLO_MODEL", "yolov8n.pt")
# 상대경로면 프로젝트 최상단 기준으로 (uvicorn 을 어디서 실행해도 같은 파일을 찾도록)
YOLO_MODEL = _model_env if Path(_model_env).is_absolute() else str(BASE_DIR / _model_env)

CONF_THRESHOLD = float(os.getenv("CONF_THRESHOLD", "0.25"))          # 이 이상만 사람으로 셈
LOW_CONF_THRESHOLD = float(os.getenv("LOW_CONF_THRESHOLD", "0.5"))   # 이 미만이면 '신뢰도 낮음'
IOU_THRESHOLD = float(os.getenv("IOU_THRESHOLD", "0.5"))             # 붙어 앉은 사람이 하나로 합쳐지면 올림
IMG_SIZE = int(os.getenv("IMG_SIZE", "640"))                         # 사람이 작게 찍히면 960~1280

log = logging.getLogger("detector")
_model = None
_person_classes: list[int] | None = None   # None 이면 모든 클래스를 사람으로 셈


def _mock_detect(count: int, low_conf: int = 0) -> list[dict]:
    dets = []
    for i in range(count):
        is_low = i >= count - low_conf
        conf = 0.40 if is_low else round(0.95 - 0.02 * i, 2)
        dets.append({"label": "person", "conf": conf, "bbox": [10 + i * 50, 20, 60 + i * 50, 120]})
    return dets


def load_model():
    """모델을 메모리에 올린다. 서버 시작 시 한 번 부르면 첫 요청이 느려지지 않는다."""
    global _model, _person_classes
    if _model is not None:
        return _model

    from ultralytics import YOLO

    _model = YOLO(YOLO_MODEL)
    names = _model.names  # 예: {0: 'person'} 또는 COCO 80개 클래스

    if len(names) == 1:
        _person_classes = None                       # 클래스가 하나뿐 → 전부 사람
    else:
        ids = [i for i, n in names.items() if str(n).lower() == "person"]
        _person_classes = ids or [0]

    log.info("YOLO 로딩 완료: %s / 클래스 %s / 사람 클래스 %s",
             YOLO_MODEL, names, _person_classes or "전체")
    return _model


def open_image(image_bytes: bytes):
    """휴대폰 사진은 실제 픽셀은 옆으로 누워 있고 '회전 정보(EXIF)'만 붙어 있는 경우가 많다.
    PIL 은 이 정보를 자동 적용하지 않아서, 그대로 넣으면 모델이 90도 돌아간 사진을 보게 된다.
    (Roboflow 는 내보낼 때 Auto-Orient 로 이미 회전을 적용하므로 학습·평가 사진은 똑바로 서 있음)"""
    from PIL import Image, ImageOps

    img = Image.open(io.BytesIO(image_bytes))
    return ImageOps.exif_transpose(img).convert("RGB")


def _real_detect(image_bytes: bytes) -> list[dict]:
    model = load_model()
    img = open_image(image_bytes)
    result = model.predict(
        img,
        classes=_person_classes,
        conf=CONF_THRESHOLD,
        iou=IOU_THRESHOLD,
        imgsz=IMG_SIZE,
        verbose=False,
    )[0]

    dets = []
    for box in result.boxes:
        dets.append({
            "label": "person",
            "conf": round(float(box.conf[0]), 3),
            "bbox": [round(float(v), 1) for v in box.xyxy[0].tolist()],
        })
    return dets


def detect_people(
    image_bytes: bytes,
    mock_count: int | None = None,
    mock_low_conf: int = 0,
    default_count: int = 0,
) -> tuple[list[dict], str]:
    """-> (detections, 실제 사용된 모드: 'real' | 'mock' | 'mock-fallback')"""
    if MODE == "real":
        try:
            return _real_detect(image_bytes), "real"
        except Exception as e:   # noqa: BLE001
            log.exception("real 검출 실패 → mock-fallback: %s", e)
            # 주의: 이 결과는 실제 사진을 본 게 아니다. 라우터에서 반드시 막아야 한다.
            return _mock_detect(default_count), "mock-fallback"

    count = mock_count if mock_count is not None else default_count
    return _mock_detect(count, mock_low_conf), "mock"


def low_conf_count(detections: list[dict]) -> int:
    return sum(1 for d in detections if d["conf"] < LOW_CONF_THRESHOLD)


def status() -> dict:
    """/health 등에서 상태 확인용"""
    return {
        "mode": MODE,
        "model_path": YOLO_MODEL,
        "model_exists": Path(YOLO_MODEL).exists(),
        "model_loaded": _model is not None,
        "conf": CONF_THRESHOLD,
        "low_conf": LOW_CONF_THRESHOLD,
        "iou": IOU_THRESHOLD,
        "imgsz": IMG_SIZE,
    }
