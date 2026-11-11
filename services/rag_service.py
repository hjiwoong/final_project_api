"""
============================================================
services/rag_service.py — 하차 확인 의무·대응 수칙 RAG  (AI ③)
============================================================
🟥 [AI] road_hazard/services/rag_service.py 를 미하차 대응용으로 옮긴 것
        (구조 그대로: USE_REAL_AI 스위치 / mock 검색 / 진짜 검색 / _normalize / 실패 시 폴백)

바뀐 점
   - 지식 문서: 도로 규정 → knowledge/ 폴더의 하차 확인 의무·대응 수칙·서비스 안내 (.md)
   - 임베딩: OpenAIEmbeddings → Gemini 임베딩 (무료)
   - 벡터DB: Chroma(langchain) → numpy 없이 파이썬 리스트 + rag_index.json 캐시
     (조각별 캐시 → 문서를 추가·수정하면 바뀐 조각만 새로 임베딩)
   - 지식 문서: .md / .txt / .pdf (하위 폴더 포함)
     · 법령 PDF(국가법령정보센터에서 저장)는 '제○조' 단위로 자르고 출처에 쪽 번호를 단다
     · 매뉴얼 PDF 는 쪽 단위 · .hwp 는 PDF 로 저장해서 넣을 것 · 스캔 PDF 는 OCR 필요
   - mock 검색도 하드코딩 문서가 아니라 같은 knowledge/ 문서를 글자 조각 매칭으로 검색
     → API 키가 없어도 같은 문서로 동작

📤 검색 결과 형식 (road_hazard 와 같음)
   [{"title": "도로교통법 제53조 제4항", "source": "01_법령_하차확인.md · 국가법령정보센터", "content": "..."}]

📤 질문 답변 형식 (POST /rag/ask)
   {"answer": "...[1]...", "sources": [...], "mode": "real" | "mock"}

.env
   RAG_ENABLED=true     # false 면 글자 매칭 검색 + 문서 발췌 답변 (기본: GEMINI_API_KEY 가 있으면 true)
============================================================
"""
import hashlib
import json
import logging
import os
import re
from pathlib import Path

from dotenv import load_dotenv

from services import gemini_client

load_dotenv()
log = logging.getLogger("rag")

BASE_DIR = Path(__file__).resolve().parent.parent
KNOWLEDGE_DIR = BASE_DIR / os.getenv("RAG_KNOWLEDGE_DIR", "knowledge")
INDEX_FILE = BASE_DIR / os.getenv("RAG_INDEX_FILE", "rag_index.json")

USE_REAL_AI = os.getenv("RAG_ENABLED", "true").lower() == "true" and gemini_client.available()
FALLBACK_ON_ERROR = True

CHUNK_CHARS = 450          # .md 조각 크기
PDF_CHUNK_CHARS = 700      # PDF(법령·매뉴얼) 조각 크기 — 조문 하나가 되도록 조금 크게
CONTENT_PREVIEW = 220
NOT_FOUND = "등록된 수칙·안내 문서에서 관련 내용을 찾지 못했습니다. 관리자(학교·기관)에게 문의해 주세요."

# 법령 PDF 의 조문 머리: "제53조(어린이통학버스 운전자 및 운영자 등의 의무)", "제53조의3(…)"
_ARTICLE_RE = re.compile(r"^\s*(제\s*\d+\s*조(?:\s*의\s*\d+)?)\s*(\([^)]{1,60}\))?")


# ---------- 문서 읽기 · 조각내기 ----------
def _split_text(text: str, limit: int, head: str = "") -> list[str]:
    """문단(빈 줄) → 줄 단위로 limit 근처에서 자른다. head 는 이어지는 조각 앞에 붙여 문맥 유지"""
    out, buf = [], ""
    units = [u for u in re.split(r"\n\s*\n", text) if u.strip()]
    pieces: list[str] = []
    for u in units:                       # 문단 하나가 너무 길면 줄 단위로 더 쪼갠다
        if len(u) <= limit:
            pieces.append(u)
        else:
            pieces.extend(line for line in u.splitlines() if line.strip())
    for piece in pieces:
        if buf and len(buf) + len(piece) > limit:
            out.append(buf.strip())
            buf = (head + "\n") if head else ""
        buf += piece + "\n\n"
    if buf.strip() and buf.strip() != head:
        out.append(buf.strip())
    return out


def _md_chunks(path: Path) -> list[dict]:
    """.md/.txt — 첫 줄 '# 제목', (선택) '출처: ...' 줄, '## 소제목' 단위"""
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    title, origin = path.stem, ""
    sections: list[tuple[str, list[str]]] = [("", [])]
    for line in text.splitlines():
        if line.startswith("# "):
            title = line[2:].strip()
        elif line.startswith("출처:"):
            origin = line[3:].strip()
        elif line.startswith("## "):
            sections.append((line[3:].strip(), []))
        else:
            sections[-1][1].append(line)

    source = f"{path.name}" + (f" · {origin}" if origin else "")
    chunks = []
    for section, lines in sections:
        body = "\n".join(lines).strip()
        for piece in (_split_text(body, CHUNK_CHARS) if body else []):
            chunks.append({"title": title, "section": section, "source": source, "text": piece})
    return chunks


def _pdf_pages(path: Path) -> list[tuple[int, str]]:
    """PDF → [(쪽 번호, 글자)] — 매 쪽 반복되는 머리말·꼬리말(예: '법제처 3 국가법령정보센터')은 뺀다"""
    from pypdf import PdfReader

    # 일부 PDF 는 공백을 제어문자(\x01 등)로 내보낸다 → 공백으로 바꿔 단어가 붙지 않게 한다
    pages = [(i + 1, re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", pg.extract_text() or ""))
             for i, pg in enumerate(PdfReader(str(path)).pages)]
    if len(pages) >= 2:
        def key(line: str) -> str:
            return re.sub(r"\d+", "#", line.strip())

        def edges(t: str) -> list[str]:          # 머리말·꼬리말 후보: 각 쪽의 처음·마지막 2줄
            ls = [line for line in t.splitlines() if line.strip()]
            return ls[:2] + ls[-2:]
        counts: dict[str, int] = {}
        for _, t in pages:
            for k in {key(line) for line in edges(t)}:
                counts[k] = counts.get(k, 0) + 1
        need = max(2, 0.6 * len(pages))
        repeated = {k for k, c in counts.items() if c >= need and len(k) < 60}
        out = []
        for n, t in pages:
            edge = {key(line) for line in edges(t)}
            out.append((n, "\n".join(line for line in t.splitlines()
                                      if not (key(line) in repeated and key(line) in edge))))
        pages = out
    return pages


def _unwrap(text: str) -> str:
    """PDF 줄바꿈(쪽 폭 때문에 생긴 것)을 공백으로 — 항(①②…)·호(1. 2.)·가목 앞의 줄바꿈은 살린다"""
    return re.sub(r"\n(?![ \t]*(?:[①-⑳❑❍□○■●◎※]|\d+\.\s|[가-하]\.\s|\n))", " ", text)


# 매뉴얼(한글 문서) 제목 기호: 큰 제목 ❑ □ ■, 작은 제목 ❍ ○ ◎
_MAJOR_RE = re.compile(r"^\s*[❑□■]\s*(.+)")
_MINOR_RE = re.compile(r"^\s*[❍○◎]\s*(.+)")


def _manual_sections(pages: list[tuple[int, str]]) -> list[tuple[str, int, str]]:
    """❑ 큰 제목 / ❍ 작은 제목 단위로 자른다 → [('운전자 매뉴얼 · 하차 시', 쪽, 본문)]
    큰 제목은 다음 쪽까지 이어진다. 제목 기호가 없으면 [] (쪽 단위로 처리)"""
    if not any(_MAJOR_RE.match(l) or _MINOR_RE.match(l) for _, t in pages for l in t.splitlines()):
        return []
    out: list[tuple[str, int, list[str]]] = []
    major = minor = ""
    for n, t in pages:
        for line in t.splitlines():
            if not line.strip():
                continue
            m1, m2 = _MAJOR_RE.match(line), _MINOR_RE.match(line)
            if m1:
                major, minor = m1.group(1).strip(), ""
                out.append((major, n, []))
                continue
            if m2:
                minor = m2.group(1).strip()
                out.append((f"{major} · {minor}" if major else minor, n, []))
                continue
            if not out:
                out.append(("머리말", n, []))
            out[-1][2].append(line)
    return [(h, n, "\n".join(b).strip()) for h, n, b in out if len(re.sub(r"\s", "", "\n".join(b))) >= 10]


def _pdf_chunks(path: Path) -> list[dict]:
    """PDF — 법령이면 조문(제○조) 단위, 아니면 쪽 단위로 자르고 출처에 쪽 번호를 단다"""
    pages = _pdf_pages(path)
    total = sum(len(t.strip()) for _, t in pages)
    if total < 50:
        log.warning("⚠️ [RAG] %s: 글자를 읽을 수 없습니다 (스캔 이미지 PDF 는 OCR 후 넣어 주세요)", path.name)
        return []

    title = path.stem.replace("_", " ")
    lines = [(n, line) for n, t in pages for line in t.splitlines() if line.strip()]
    is_law = sum(1 for _, line in lines if _ARTICLE_RE.match(line) and "(" in line) >= 3

    chunks = []
    if is_law:
        sections: list[tuple[str, int, list[str]]] = [("", lines[0][0] if lines else 1, [])]
        for n, line in lines:
            m = _ARTICLE_RE.match(line)
            if m and m.group(2):
                head = re.sub(r"\s+", "", m.group(1)) + m.group(2)
                sections.append((head, n, []))
            sections[-1][2].append(line)
        for head, page, body in sections:
            text = _unwrap("\n".join(body).strip())
            for piece in (_split_text(text, PDF_CHUNK_CHARS, head) if text else []):
                chunks.append({"title": title, "section": head or "총칙·머리말",
                               "source": f"{path.name} p.{page}", "text": piece})
    elif sections := _manual_sections(pages):
        for head, n, body in sections:
            for piece in _split_text(_unwrap(body), PDF_CHUNK_CHARS, f"[{head}]"):
                if not piece.startswith(f"[{head}]"):
                    piece = f"[{head}]\n{piece}"
                chunks.append({"title": title, "section": head, "source": f"{path.name} p.{n}",
                               "text": piece})
    else:
        for n, t in pages:
            if len(re.sub(r"\s", "", t)) < 30:     # 그림·스티커만 있는 쪽은 건너뛴다
                continue
            for piece in _split_text(_unwrap(t.strip()), PDF_CHUNK_CHARS):
                chunks.append({"title": title, "section": f"{n}쪽", "source": f"{path.name} p.{n}",
                               "text": piece})
    return chunks


_file_cache: dict[str, tuple[tuple, list[dict]]] = {}


def _load_chunks() -> list[dict]:
    """knowledge/ 아래(하위 폴더 포함) .md · .txt · .pdf → [{title, section, source, text}]
    파일이 바뀌지 않았으면 다시 읽지 않는다 (PDF 읽기는 느리므로)"""
    if not KNOWLEDGE_DIR.exists():
        log.warning("⚠️ [RAG] '%s/' 폴더가 없습니다.", KNOWLEDGE_DIR.name)
        return []

    chunks: list[dict] = []
    for path in sorted(KNOWLEDGE_DIR.rglob("*")):
        ext = path.suffix.lower()
        if not path.is_file() or path.name.startswith((".", "~", "_")) or path.stem.upper() == "README":
            continue
        if ext in (".hwp", ".hwpx", ".docx"):
            log.warning("⚠️ [RAG] %s: 이 형식은 읽지 않습니다. PDF 로 저장해서 넣어 주세요.", path.name)
            continue
        if ext not in (".md", ".txt", ".pdf"):
            continue
        st = path.stat()
        sig = (st.st_mtime_ns, st.st_size)
        cached = _file_cache.get(str(path))
        if cached and cached[0] == sig:
            chunks.extend(cached[1])
            continue
        try:
            got = _pdf_chunks(path) if ext == ".pdf" else _md_chunks(path)
        except Exception as e:  # noqa: BLE001  (깨진 PDF 하나 때문에 전체가 멈추지 않게)
            log.warning("⚠️ [RAG] %s 읽기 실패: %s: %s", path.name, type(e).__name__, e)
            got = []
        _file_cache[str(path)] = (sig, got)
        chunks.extend(got)
    return chunks


def _display_title(c: dict) -> str:
    return f"{c['title']} — {c['section']}" if c.get("section") else c["title"]


def _normalize(docs) -> list[dict]:
    out = []
    for d in docs or []:
        if not isinstance(d, dict):
            continue
        content = str(d.get("content") or d.get("text") or "")
        out.append({
            "title": str(d.get("title") or "관련 수칙"),
            "source": str(d.get("source") or ""),
            "content": content[:CONTENT_PREVIEW] + ("..." if len(content) > CONTENT_PREVIEW else ""),
        })
    return out


def _as_result(c: dict, score: float | None = None) -> dict:
    r = {"title": _display_title(c), "source": c["source"], "content": c["text"]}
    if score is not None:
        r["score"] = round(score, 3)
    return r


# ---------- mock 검색: 글자 조각(2~4글자) 매칭 — road_hazard 와 같은 방식 ----------
def _tokenize(text: str) -> list[str]:
    out = []
    for size in (4, 3, 2):
        for i in range(len(text) - size + 1):
            out.append(text[i:i + size])
    return out


def _mock_search(query: str, k: int) -> list[dict]:
    q = re.sub(r"\s+", "", (query or "").lower())
    tokens = set(_tokenize(q))
    scored = []
    for c in _load_chunks():
        hay = re.sub(r"\s+", "", (_display_title(c) + c["text"]).lower())
        score = sum(len(t) for t in tokens if t in hay)
        if score > 0:
            scored.append((score, c))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [_as_result(c) for _, c in scored[:k]]


# ---------- 진짜 검색: Gemini 임베딩 + 코사인 유사도 ----------
# rag_index.json = {"model": ..., "dim": ..., "vecs": {조각 해시: 벡터}}
# 조각별로 저장하므로 PDF 를 하나 추가해도 새 조각만 임베딩한다 (무료 한도 절약)
_index: dict | None = None


def _chunk_key(c: dict) -> str:
    raw = json.dumps([_display_title(c), c["text"]], ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _load_index_file() -> dict:
    empty = {"model": gemini_client.GEMINI_EMBED_MODEL, "dim": gemini_client.EMBED_DIM, "vecs": {}}
    if not INDEX_FILE.exists():
        return empty
    try:
        data = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return empty
    if data.get("model") != empty["model"] or data.get("dim") != empty["dim"] or "vecs" not in data:
        return empty                       # 모델·차원이 바뀌면 처음부터
    return data


def _get_index() -> list[tuple[dict, list[float]]]:
    global _index
    chunks = _load_chunks()
    if not chunks:
        raise RuntimeError(f"'{KNOWLEDGE_DIR.name}/' 에 문서(.md/.txt/.pdf)가 없습니다.")
    if _index is None:
        _index = _load_index_file()
    vecs = _index["vecs"]
    keys = [_chunk_key(c) for c in chunks]
    missing = [(k, c) for k, c in zip(keys, chunks) if k not in vecs]
    if missing:
        log.info("📚 [RAG] 새 문서 조각 %d개를 임베딩합니다 (전체 %d개)...", len(missing), len(chunks))
        new_vecs = gemini_client.embed(
            [c["text"] for _, c in missing], kind="document",
            titles=[_display_title(c) for _, c in missing],
        )
        for (k, _), v in zip(missing, new_vecs):
            vecs[k] = v
        live = set(keys)                     # 지운 문서의 벡터는 정리
        _index["vecs"] = {k: v for k, v in vecs.items() if k in live}
        try:
            INDEX_FILE.write_text(json.dumps(_index, ensure_ascii=False), encoding="utf-8")
        except OSError:
            log.warning("RAG 인덱스 캐시 저장 실패 (메모리에서만 사용)")
    return [(c, _index["vecs"][k]) for k, c in zip(keys, chunks)]


def _real_search(query: str, k: int) -> list[dict]:
    index = _get_index()
    qv = gemini_client.embed([query], kind="query")[0]
    scored = sorted(
        ((sum(a * b for a, b in zip(qv, v)), c) for c, v in index),
        key=lambda x: x[0], reverse=True,
    )
    return [_as_result(c, s) for s, c in scored[:k]]


def search(query: str, k: int = 3) -> tuple[list[dict], str]:
    """검색 결과(본문 전체)와 모드("real" | "mock" | "mock-fallback") — 답변·챗봇에서 LLM 에 넘길 때 사용"""
    if not USE_REAL_AI:
        return _mock_search(query, k), "mock"
    try:
        return _real_search(query, k), "real"
    except Exception as e:  # noqa: BLE001
        log.warning("❌ [RAG] 검색 실패: %s: %s", type(e).__name__, e)
        if not FALLBACK_ON_ERROR:
            raise
        log.info("↩️  [RAG] 글자 매칭 검색으로 대체합니다")
        return _mock_search(query, k), "mock-fallback"


def search_by_text(query: str, k: int = 3) -> list[dict]:
    """질문 문장으로 수칙 검색 (agent_service 도 사용) — 응답용으로 본문을 줄인 형식"""
    return _normalize(search(query, k)[0])


def sources_of(docs: list[dict]) -> list[dict]:
    """API 응답에 싣는 출처 목록 (본문은 앞부분만)"""
    return _normalize(docs)


# ---------- 질문 답변 ----------
def guide_question(level: str | None) -> str:
    """경고 상세 화면의 '대응 수칙' 카드에서 질문 없이 부를 때 쓰는 기본 질문"""
    if level == "URGENT":
        return "미하차 의심 긴급 경고가 발생했을 때 기사와 관리자, 보호자는 무엇을 해야 하나요?"
    if level == "CHECK":
        return "확인 필요 경고가 발생했을 때 어떻게 처리해야 하나요?"
    return "운행을 마친 뒤 어린이 하차 확인은 어떻게 해야 하나요?"


def _mock_answer(question: str, docs: list[dict]) -> str:
    if not docs:
        return NOT_FOUND
    lines = ["관련 수칙을 찾았습니다.\n"]
    for i, d in enumerate(docs, 1):
        lines.append(f"[{i}] {d['title']}\n{d['content']}\n")
    lines.append("※ 위 문서 발췌입니다. 자세한 내용은 출처 문서를 확인해 주세요.")
    return "\n".join(lines)


_SYSTEM = """당신은 어린이 통학버스 미하차 예방 서비스의 안내 AI입니다.
반드시 아래 [문서]에 있는 내용만 근거로 답하세요. 문서에 없으면 지어내지 말고
"등록된 수칙에서 찾지 못했습니다. 관리자(학교·기관)에게 문의해 주세요."라고 답하세요.
법 조항은 문서에 적힌 그대로만 인용하고, 문서에 없는 처벌·과태료 금액 등을 추측하지 마세요.
근거 문장 끝에 [1], [2]처럼 문서 번호를 붙이세요. 존댓말, 3~6문장."""


def _real_answer(question: str, docs: list[dict], context: str | None) -> str:
    doc_txt = "\n\n".join(f"[{i}] {d['title']} ({d['source']})\n{d['content']}" for i, d in enumerate(docs, 1))
    prompt = f"""[문서]
{doc_txt}
{f'[현재 경고 상황]{chr(10)}{context}' if context else ''}

[질문] {question}

JSON 으로만: {{"answer": "..."}}"""
    data = gemini_client.generate_json(prompt, system=_SYSTEM, max_tokens=600)
    return str(data.get("answer") or "").strip()


def answer(question: str, context: str | None = None, k: int = 4) -> dict:
    """POST /rag/ask 본체 → {"answer", "sources", "mode"}"""
    query = question if not context else f"{question}\n{context}"
    docs, mode = search(query, k)
    if mode != "real":
        shown = _normalize(docs)
        return {"answer": _mock_answer(question, shown), "sources": shown, "mode": mode}
    try:
        text = _real_answer(question, docs, context) if docs else NOT_FOUND   # LLM 에는 본문 전체
        if not text:
            raise ValueError("빈 답변")
        return {"answer": text, "sources": _normalize(docs), "mode": "real"}
    except Exception as e:  # noqa: BLE001
        log.warning("❌ [RAG] 답변 실패: %s: %s", type(e).__name__, e)
        if not FALLBACK_ON_ERROR:
            raise
        shown = _normalize(docs)
        return {"answer": _mock_answer(question, shown), "sources": shown, "mode": "mock-fallback"}


def status() -> dict:
    chunks = _load_chunks()
    files = sorted({c["source"].split(" · ")[0].split(" p.")[0] for c in chunks})
    return {"rag_enabled": USE_REAL_AI, "knowledge_files": len(files),
            "knowledge_pdfs": sum(f.lower().endswith(".pdf") for f in files),
            "knowledge_chunks": len(chunks)}
