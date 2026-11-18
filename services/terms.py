from sqlalchemy.orm import Session

from core import terms
from models.models import TermsAgreement, User, UserRole


def get_pending_terms(db: Session, user: User) -> list[dict[str, str]]:
    if user.role != UserRole.PARENT:
        return []
    agreed = {
        (term_type.value, version)
        for term_type, version in (
            db.query(TermsAgreement.type, TermsAgreement.version)
            .filter(TermsAgreement.user_id == user.id)
            .all()
        )
    }
    return [
        {"term_type": term["type"].value, "term_version": term["version"], "title": term["title"]}
        for term in terms.TERMS
        if term["required"] and (term["type"].value, term["version"]) not in agreed
    ]
