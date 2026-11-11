from datetime import datetime, timedelta

from database.db_connection import SessionLocal
from models.models import (
    ChildRequest, Guardian, Notification, PushToken, SocialAccount, User, UserRole,
)

ACCOUNT_DELETION_GRACE_DAYS = 30


def delete_expired_accounts() -> int:
    """Permanently remove parent accounts whose deletion grace period has expired."""
    cutoff = datetime.now() - timedelta(days=ACCOUNT_DELETION_GRACE_DAYS)
    with SessionLocal() as db:
        user_ids = [
            user_id
            for (user_id,) in db.query(User.id)
            .filter(
                User.role == UserRole.PARENT,
                User.deletion_requested_at <= cutoff,
            )
            .with_for_update()
            .all()
        ]
        if not user_ids:
            return 0

        db.query(Notification).filter(Notification.user_id.in_(user_ids)).delete(
            synchronize_session=False
        )
        db.query(PushToken).filter(PushToken.user_id.in_(user_ids)).delete(
            synchronize_session=False
        )
        db.query(ChildRequest).filter(ChildRequest.user_id.in_(user_ids)).delete(
            synchronize_session=False
        )
        db.query(SocialAccount).filter(SocialAccount.user_id.in_(user_ids)).delete(
            synchronize_session=False
        )
        db.query(Guardian).filter(Guardian.user_id.in_(user_ids)).delete(
            synchronize_session=False
        )
        deleted = (
            db.query(User)
            .filter(User.id.in_(user_ids), User.role == UserRole.PARENT)
            .delete(synchronize_session=False)
        )
        db.commit()
        return deleted
