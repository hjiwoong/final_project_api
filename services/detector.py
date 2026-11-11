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

MODE = os.getenv("DETECTOR_MODE", "real").lower()


def _normalize_model_path(raw_path: str) -> str:
    p = Path(raw_path.strip())
    return str(p if p.is_absolute() else (BASE_DIR / p).resolve())


def _build_model_candidates() -> list[str]:
    model_list = os.getenv("YOLO_MODELS", "").strip()
    single_model = os.getenv("YOLO_MODEL", "").strip()
    if model_list:
        configured = [item.strip() for item in model_list.split(",") if item.strip()]
    elif single_model:
        configured = [single_model]
    else:
        weights_dir = BASE_DIR / "weights"
        if weights_dir.exists():
            preferred = weights_dir / "bus_person_k2_more_26s_1105_0855.pt"
            configured = [
                str(path)
                for path in (
                    preferred,
                    weights_dir / "bus_person_1029_0718.pt",
                    weights_dir / "yolo11s.pt",
                )
                if path.is_file()
            ]
        else:
            configured = ["weights/bus_person_k2_more_26s_1105_0855.pt"]

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

# 모든 설정 모델을 로드해 앙상블 추론에 사용한다.
# .env 의 YOLO_MODELS 에 모델 경로를 쉼표로 구분해 설정한다.
YOLO_MODELS = MODEL_CANDIDATES

# 업로드된 사진을 15초 내에 처리해야 브라우저/요청 클라이언트의 receive timeout 을 피할 수 있다.
# 실추론이 너무 느리면 이 시간을 초과했을 때 명시적으로 504 응답을 돌려준다.
DETECTOR_TIMEOUT_SEC = float(os.getenv("DETECTOR_TIMEOUT_SEC", "12"))


CONF_THRESHOLD = float(os.getenv("CONF_THRESHOLD", "0.25"))          # 이 이상만 사람으로 셈
LOW_CONF_THRESHOLD = float(os.getenv("LOW_CONF_THRESHOLD", "0.5"))   # 이 미만이면 '신뢰도 낮음'
IOU_THRESHOLD = float(os.getenv("IOU_THRESHOLD", "0.5"))             # 붙어 앉은 사람이 하나로 합쳐지면 올림
IMG_SIZE = int(os.getenv("IMG_SIZE", "640"))                         # 사람이 작게 찍히면 960~1280

log = logging.getLogger("detector")
_models: list[tuple[str, object, list[int] | None]] = []


def _try_load_yolo(model_path: str):
    from ultralytics import YOLO

    model = YOLO(model_path)
    names = model.names
    if len(names) == 1:
        person_ids = None
    else:
        person_ids = [
            i for i, n in names.items()
            if str(n).strip().lower() in {"person", "passenger"}
        ]
        person_ids = person_ids or [0]
    return model, person_ids


def load_model():
    """설정된 모든 모델을 메모리에 올려 앙상블 추론에 사용한다."""
    global _models
    if _models:
        return _models

    loaded_models = []
    for model_path in YOLO_MODELS:
        try:
            model, person_classes = _try_load_yolo(model_path)
            loaded_models.append((model_path, model, person_classes))
            log.info("YOLO 로딩 완료: %s / 클래스 %s / 사람 클래스 %s",
                     model_path, model.names, person_classes or "전체")
        except Exception as e:  # noqa: BLE001
            log.warning("YOLO 모델 로드 실패: %s / %s", model_path, e)

    if len(loaded_models) != len(YOLO_MODELS):
        raise RuntimeError(
            f"설정된 YOLO 모델 {len(YOLO_MODELS)}개 중 "
            f"{len(loaded_models)}개만 로드되어 앙상블을 실행할 수 없습니다."
        )
    if not loaded_models:
        raise RuntimeError("설정된 YOLO 모델이 없습니다.")

    _models = loaded_models
    return _models


def open_image(image_bytes: bytes):
    """휴대폰 사진은 실제 픽셀은 옆으로 누워 있고 '회전 정보(EXIF)'만 붙어 있는 경우가 많다.
    PIL 은 이 정보를 자동 적용하지 않아서, 그대로 넣으면 모델이 90도 돌아간 사진을 보게 된다.
    (Roboflow 는 내보낼 때 Auto-Orient 로 이미 회전을 적용하므로 학습·평가 사진은 똑바로 서 있음)"""
    from PIL import Image, ImageOps

    img = Image.open(io.BytesIO(image_bytes))
    return ImageOps.exif_transpose(img).convert("RGB")


def _real_detect(image_bytes: bytes) -> list[dict]:
    models = load_model()
    img = open_image(image_bytes)
    detections = []
    for _, model, person_classes in models:
        result = model.predict(
            img,
            classes=person_classes,
            conf=CONF_THRESHOLD,
            iou=IOU_THRESHOLD,
            imgsz=IMG_SIZE,
            verbose=False,
        )[0]
        for box in result.boxes:
            detections.append({
                "label": "person",
                "conf": round(float(box.conf[0]), 3),
                "bbox": [round(float(v), 1) for v in box.xyxy[0].tolist()],
            })
    return _merge_detections(detections)


def _box_iou(left: list[float], right: list[float]) -> float:
    x1 = max(left[0], right[0])
    y1 = max(left[1], right[1])
    x2 = min(left[2], right[2])
    y2 = min(left[3], right[3])
    intersection = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    left_area = max(0.0, left[2] - left[0]) * max(0.0, left[3] - left[1])
    right_area = max(0.0, right[2] - right[0]) * max(0.0, right[3] - right[1])
    union = left_area + right_area - intersection
    return intersection / union if union else 0.0


def _merge_detections(detections: list[dict]) -> list[dict]:
    """Suppress overlapping detections from different models to avoid duplicate counts."""
    kept = []
    for detection in sorted(detections, key=lambda item: item["conf"], reverse=True):
        if any(
            _box_iou(detection["bbox"], existing["bbox"]) > IOU_THRESHOLD
            for existing in kept
        ):
            continue
        kept.append(detection)
    return kept


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
            # 주의: 이 결과는 실제 사진을 본 게 아니다. 라우터(run_detector)에서 반드시 막아야 한다.
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
    loaded_paths = [model_path for model_path, _, _ in _models]
    return {
        "mode": MODE,
        "model_path": loaded_paths[0] if loaded_paths else YOLO_MODEL,
        "model_candidates": YOLO_MODELS,
        "model_exists": all(Path(p).exists() for p in YOLO_MODELS),
        "model_loaded": len(loaded_paths) == len(YOLO_MODELS) and bool(loaded_paths),
        "models_loaded": loaded_paths,
        "model_count": len(YOLO_MODELS),
        "conf": CONF_THRESHOLD,
        "low_conf": LOW_CONF_THRESHOLD,
        "iou": IOU_THRESHOLD,
        "imgsz": IMG_SIZE,
    }