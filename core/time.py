from datetime import datetime
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")


def now_kst_naive() -> datetime:
    """Return KST wall time for storage alongside naive database datetimes."""
    # DB의 naive datetime은 KST 벽시각으로 간주한다.
    return datetime.now(KST).replace(tzinfo=None)
