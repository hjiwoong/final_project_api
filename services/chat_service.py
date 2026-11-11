"""
============================================================
services/chat_service.py — 안전 안내 챗봇  (AI ③ RAG 확장)
============================================================
🟥 [AI] road_hazard/services/chat_service.py 를 통학버스 서비스용으로 옮긴 것
        (구조 그대로: USE_REAL_AI 스위치 / _mock_reply / _real_reply / _normalize / chat())

바뀐 점
   - 답변 근거: 코드 안 _FAQ 목록 → knowledge/ 문서 전체(RAG 검색)
       · 법령 PDF, 기관 안전 매뉴얼 PDF, 대응 수칙, 앱 이용 안내(04_앱_이용_안내.md)
       · FAQ 를 고치려면 코드가 아니라 knowledge/04_앱_이용_안내.md 를 고치면 된다
   - LLM: OpenAI gpt-4o-mini → Gemini (무료) · 이전 대화(history) 6턴까지 함께 보냄
   - 사용자 상황: 신고 현황 → 학부모는 내 아이 버스의 진행 중 경고·오늘 결석 여부,
                  관리자는 처리 안 된 경고 (아이 이름은 Gemini 로 보내지 않음)
   - 🚨 안전 규칙(Agent): '아이가 안 내렸다·갇혔다·숨을 안 쉰다' 같은 말이 있으면
       LLM 답과 상관없이 맨 앞에 '지금 바로 차량 확인 · 119' 안내를 고정으로 붙인다

📤 출력 형식 (road_hazard 와 같고 sources·mode 추가)
   {"reply": "...", "suggestions": ["...", "..."], "sources": [...], "mode": "real" | "mock" | "mock-fallback"}

.env
   CHAT_ENABLED=true    # false 면 문서 검색 발췌로만 답한다 (기본: GEMINI_API_KEY 가 있으면 true)
============================================================
"""
import logging
import os
import re

from dotenv import load_dotenv

from services import gemini_client, rag_service

load_dotenv()
log = logging.getLogger("chat")

USE_REAL_AI = os.getenv("CHAT_ENABLED", "true").lower() == "true" and gemini_client.available()
FALLBACK_ON_ERROR = True

MAX_SUGGESTIONS = 3
MAX_HISTORY_TURNS = 6
MAX_REPLY_CHARS = 1500
SEARCH_K = 4

GREETING = {
    "ADMIN": "안녕하세요! 통학버스 안전 안내 챗봇입니다. 🙂\n"
             "경고 처리 방법, 하차 확인 의무, 앱 사용법을 물어보세요.",
    "PARENT": "안녕하세요! 통학버스 안전 안내 챗봇입니다. 🙂\n"
              "경고 알림의 뜻, 결석 설정, 자녀 연결 방법 등을 물어보세요.",
}
SUGGESTIONS = {
    "ADMIN": ["긴급 경고가 오면 무엇을 해야 하나요?", "확인 필요 경고는 어떻게 처리하나요?",
              "운행 후 하차 확인 의무가 뭔가요?"],
    "PARENT": ["미하차 의심 알림을 받으면 어떻게 하나요?", "아이가 오늘 버스를 안 타요",
               "AI 요약은 무엇인가요?"],
}

# 🚨 급한 상황 표현 — 있으면 고정 안전 안내를 맨 앞에 붙인다 (LLM 이 빠뜨려도 항상 나가게)
_EMERGENCY_RE = re.compile(
    r"(안\s*내렸|못\s*내렸|안\s*내린|갇혔|갇힌|갇혀|남아\s*있|두고\s*내|방치|숨을|숨\s*안|의식|쓰러|"
    r"반응이?\s*없|열사병|경련|구토|119|응급|안\s*보여|없어졌|실종)"
)
EMERGENCY_NOTICE = (
    "🚨 급한 상황이라면 챗봇 답을 기다리지 말고 지금 바로 차량 맨 뒷좌석까지 직접 확인하세요. "
    "아이가 반응이 없거나 몸이 뜨거우면 즉시 119에 신고하고 기관에 알리세요."
)


def _role(context: dict | None) -> str:
    return "ADMIN" if (context or {}).get("role") == "ADMIN" else "PARENT"


def hello(role: str) -> dict:
    role = "ADMIN" if role == "ADMIN" else "PARENT"
    return {"reply": GREETING[role], "suggestions": SUGGESTIONS[role]}


def is_emergency(message: str) -> bool:
    return bool(_EMERGENCY_RE.search(message or ""))


def _context_lines(context: dict | None) -> list[str]:
    return [str(x) for x in (context or {}).get("lines", []) if x]


# ---------- 정규화 (LLM 이 뭘 주든 앱이 안 죽게) ----------
def _normalize(result, context: dict | None, message: str, sources: list[dict], mode: str) -> dict:
    if not isinstance(result, dict):
        result = {}
    reply = str(result.get("reply") or "").strip()[:MAX_REPLY_CHARS]
    if not reply:
        reply = ("죄송합니다. 잘 이해하지 못했습니다. 😢\n"
                 "아래 질문 중에서 골라 보시거나, 다른 표현으로 다시 물어봐 주세요.")
    if is_emergency(message) and not reply.startswith("🚨"):
        reply = f"{EMERGENCY_NOTICE}\n\n{reply}"

    raw = result.get("suggestions")
    if isinstance(raw, list):
        sugg = [str(x).strip()[:60] for x in raw if x and str(x).strip()][:MAX_SUGGESTIONS]
    elif raw:
        sugg = [str(raw)[:60]]
    else:
        sugg = []
    if not sugg:
        sugg = SUGGESTIONS[_role(context)]
    return {"reply": reply, "suggestions": sugg, "sources": rag_service.sources_of(sources), "mode": mode}


def _search_query(message: str, history: list) -> str:
    """'그럼 보호자는요?' 같은 이어지는 질문도 찾을 수 있게 직전 사용자 질문을 붙여 검색"""
    prev = [h.get("text", "") for h in (history or []) if h.get("role") == "user"]
    return f"{message}\n{prev[-1]}" if prev else message


# ---------- mock: 문서 검색 발췌로 답 ----------
def _mock_reply(message: str, history: list, context: dict | None, docs: list[dict]) -> dict:
    if not docs:
        return {"reply": "", "suggestions": []}
    top = docs[0]
    body = top["content"]
    if len(body) > 500:
        body = body[:500].rsplit("\n", 1)[0] + "\n..."
    lines = [f"관련 안내를 찾았습니다.\n\n📄 {top['title']}\n{body}"]
    if len(docs) > 1:
        lines.append("\n함께 볼 문서: " + ", ".join(d["title"] for d in docs[1:3]))
    ctx = _context_lines(context)
    if ctx and re.search(r"경고|알림|운행|상태|결석", message or ""):
        lines.append("\n───────────────\n📋 현재 상황\n" + "\n".join(f"· {x}" for x in ctx))
    return {"reply": "\n".join(lines), "suggestions": []}


# ---------- 진짜: Gemini + 검색 문서 + 이전 대화 ----------
_SYSTEM = """당신은 '어린이 통학버스 미하차 예방 서비스'의 안전 안내 챗봇입니다.
대화 상대: {who}

규칙
1. 아래 [문서]에 있는 내용만 근거로 답하세요. 문서에 없으면 지어내지 말고
   "등록된 안내 문서에서 찾지 못했습니다. 기관(관리자)에게 문의해 주세요."라고 답하세요.
2. 법 조항·벌칙 금액은 문서에 적힌 그대로만 말하세요. 추측하지 마세요.
3. 근거가 된 문장 끝에 [1], [2]처럼 문서 번호를 붙이세요.
4. 아이가 차에 남아 있을 수 있는 상황이면 무엇보다 먼저 '즉시 차량 직접 확인'과 '상태가 나쁘면 119'를 말하세요.
   AI 판정이나 앱 화면을 믿고 확인을 미루라고 하지 마세요.
5. [사용자 상황]은 참고만 하고, 거기 없는 경고·인원·상태를 지어내지 마세요.
6. 친절한 존댓말, 3~6문장. 목록이 필요하면 짧은 번호 목록.

JSON 으로만 답하세요: {{"reply": "답변", "suggestions": ["이어서 물어볼 만한 짧은 질문 2~3개"]}}"""


def _real_reply(message: str, history: list, context: dict | None, docs: list[dict]) -> dict:
    who = "학교·기관 관리자 또는 기사" if _role(context) == "ADMIN" else "학부모(보호자)"
    doc_txt = "\n\n".join(f"[{i}] {d['title']} ({d['source']})\n{d['content']}"
                          for i, d in enumerate(docs, 1)) or "(검색된 문서 없음)"
    ctx = _context_lines(context)
    prompt = f"""[문서]
{doc_txt}

[사용자 상황]
{chr(10).join('· ' + x for x in ctx) if ctx else '(없음)'}

[질문] {message}"""
    turns = [("model" if h.get("role") == "bot" else "user", str(h.get("text", ""))[:800])
             for h in (history or [])[-MAX_HISTORY_TURNS:]]
    return gemini_client.generate_json(
        prompt, system=_SYSTEM.format(who=who), max_tokens=700, temperature=0.3, history=turns,
    )


def chat(message: str, history: list | None = None, context: dict | None = None) -> dict:
    """POST /chat 본체"""
    history = history or []
    # 검색은 임베딩이 실패해도 글자 매칭으로 대체되므로, 여기서는 문서만 받아 쓴다
    docs, _ = rag_service.search(_search_query(message, history), SEARCH_K)
    if not USE_REAL_AI:
        return _normalize(_mock_reply(message, history, context, docs), context, message, docs, "mock")
    try:
        result = _real_reply(message, history, context, docs)
        if not str((result or {}).get("reply") or "").strip():
            raise ValueError("빈 답변")
        return _normalize(result, context, message, docs, "real")
    except Exception as e:  # noqa: BLE001
        log.warning("❌ [Chat] 응답 생성 실패: %s: %s", type(e).__name__, e)
        if not FALLBACK_ON_ERROR:
            raise
        log.info("↩️  [Chat] 문서 발췌 답변으로 대체합니다")
        return _normalize(_mock_reply(message, history, context, docs), context, message, docs, "mock-fallback")


def status() -> dict:
    return {"chat_enabled": USE_REAL_AI}
