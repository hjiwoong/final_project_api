"""
============================================================
services/llm_service.py — 경고 상황 LLM 요약 서비스  (AI ②)
============================================================
🟥 [AI] road_hazard/services/llm_service.py 를 미하차 경고용으로 옮긴 것
        (구조 그대로: USE_REAL_AI 스위치 / _mock_ / _real_ / _normalize / 실패 시 폴백)

역할: 미하차·확인 필요 경고가 생기면 상황을 한두 문장으로 요약해 alert.llm_summary 에 저장
      → 관리자 웹 '경고 발생 당시 AI 분석', 학부모 앱 'AI 요약' 카드에 그대로 표시된다.

🔴 안전 원칙
   ① 인원 수(승차·잔류)는 판정 로직이 정한 값만 쓴다. LLM 이 숫자를 바꿀 수 없게
      첫 줄은 항상 서버가 만든 문장(템플릿)으로 고정하고, LLM 문장은 그 뒤에 붙인다.
   ② LLM 이 실패해도 템플릿 요약이 이미 저장돼 있으므로 경고·푸시는 절대 늦어지지 않는다.
      (경고를 만들 때는 템플릿 → 응답 후 백그라운드에서 LLM 문장으로 보강)
   ③ 아이 사진은 기본으로 외부(Gemini)에 보내지 않는다. LLM_SEND_IMAGE=true 일 때만 첨부.
      (무료 등급은 보낸 내용이 Google 제품 개선에 쓰일 수 있음 → 실제 아동 사진에는 끄기)

📤 저장 형식 (alert.llm_summary, 여러 줄 문자열)
   하차 후 1명 잔류 의심 · 승차 5 / 잔류 1
   사진 기준 뒤쪽 오른쪽 좌석에서 신뢰도 0.82로 사람이 감지되었습니다.
   확인할 곳: 3열 좌석 아래, 뒷좌석 바닥

.env
   LLM_ENABLED=true        # false 면 템플릿 요약만 (기본: GEMINI_API_KEY 가 있으면 true)
   LLM_SEND_IMAGE=false    # true 면 종점 사진도 함께 보냄 (시연·성인 촬영 사진에서만 권장)
============================================================
"""
import logging
import os
import re

from dotenv import load_dotenv

from services import gemini_client

load_dotenv()
log = logging.getLogger("llm")

USE_REAL_AI = os.getenv("LLM_ENABLED", "true").lower() == "true" and gemini_client.available()
SEND_IMAGE = os.getenv("LLM_SEND_IMAGE", "false").lower() == "true"
FALLBACK_ON_ERROR = True

MAX_OBS_CHARS = 160
MAX_POINTS = 3


# ---------- 사실(facts) 만들기: LLM 에 넘길 재료는 전부 서버가 계산 ----------
def headline(level: str, boarding: int, remaining: int) -> str:
    """첫 줄 (숫자가 들어가는 문장은 서버가 고정) — notifier.build_summary 와 같은 문장"""
    if level == "URGENT":
        return f"하차 후 {remaining}명 잔류 의심 · 승차 {boarding} / 잔류 {remaining}"
    return "사진의 검출 신뢰도가 낮아 재촬영 또는 관리자 확인이 필요합니다"


def _position(bbox: list[float], width: float, height: float) -> str:
    """박스 위치를 '사진 기준' 말로 바꾼다. 카메라가 앞에서 뒤를 보므로 사진 위쪽 = 뒷좌석 쪽"""
    try:
        cx = (bbox[0] + bbox[2]) / 2 / max(width, 1)
        cy = (bbox[1] + bbox[3]) / 2 / max(height, 1)
    except (TypeError, IndexError):
        return "위치 미상"
    horiz = "왼쪽" if cx < 0.36 else ("오른쪽" if cx > 0.64 else "가운데")
    depth = "뒤쪽" if cy < 0.45 else ("중간" if cy < 0.68 else "앞쪽")
    return f"{depth} {horiz}"


def _image_size(image_path: str | None, detections: list[dict]) -> tuple[float, float]:
    if image_path and os.path.exists(image_path):
        try:
            from PIL import Image, ImageOps
            with Image.open(image_path) as im:
                return ImageOps.exif_transpose(im).size
        except Exception:  # noqa: BLE001
            pass
    xs = [d["bbox"][2] for d in detections if d.get("bbox")] or [640]
    ys = [d["bbox"][3] for d in detections if d.get("bbox")] or [640]
    return max(max(xs), 640), max(max(ys), 640)


def build_facts(*, level: str, message: str, boarding: int, remaining: int, low_conf: int,
                detections: list[dict], bus_name: str | None, image_path: str | None,
                low_conf_threshold: float, brightness: float | None = None) -> dict:
    w, h = _image_size(image_path, detections)
    people = [
        {"위치": _position(d.get("bbox") or [], w, h),
         "신뢰도": round(float(d.get("conf", 0)), 2),
         "확실함": float(d.get("conf", 0)) >= low_conf_threshold}
        for d in sorted(detections or [], key=lambda d: -float(d.get("conf", 0)))
    ]
    return {
        "level": level, "message": message, "bus_name": bus_name or "통학버스",
        "boarding": boarding, "remaining": remaining, "low_conf": low_conf,
        "people": people, "brightness": brightness,
    }


# ---------- 정규화 (LLM 이 뭘 주든 앱이 안 죽게) ----------
_NUM_RE = re.compile(r"\d+(?:\.\d+)?")


def _allowed_numbers(facts: dict) -> set[str]:
    """요약에 나와도 되는 숫자 = 판정 로직이 준 값뿐 (인원·신뢰도 등)"""
    nums = {facts.get("boarding"), facts.get("remaining"), facts.get("low_conf"), len(facts.get("people") or [])}
    allowed = {str(n) for n in nums if n is not None}
    for p in facts.get("people") or []:
        c = float(p.get("신뢰도", 0))
        allowed |= {f"{c:.2f}", f"{c:g}", str(round(c * 100))}
    return allowed


def _has_new_number(text: str, allowed: set[str]) -> bool:
    return any(n not in allowed for n in _NUM_RE.findall(text or ""))


def _normalize(result, facts: dict) -> dict:
    if not isinstance(result, dict):
        result = {}
    obs = str(result.get("observation") or "").strip().replace("\n", " ")[:MAX_OBS_CHARS]
    raw = result.get("check_points")
    if isinstance(raw, list):
        points = [str(x).strip()[:40] for x in raw if x][:MAX_POINTS]
    elif raw:
        points = [str(raw)[:40]]
    else:
        points = []
    # 안전장치: LLM 이 판정 로직에 없는 숫자(인원 등)를 지어내면 그 문장은 버린다
    allowed = _allowed_numbers(facts)
    if _has_new_number(obs, allowed):
        log.warning("⚠️ [LLM] 요약에 판정에 없는 숫자가 있어 버립니다: %s", obs)
        obs = ""
    points = [p for p in points if not _has_new_number(p, allowed)]
    return {"observation": obs, "check_points": points}


def compose(facts: dict, result: dict) -> str:
    """첫 줄(서버 고정) + LLM 관찰 문장 + 확인할 곳"""
    lines = [headline(facts["level"], facts["boarding"], facts["remaining"])]
    if result.get("observation"):
        lines.append(result["observation"])
    if result.get("check_points"):
        lines.append("확인할 곳: " + ", ".join(result["check_points"]))
    return "\n".join(lines)


# ---------- mock: 키 없이도 그럴듯한 요약 ----------
def _mock_summarize(facts: dict) -> dict:
    people = facts["people"]
    if facts["level"] == "URGENT" and people:
        top = people[0]
        obs = f"사진 기준 {top['위치']} 좌석 부근에서 신뢰도 {top['신뢰도']:.2f}로 사람이 감지되었습니다."
        points = [f"{top['위치']} 좌석과 그 아래", "뒷좌석 바닥", "좌석 사이 통로"]
    elif people:
        obs = f"신뢰도가 낮은 검출 {len(people)}건이 있어 사람인지 물건인지 확실하지 않습니다."
        points = [f"{people[0]['위치']} 좌석", "실내등을 켜고 재촬영"]
    else:
        obs = "사진에서 사람이 확실하게 감지되지 않았지만 확인이 필요한 상태입니다."
        points = ["뒷좌석과 좌석 아래", "실내등을 켜고 재촬영"]
    return {"observation": obs, "check_points": points}


# ---------- real: Gemini ----------
_SYSTEM = """당신은 어린이 통학버스 미하차(차 안에 아이가 남는 사고) 예방 서비스의 안전 보조 AI입니다.
기사와 관리자가 차량을 직접 확인할 때 참고할 짧은 요약을 씁니다.

반드시 지킬 것:
- 인원 수는 주어진 값만 사용하고, 새로운 숫자를 만들지 마세요.
- 사람의 외모·옷·나이·신원을 추측하거나 묘사하지 마세요.
- 위치는 '사진 기준' 표현(앞쪽/중간/뒤쪽, 왼쪽/가운데/오른쪽)만 쓰세요.
- 확실하지 않으면 '~로 보입니다', '확인이 필요합니다'처럼 쓰세요.
- 안심시키는 말(예: '문제없습니다')은 쓰지 마세요. 판단은 사람이 직접 확인해서 합니다.
- 존댓말, 짧게."""


def _real_summarize(facts: dict, image_bytes: bytes | None) -> dict:
    people_txt = "\n".join(
        f"- {p['위치']} / 신뢰도 {p['신뢰도']} / {'확실' if p['확실함'] else '불확실'}"
        for p in facts["people"]
    ) or "- 없음"
    bright = facts.get("brightness")
    bright_txt = f"{bright:.0f}/255 ({'어두움' if bright < 60 else '보통'})" if bright is not None else "모름"
    prompt = f"""[경고] {facts['message']}  (등급: {'긴급' if facts['level'] == 'URGENT' else '확인 필요'})
[차량] {facts['bus_name']}
[인원] 승차 {facts['boarding']}명 / 종점 잔류 {facts['remaining']}명 / 신뢰도 낮은 검출 {facts['low_conf']}건
[검출 목록 — 사진 기준 위치]
{people_txt}
[사진 밝기] {bright_txt}
{'[첨부] 종점 실내 사진' if image_bytes else '[첨부 없음] 사진은 보내지 않았습니다. 위 정보만 사용하세요.'}

아래 JSON 으로만 답하세요:
{{
  "observation": "상황을 1~2문장으로 (어디에서 무엇이 감지됐는지, 왜 확인이 필요한지)",
  "check_points": ["기사가 직접 확인할 곳 2~3개 (짧게)"]
}}"""
    return gemini_client.generate_json(prompt, image_bytes=image_bytes, system=_SYSTEM, max_tokens=400)


def summarize(facts: dict, image_bytes: bytes | None = None) -> str:
    """요약 문자열을 돌려준다 (실패하면 mock 문장으로)"""
    if not USE_REAL_AI:
        return compose(facts, _normalize(_mock_summarize(facts), facts))
    try:
        result = _normalize(_real_summarize(facts, image_bytes if SEND_IMAGE else None), facts)
        if not result["observation"]:
            raise ValueError("빈 요약")
        return compose(facts, result)
    except Exception as e:  # noqa: BLE001
        log.warning("❌ [LLM] 요약 실패: %s: %s", type(e).__name__, e)
        if not FALLBACK_ON_ERROR:
            raise
        log.info("↩️  [LLM] mock 요약으로 대체합니다")
        return compose(facts, _normalize(_mock_summarize(facts), facts))


# ---------- 경고 생성 후 백그라운드에서 요약 보강 ----------
def enrich_alert_summary(alert_id: int, brightness: float | None = None) -> None:
    """dropoff-photo 응답을 보낸 뒤 BackgroundTasks 로 실행.
    경고에는 이미 템플릿 요약이 저장돼 있고, 여기서 LLM 문장으로 덧붙여 갱신한 뒤 SSE 로 알린다."""
    from database.db_connection import SessionLocal
    from models.models import Alert, AlertStatus
    from services import event_bus
    from services import detector
    LOW_CONF_THRESHOLD = getattr(detector, "LOW_CONF_THRESHOLD", 0.5)
    from services.serializers import alert_to_dict

    try:
        with SessionLocal() as db:
            a = db.get(Alert, alert_id)
            if a is None or a.status == AlertStatus.RESOLVED:
                return
            ev, trip = a.event, a.trip
            detections = (ev.detections or []) if ev else []
            facts = build_facts(
                level=a.level.value, message=a.message or "",
                boarding=trip.boarding_count or 0, remaining=trip.remaining_count or 0,
                low_conf=ev.low_conf_count if ev else 0, detections=detections,
                bus_name=trip.bus.name if trip and trip.bus else None,
                image_path=ev.image_path if ev else None,
                low_conf_threshold=LOW_CONF_THRESHOLD, brightness=brightness,
            )
            image_bytes = None
            if SEND_IMAGE and ev and ev.image_path and os.path.exists(ev.image_path):
                with open(ev.image_path, "rb") as f:
                    image_bytes = f.read()
            a.llm_summary = summarize(facts, image_bytes)
            db.commit()
            db.refresh(a)
            event_bus.publish("alert", alert_to_dict(a))
    except Exception:  # noqa: BLE001  — 요약 보강 실패는 경고 자체에 영향 없음
        log.exception("경고 %s 요약 보강 실패 (템플릿 요약 유지)", alert_id)


def status() -> dict:
    return {"llm_enabled": USE_REAL_AI, "llm_send_image": SEND_IMAGE}
