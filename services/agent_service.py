"""
============================================================
services/agent_service.py — 경고 발령 판단 Agent  (AI ④)
============================================================
🟥 [AI] road_hazard/services/agent_service.py 를 미하차 경고용으로 옮긴 것
        (road_hazard: 심각도 보고 '신고할지' 판단 → 여기: 판정 결과·사진 상태 보고 '어떻게 경고할지' 판단)

역할: judge.py 의 판정(OK / MISSED / NEEDS_CHECK)을 받아
      ① 경고 등급을 최종 결정 (올리기만 하고 내리지는 않음)
      ② 학부모에게까지 알릴지, 재촬영을 요청할지 결정
      ③ 기사에게 보여줄 행동 안내문(guidance) 작성

🔴 안전 원칙 — 판단은 규칙으로, 문장만 LLM
   - 등급·알림 대상·재촬영 여부는 아래 규칙(코드)으로만 정한다. LLM 은 이 값을 바꿀 수 없다.
   - Agent 는 등급을 '올리기만' 한다. (예: 정상인데 사진이 너무 어두움 → 확인 필요로 올림)
     판정 로직이 미하차 의심이라고 한 것을 Agent 가 내리는 일은 없다.
   - AGENT_LLM_GUIDANCE=true 면 Gemini 가 RAG 대응 수칙을 근거로 안내문만 다듬는다.
     (기본 false: 종점 사진 응답이 LLM 때문에 늦어지지 않게 템플릿 안내문 사용)

규칙
   판정            사진 상태   → 최종                     학부모  재촬영
   MISSED          -          → MISSED (긴급)            알림    -
   NEEDS_CHECK     -          → NEEDS_CHECK (확인 필요)   -      요청
   OK              어두움     → NEEDS_CHECK (확인 필요)   -      요청   ← AGENT_ESCALATE_DARK
   OK              보통       → OK                       -      -

.env
   AGENT_ESCALATE_DARK=true          # 어두운 종점 사진은 '정상'으로 넘기지 않음
   DARK_BRIGHTNESS_THRESHOLD=60      # 평균 밝기(0~255)가 이보다 낮으면 어두움
   AGENT_LLM_GUIDANCE=false
============================================================
"""
import io
import logging
import os

from dotenv import load_dotenv

from models.models import AlertLevel, TripResult
from services import gemini_client

load_dotenv()
log = logging.getLogger("agent")

ESCALATE_DARK = os.getenv("AGENT_ESCALATE_DARK", "true").lower() == "true"
DARK_THRESHOLD = float(os.getenv("DARK_BRIGHTNESS_THRESHOLD", "60"))
USE_REAL_AI = os.getenv("AGENT_LLM_GUIDANCE", "false").lower() == "true" and gemini_client.available()
FALLBACK_ON_ERROR = True


# ---------- 사진 상태 ----------
def photo_brightness(image_bytes: bytes | None) -> float | None:
    """평균 밝기 0~255 (회전 정보 반영, 작게 줄여서 빠르게 계산). 읽을 수 없으면 None"""
    if not image_bytes:
        return None
    try:
        from PIL import Image, ImageOps, ImageStat
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(image_bytes))).convert("L")
        img.thumbnail((256, 256))
        return round(ImageStat.Stat(img).mean[0], 1)
    except Exception:  # noqa: BLE001
        return None


def photo_check(image_bytes: bytes | None) -> dict:
    """승차 사진에도 쓰는 간단한 사진 점검"""
    b = photo_brightness(image_bytes)
    dark = b is not None and b < DARK_THRESHOLD
    return {
        "brightness": b,
        "photo_dark": dark,
        "guidance": "사진이 어둡습니다. 실내등을 켜고 다시 촬영하면 더 정확합니다." if dark else "",
    }


# ---------- 규칙 판단 (LLM 이 바꿀 수 없는 부분) ----------
def _decide_rules(verdict: dict, brightness: float | None) -> dict:
    result = verdict["result"]
    level = verdict["level"]
    message = verdict["message"]
    reasons: list[str] = []
    dark = brightness is not None and brightness < DARK_THRESHOLD

    if result == TripResult.OK and dark and ESCALATE_DARK:
        result, level = TripResult.NEEDS_CHECK, AlertLevel.CHECK
        message = "확인 필요: 사진이 어두워 잔류 인원을 확인할 수 없습니다 · 실내등을 켜고 재촬영하세요"
        reasons.append("dark_photo_escalated")
    elif dark:
        reasons.append("dark_photo")

    if result == TripResult.MISSED:
        reasons.append("sure_detection")
    elif result == TripResult.NEEDS_CHECK and "dark_photo_escalated" not in reasons:
        reasons.append("low_confidence")

    return {
        **verdict,
        "result": result,
        "level": level,
        "message": message,
        "notify_parents": result == TripResult.MISSED,          # notifier 와 같은 기준 (긴급만)
        "retake_recommended": result == TripResult.NEEDS_CHECK,
        "brightness": brightness,
        "reasons": reasons,
    }


# ---------- 안내문 ----------
def _mock_guidance(decision: dict) -> str:
    r = decision["result"]
    if r == TripResult.MISSED:
        return ("즉시 차량 내부를 직접 확인하세요. 좌석 아래와 뒷좌석까지 살펴보고, "
                "아이가 있으면 상태를 확인한 뒤 학교와 보호자에게 연락하세요. "
                "확인이 끝나면 앱에서 경고를 '해결' 처리하세요.")
    if r == TripResult.NEEDS_CHECK:
        tip = "실내등을 켜고 " if "dark_photo_escalated" in decision["reasons"] or "dark_photo" in decision["reasons"] else ""
        return (f"{tip}종점 사진을 다시 촬영하거나 차량 내부를 직접 확인하세요. "
                "재촬영 결과가 정상이면 경고가 자동으로 해결됩니다.")
    return "하차 확인 완료. 운행 종료 전에 뒷좌석까지 직접 확인한 뒤 '안전 확인 완료'를 눌러 주세요."


def _real_guidance(decision: dict, rules: list[dict]) -> str:
    reg = "\n".join(f"- {r['title']}: {r['content']}" for r in rules) or "- (검색된 수칙 없음)"
    prompt = f"""[판정] {decision['message']}
[결정] 학부모 알림 {'예' if decision['notify_parents'] else '아니오'} / 재촬영 요청 {'예' if decision['retake_recommended'] else '아니오'}
[대응 수칙]
{reg}

위 결정을 바꾸지 말고, 기사에게 보여줄 행동 안내문을 2~3문장으로 쓰세요.
대응 수칙에 없는 절차는 만들지 마세요. JSON 으로만: {{"guidance": "..."}}"""
    data = gemini_client.generate_json(
        prompt, system="당신은 통학버스 기사에게 짧고 분명한 행동 지침을 주는 안전 보조 AI입니다. 존댓말.",
        max_tokens=300,
    )
    return str(data.get("guidance") or "").strip()[:300]


def decide(verdict: dict, image_bytes: bytes | None = None) -> dict:
    """judge() 결과 + 종점 사진 → 최종 판단 dict
    (judge 결과의 키는 그대로 유지하고 notify_parents / retake_recommended / guidance / reasons / brightness 추가)"""
    decision = _decide_rules(verdict, photo_brightness(image_bytes))
    guidance = _mock_guidance(decision)
    if USE_REAL_AI and decision["result"] != TripResult.OK:
        try:
            from services.rag_service import search_by_text
            rules = search_by_text(decision["message"] + " 대응 수칙", k=3)
            guidance = _real_guidance(decision, rules) or guidance
        except Exception as e:  # noqa: BLE001
            log.warning("❌ [Agent] 안내문 생성 실패: %s: %s → 템플릿 안내문 사용", type(e).__name__, e)
            if not FALLBACK_ON_ERROR:
                raise
    decision["guidance"] = guidance
    return decision


def status() -> dict:
    return {"agent_llm_guidance": USE_REAL_AI, "escalate_dark": ESCALATE_DARK,
            "dark_threshold": DARK_THRESHOLD}
