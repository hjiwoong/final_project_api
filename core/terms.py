from fastapi import HTTPException
from pydantic import BaseModel

from models.models import TermType


TERMS = (
    {"type": TermType.COOKIE, "title": "쿠키 사용", "version": "2026-11-18", "required": True},
    {"type": TermType.TERMS, "title": "이용약관", "version": "2026-11-18", "required": True},
    {"type": TermType.PRIVACY, "title": "개인정보 처리방침", "version": "2026-11-18", "required": True},
)


class AgreementInput(BaseModel):
    term_type: str
    term_version: str


def validate_agreements(agreements: list[AgreementInput]) -> None:
    expected = {(term["type"].value, term["version"]) for term in TERMS}
    received = {(item.term_type, item.term_version) for item in agreements}
    if len(agreements) != len(expected) or received != expected:
        raise HTTPException(422, "필수 약관에 모두 동의해야 가입할 수 있습니다")
