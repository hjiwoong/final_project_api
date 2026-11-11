"""
============================================================
services/gemini_client.py — Gemini API 공용 클라이언트
============================================================
🟥 [AI] road_hazard 의 OpenAI 호출(client.chat.completions / OpenAIEmbeddings)을
        Gemini 무료 API 로 바꾼 공용 함수 모음

LLM 요약(llm_service) · Agent 안내문(agent_service) · RAG(rag_service) 가 모두 이 파일만 쓴다.
모델 이름은 .env 로 바꿀 수 있다.

    GEMINI_API_KEY=...                       # Google AI Studio 에서 발급 (저장소에 올리지 말 것)
    GEMINI_MODEL=gemini-3.5-flash-lite       # 무료 등급 텍스트·이미지 모델
    GEMINI_EMBED_MODEL=gemini-embedding-2    # 무료 등급 임베딩 모델
    GEMINI_TIMEOUT_SEC=20

⚠️ 무료 등급은 보낸 내용이 Google 제품 개선에 쓰일 수 있다.
   그래서 아이 사진은 기본으로 보내지 않는다 (llm_service 의 LLM_SEND_IMAGE 참고).
============================================================
"""
import io
import json
import logging
import math
import os
import re
import time

from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger("gemini")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or ""
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
GEMINI_EMBED_MODEL = os.getenv("GEMINI_EMBED_MODEL", "gemini-embedding-2")
GEMINI_TIMEOUT_SEC = float(os.getenv("GEMINI_TIMEOUT_SEC", "20"))
EMBED_DIM = int(os.getenv("GEMINI_EMBED_DIM", "768"))

_client = None


def available() -> bool:
    """API 키가 있으면 True (키가 없으면 각 서비스가 mock 으로 동작)"""
    return bool(GEMINI_API_KEY)


def get_client():
    global _client
    if _client is not None:
        return _client
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY 가 설정되지 않았습니다 (.env 확인)")
    from google import genai
    from google.genai import types

    _client = genai.Client(
        api_key=GEMINI_API_KEY,
        http_options=types.HttpOptions(timeout=int(GEMINI_TIMEOUT_SEC * 1000)),  # ms
    )
    return _client


def _strip_fence(text: str) -> str:
    """```json ... ``` 로 감싸 오는 경우 벗겨낸다"""
    text = (text or "").strip()
    m = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.S)
    return m.group(1) if m else text


def shrink_image(image_bytes: bytes, max_side: int = 768) -> bytes:
    """토큰·전송량을 줄이려고 긴 변을 max_side 로 줄이고 회전 정보를 반영한 JPEG 로 바꾼다"""
    from PIL import Image, ImageOps

    img = ImageOps.exif_transpose(Image.open(io.BytesIO(image_bytes))).convert("RGB")
    img.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def _is_rate_limited(e: Exception) -> bool:
    return getattr(e, "code", None) == 429 or "RESOURCE_EXHAUSTED" in str(e)


def _with_retry(fn, tries: int = 4, first_wait: float = 2.0):
    """무료 등급 분당 한도(429)에 걸리면 2→4→8초 쉬었다가 다시 시도 (문서 색인 만들 때 필요)"""
    wait = first_wait
    for i in range(tries):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            if i == tries - 1 or not _is_rate_limited(e):
                raise
            log.info("⏳ Gemini 한도 초과(429) → %.0f초 후 다시 시도", wait)
            time.sleep(wait)
            wait *= 2


def generate_json(prompt: str, image_bytes: bytes | None = None, system: str | None = None,
                  max_tokens: int = 600, temperature: float = 0.2,
                  history: list[tuple[str, str]] | None = None) -> dict:
    """프롬프트(+선택: 사진)를 보내고 JSON 으로 받는다. 파싱 실패 시 {} 반환

    history: 챗봇용 이전 대화 [("user"|"model", 문장), ...] — 주면 여러 턴 대화로 보낸다."""
    from google.genai import types

    if history:
        contents: list = [
            types.Content(role="model" if r == "model" else "user", parts=[types.Part.from_text(text=t)])
            for r, t in history if t
        ]
        parts = [types.Part.from_text(text=prompt)]
        if image_bytes:
            parts.append(types.Part.from_bytes(data=shrink_image(image_bytes), mime_type="image/jpeg"))
        contents.append(types.Content(role="user", parts=parts))
    else:
        contents = [prompt]
        if image_bytes:
            contents.append(types.Part.from_bytes(data=shrink_image(image_bytes), mime_type="image/jpeg"))

    response = get_client().models.generate_content(
        model=GEMINI_MODEL,
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            max_output_tokens=max_tokens,
            temperature=temperature,
        ),
    )
    text = getattr(response, "text", None) or ""
    try:
        data = json.loads(_strip_fence(text))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, TypeError) as e:
        log.warning("Gemini JSON 파싱 실패: %s / 받은 내용: %s", e, text[:200])
        return {}


def _l2_normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def embed(texts: list[str], kind: str = "document", titles: list[str] | None = None) -> list[list[float]]:
    """문장들을 벡터로 바꾼다. kind = 'query'(질문) | 'document'(지식 문서 조각)

    gemini-embedding-2 는 task_type 대신 문장 앞에 작업 표시를 붙이고,
    gemini-embedding-001 은 task_type 설정을 쓴다 (둘 다 지원)."""
    from google.genai import types

    if not texts:
        return []
    legacy = "embedding-001" in GEMINI_EMBED_MODEL
    if legacy:
        inputs = list(texts)
        config = types.EmbedContentConfig(
            task_type="RETRIEVAL_QUERY" if kind == "query" else "RETRIEVAL_DOCUMENT",
            output_dimensionality=EMBED_DIM,
        )
    else:
        if kind == "query":
            inputs = [f"task: search result | query: {t}" for t in texts]
        else:
            titles = titles or ["none"] * len(texts)
            inputs = [f"title: {ti or 'none'} | text: {t}" for ti, t in zip(titles, texts)]
        config = types.EmbedContentConfig(output_dimensionality=EMBED_DIM)

    vectors: list[list[float]] = []
    for start in range(0, len(inputs), 50):          # 한 번에 너무 많이 보내지 않게 50개씩
        batch = inputs[start:start + 50]
        result = _with_retry(lambda: get_client().models.embed_content(
            model=GEMINI_EMBED_MODEL, contents=batch, config=config,
        ))
        vectors.extend(_l2_normalize(list(e.values)) for e in result.embeddings)
    return vectors


def status() -> dict:
    return {
        "gemini_key_set": available(),
        "model": GEMINI_MODEL,
        "embed_model": GEMINI_EMBED_MODEL,
    }
