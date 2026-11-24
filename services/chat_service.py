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
   - (11/23 개선) 규정 반복 문제 수정
       · 사용자 상황에 '오늘 우리 아이 승차·하차 확인 시각, 운행 결과, 내일 결석'까지 넣어서
         "우리 아이 내렸어요?" 같은 질문에 실제 상태로 답한다
       · 문서에 딱 맞는 내용이 없어도 고정 거절문 대신 상황에 맞는 해결 방법을 단계로 안내
         (법 조항·벌칙·전화번호·아이 상태는 여전히 지어내지 않음)
       · JSON 모드 → 글 모드: 답이 길어 잘리면 JSON 파싱이 실패해 '문서 발췌'로 떨어지던 문제 해결
       · AI 가 실패해도 상태 질문은 사용자 상황으로 답하고, 문서 발췌는 짧게
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
MAX_REPLY_CHARS = 1800
MAX_OUTPUT_TOKENS = 1500
SEARCH_K = 4

# 기관 연락처 — 시연용 임시 번호 (실제 기관 번호가 생기면 .env 의 INSTITUTION_PHONE 만 바꾸면 된다)
INSTITUTION_PHONE = os.getenv("INSTITUTION_PHONE", "010-0000-0000")

GREETING = {
    "ADMIN": "안녕하세요! 통학버스 안전 안내 챗봇입니다. 🙂\n"
             "경고 처리 방법, 하차 확인 의무, 앱 사용법을 물어보세요.",
    "PARENT": "안녕하세요! 통학버스 안전 도우미입니다. 🙂\n"
              "오늘 아이의 승차·하차 상태, 알림의 뜻, 결석 설정처럼 궁금한 점을 편하게 물어보세요.",
}
SUGGESTIONS = {
    "ADMIN": ["긴급 경고가 오면 무엇을 해야 하나요?", "확인 필요 경고는 어떻게 처리하나요?",
              "운행 후 하차 확인 의무가 뭔가요?"],
    "PARENT": ["우리 아이 오늘 하차했나요?", "미하차 의심 알림을 받으면 어떻게 하나요?",
               "내일 아이가 버스를 안 타요"],
}

# 🚨 급한 상황 표현 — 있으면 고정 안전 안내를 맨 앞에 붙인다 (LLM 이 빠뜨려도 항상 나가게)
_EMERGENCY_RE = re.compile(
    r"(안\s*내렸|못\s*내렸|안\s*내린|갇혔|갇힌|갇혀|남아\s*있|두고\s*내|방치|숨을|숨\s*안|의식|쓰러|"
    r"반응이?\s*없|열사병|경련|구토|119|응급|안\s*보여|없어졌|실종)"
)
EMERGENCY_NOTICE = (
    "🚨 급한 상황이라면 챗봇 답을 기다리지 말고 지금 바로 차량 맨 뒷좌석까지 직접 확인하세요. "
    f"아이가 반응이 없거나 몸이 뜨거우면 즉시 119에 신고하고 기관({INSTITUTION_PHONE})에 알리세요."
)

# '기관에 연락·문의·전화하세요' 류의 문장 — 답에 번호가 없으면 끝에 붙인다
_CONTACT_RE = re.compile(r"(기관|관리자|선생님|원)[^\n]{0,15}(연락|문의|전화|알리|확인해\s*주)")


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
    if _CONTACT_RE.search(reply) and INSTITUTION_PHONE not in reply:
        reply = f"{reply}\n\n📞 기관 연락처: {INSTITUTION_PHONE}"

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


# 상태를 묻는 질문 — AI 가 실패해도 문서 발췌 대신 사용자 상황으로 답한다
_STATUS_RE = re.compile(r"(내렸|하차|승차|탔|탑승|도착|출발|운행|지금|어디|상태|결석|경고|알림\s*왔)")


# ---------- mock: 사용자 상황 / 문서 발췌로 답 ----------
def _mock_reply(message: str, history: list, context: dict | None, docs: list[dict]) -> dict:
    ctx = _context_lines(context)
    lines: list[str] = []
    if ctx and _STATUS_RE.search(message or ""):
        lines.append("📋 지금 확인되는 상황입니다.\n" + "\n".join(f"· {x}" for x in ctx))
    if docs and not lines:                      # 상태 질문이면 규정 발췌는 붙이지 않는다
        top = docs[0]
        body = top["content"]
        if len(body) > 300:
            body = body[:300].rsplit("\n", 1)[0] + "\n..."
        lines.append(f"📄 관련 안내 — {top['title']}\n{body}")
    if not lines:
        return {"reply": "", "suggestions": []}
    lines.append("(지금은 AI 답변이 잠시 어려워 기록과 안내 문서로 대신 답했습니다. "
                 f"더 궁금한 점은 기관({INSTITUTION_PHONE})에 문의해 주세요.)")
    return {"reply": "\n\n".join(lines), "suggestions": []}


# ---------- 진짜: Gemini + 사용자 상황 + 검색 문서 + 이전 대화 ----------
_SYSTEM = """당신은 '어린이 통학버스 미하차 예방 서비스'(앱 이름: 통학이)의 상담 도우미입니다.
대화 상대: {who}

목표: 규정을 읽어 주는 것이 아니라, 상대의 질문과 지금 상황에 맞춰 '그래서 지금 무엇을 하면 되는지'를 알려 주는 것.

정보를 쓰는 방법
1. [내 상황]: 서버 기록에서 가져온 사실입니다. 아이의 승차·하차, 운행, 결석, 경고 상태를 물으면 이것으로 답하세요.
   여기 없는 상태(위치, 도착 시각, 인원 등)는 절대 지어내지 말고 "기록에서는 확인되지 않는다"고 말한 뒤 확인 방법을 알려 주세요.
2. [문서]: 법령·대응 수칙·앱 사용법입니다. 질문과 관련 있는 문서만 쓰고, 근거로 쓴 문장 끝에 [1], [2]처럼 번호를 붙이세요.
   관련 없는 문서는 무시하세요.
3. 문서에 딱 맞는 내용이 없어도 거절만 하지 마세요. 상식적인 해결 방법(먼저 확인할 것, 누구에게 연락할지, 앱 어디를 보면 되는지)을
   안내하고, 확실하지 않은 부분은 기관(관리자)에게 확인하라고 덧붙이세요.
4. 법 조항·벌칙 금액·기관 운영 규칙은 문서에 있는 것만 말하세요. 추측하지 마세요.
5. 기관 연락처는 {phone} 입니다. 기관에 연락하라고 안내할 때는 이 번호를 함께 적고, 다른 번호는 지어내지 마세요(119 제외).
6. [내 상황]의 '승차 확인·하차 확인'은 보호자가 앱에서 직접 누른 기록입니다. 기관이 확인한 것처럼 말하지 말고,
   차 안에 남은 아이가 있는지는 '종점 확인 결과'로만 말하세요.

답하는 방법
- 질문에 대한 답을 첫 문장에 바로 말하세요. (예: "네, 오늘 오전 8시 42분에 하차 확인이 됐어요.")
- 해야 할 일이 있으면 1. 2. 3. 번호로 짧게.
- 이전 대화에서 이미 한 설명은 반복하지 말고, 이어지는 질문이면 그 다음 단계를 답하세요.
- 친절한 존댓말, 보통 2~6문장. 학부모가 불안해하면 먼저 공감 한마디.
- 확인되지 않은 일로 "괜찮을 거예요"라고 안심시키지 마세요.
- 아이가 차에 남아 있을 수 있는 상황이면 무엇보다 먼저 '즉시 차량 직접 확인, 상태가 나쁘면 119'를 말하고,
  AI 판정이나 앱 화면을 믿고 확인을 미루라고 하지 마세요.
- 통학버스·서비스와 관계없는 질문이면 짧게 답할 수 없다고 하고 도울 수 있는 것을 알려 주세요.

출력 형식 (반드시 지키기)
답변 내용
[추천]
- 이어서 물어볼 만한 짧은 질문
- 짧은 질문"""

_SUGG_SPLIT = re.compile(r"^\s*\[?\s*추천(?:\s*질문)?\s*\]?\s*:?\s*$", re.M)


def _parse_text(text: str) -> dict:
    """'답변 ... [추천] - 질문' 형식을 {"reply", "suggestions"} 로"""
    text = (text or "").strip()
    parts = _SUGG_SPLIT.split(text, maxsplit=1)
    reply = parts[0].strip()
    sugg: list[str] = []
    if len(parts) > 1:
        for line in parts[1].splitlines():
            q = re.sub(r"^\s*(?:[-*•·]|\d+[.)])\s*", "", line).strip()
            if q:
                sugg.append(q)
    return {"reply": reply, "suggestions": sugg}


def _real_reply(message: str, history: list, context: dict | None, docs: list[dict]) -> dict:
    who = "학교·기관 관리자 또는 기사" if _role(context) == "ADMIN" else "학부모(보호자)"
    doc_txt = "\n\n".join(f"[{i}] {d['title']} ({d['source']})\n{d['content']}"
                          for i, d in enumerate(docs, 1)) or "(검색된 문서 없음)"
    ctx = _context_lines(context)
    prompt = f"""[내 상황]
{chr(10).join('· ' + x for x in ctx) if ctx else '(없음)'}

[문서]
{doc_txt}

[질문] {message}"""
    turns = [("model" if h.get("role") == "bot" else "user", str(h.get("text", ""))[:800])
             for h in (history or [])[-MAX_HISTORY_TURNS:]]
    text = gemini_client.generate_text(
        prompt, system=_SYSTEM.format(who=who, phone=INSTITUTION_PHONE), max_tokens=MAX_OUTPUT_TOKENS, temperature=0.5, history=turns,
    )
    return _parse_text(text)


def chat(message: str, history: list | None = None, context: dict | None = None) -> dict:
    """POST /chat 본체"""
    history = history or []
    # 검색은 임베딩이 실패해도 글자 매칭으로 대체되므로, 여기서는 문서만 받아 쓴다
    docs, _ = rag_service.search(_search_query(message, history), SEARCH_K)
    if not USE_REAL_AI:
        log.info("💬 [Chat] mode=mock (GEMINI_API_KEY 없음 또는 CHAT_ENABLED=false) → 문서 발췌로 답함")
        return _normalize(_mock_reply(message, history, context, docs), context, message, docs, "mock")
    try:
        result = _real_reply(message, history, context, docs)
        if not str((result or {}).get("reply") or "").strip():
            raise ValueError("빈 답변")
        log.info("💬 [Chat] mode=real · 문서 %d개", len(docs))
        return _normalize(result, context, message, docs, "real")
    except Exception as e:  # noqa: BLE001
        log.warning("❌ [Chat] 응답 생성 실패: %s: %s", type(e).__name__, e)
        if not FALLBACK_ON_ERROR:
            raise
        log.info("↩️  [Chat] 기록·문서 발췌 답변으로 대체합니다")
        return _normalize(_mock_reply(message, history, context, docs), context, message, docs, "mock-fallback")


def status() -> dict:
    return {"chat_enabled": USE_REAL_AI}
