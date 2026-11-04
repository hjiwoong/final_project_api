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



def _normalize_model_path(raw_path: str) -> str:
    p = Path(raw_path.strip())
    return str(p if p.is_absolute() else (BASE_DIR / p).resolve())


def _build_model_candidates() -> list[str]:
    configured: list[str] = []
    for value in [os.getenv("YOLO_MODELS", ""), os.getenv("YOLO_MODEL", "")]:
        for item in value.split(","):
            item = item.strip()
            if item:
                configured.append(item)

    if not configured:
        weights_dir = BASE_DIR / "weights"
        if weights_dir.exists():
            configured = [str(p) for p in sorted(weights_dir.glob("*.pt"))]
        else:
            configured = ["weights/yolo11s.pt"]

    seen: set[str] = set()
    ordered: list[str] = []
    for item in configured:
        normalized = _normalize_model_path(item)
        if normalized not in seen:
            seen.add(normalized)
            ordered.append(normalized)
    return ordered


MODEL_CANDIDATES = _build_model_candidates()
YOLO_MODEL = MODEL_CANDIDATES[0] if MODEL_CANDIDATES else str((BASE_DIR / "weights" / "yolo11s.pt").resolve())

# 여러 PT 모델을 후보로 두고, 첫 로드 성공 모델을 사용한다.
# .env 에 YOLO_MODELS=path1,path2 또는 weights/*.pt 전부 자동 탐색
YOLO_MODELS = MODEL_CANDIDATES

# 업로드된 사진을 15초 내에 처리해야 브라우저/요청 클라이언트의 receive timeout 을 피할 수 있다.
# 실추론이 너무 느리면 이 시간을 초과했을 때 명시적으로 504 응답을 돌려준다.
DETECTOR_TIMEOUT_SEC = float(os.getenv("DETECTOR_TIMEOUT_SEC", "12"))


CONF_THRESHOLD = float(os.getenv("CONF_THRESHOLD", "0.25"))          # 이 이상만 사람으로 셈
LOW_CONF_THRESHOLD = float(os.getenv("LOW_CONF_THRESHOLD", "0.5"))   # 이 미만이면 '신뢰도 낮음'
IOU_THRESHOLD = float(os.getenv("IOU_THRESHOLD", "0.5"))             # 붙어 앉은 사람이 하나로 합쳐지면 올림
IMG_SIZE = int(os.getenv("IMG_SIZE", "640"))                         # 사람이 작게 찍히면 960~1280

log = logging.getLogger("detector")
_model = None

_model_path = None

_person_classes: list[int] | None = None   # None 이면 모든 클래스를 사람으로 셈


def _try_load_yolo(model_path: str):
    from ultralytics import YOLO

    model = YOLO(model_path)
    names = model.names
    if len(names) == 1:
        person_ids = None
    else:
        person_ids = [i for i, n in names.items() if str(n).lower() == "person"]
        person_ids = person_ids or [0]
    return model, person_ids


def load_model():
    """모델을 메모리에 올린다. 서버 시작 시 한 번 부르면 첫 요청이 느려지지 않는다.
    여러 PT 모델 후보를 순서대로 시도하여 가장 먼저 사용 가능한 모델을 로드한다."""
    global _model, _model_path, _person_classes
    if _model is not None:
        return _model

    last_error = None
    for model_path in YOLO_MODELS:
        try:
            _model, _person_classes = _try_load_yolo(model_path)
            _model_path = model_path
            log.info("YOLO 로딩 완료: %s / 클래스 %s / 사람 클래스 %s",
                     model_path, _model.names, _person_classes or "전체")
            return _model
        except Exception as e:  # noqa: BLE001
            last_error = e
            log.warning("YOLO 모델 로드 실패: %s / %s", model_path, e)

    raise RuntimeError(f"사용 가능한 YOLO 모델이 없습니다. 마지막 오류: {last_error}")


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

        except Exception as e:  # noqa: BLE001
            log.exception("real 검출 실패 → mock-fallback: %s", e)
            return _mock_detect(default_count), "mock-fallback"

    count = mock_count if mock_count is not None else default_count
    return _mock_detect(count, mock_low_conf), "mock"


def _mock_detect(count: int, low_conf: int = 0) -> list[dict]:
    dets = []
    for i in range(count):
        is_low = i >= count - low_conf
        conf = 0.40 if is_low else round(0.95 - 0.02 * i, 2)
        dets.append({"label": "person", "conf": conf, "bbox": [10 + i * 50, 20, 60 + i * 50, 120]})
    return dets


def low_conf_count(detections: list[dict]) -> int:
    return sum(1 for d in detections if d["conf"] < LOW_CONF_THRESHOLD)


def status() -> dict:
    """/health 등에서 상태 확인용"""
    return {
        "mode": MODE,

        "model_path": _model_path or YOLO_MODEL,
        "model_candidates": YOLO_MODELS,
        "model_exists": any(Path(p).exists() for p in YOLO_MODELS),
        "model_loaded": _model is not None,
        "conf": CONF_THRESHOLD,
        "low_conf": LOW_CONF_THRESHOLD,
        "iou": IOU_THRESHOLD,
        "imgsz": IMG_SIZE,
    }
