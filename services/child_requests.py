"""자녀 연결 요청 공통 도구 (회원가입 때와 앱에서 자녀를 추가할 때 같은 규칙을 쓴다)"""
from pydantic import BaseModel, Field, field_validator

from models.models import ChildRequest

MAX_CHILDREN_AT_SIGNUP = 5
MAX_PENDING_PER_USER = 5     # 승인 대기 요청이 이만큼 쌓여 있으면 더 올릴 수 없다 (관리자 목록 도배 방지)


class ChildInput(BaseModel):
    """학부모가 적는 자녀 정보. 학생 목록을 보여주지 않으므로(아동 정보 보호) 직접 입력한다."""
    name: str = Field(min_length=1, max_length=100)
    class_name: str | None = Field(default=None, max_length=50)
    relation: str | None = Field(default=None, max_length=30)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("자녀 이름을 입력하세요")
        return v

    @field_validator("class_name", "relation")
    @classmethod
    def _blank_to_none(cls, v: str | None) -> str | None:
        v = (v or "").strip()
        return v or None


def dedupe(children: list[ChildInput]) -> list[ChildInput]:
    seen, out = set(), []
    for c in children:
        key = (c.name.lower(), (c.class_name or "").lower())
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


def make_requests(user_id: int, children: list[ChildInput]) -> list[ChildRequest]:
    return [ChildRequest(user_id=user_id, child_name=c.name, class_name=c.class_name, relation=c.relation)
            for c in children]


def parent_request_dict(r: ChildRequest) -> dict:
    """학부모에게 보여줄 형태 (자기 요청만)"""
    return {
        "id": r.id,
        "child_name": r.child_name,
        "class_name": r.class_name,
        "relation": r.relation,
        "status": r.status.value,
        "reject_reason": r.reject_reason,
        "student": ({"id": r.student.id, "name": r.student.name, "class_name": r.student.class_name}
                    if r.student else None),
        "created_at": r.created_at,
        "decided_at": r.decided_at,
    }


def admin_request_dict(r: ChildRequest, candidates: list[dict] | None = None) -> dict:
    return {
        **parent_request_dict(r),
        "user": {"id": r.user.id, "name": r.user.name, "email": r.user.email},
        "candidates": candidates or [],
    }
