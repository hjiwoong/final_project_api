from datetime import date, datetime
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")


def now_kst_naive() -> datetime:
    """Return KST wall time for storage alongside naive database datetimes."""
    # DB의 naive datetime은 KST 벽시각으로 간주한다.
    return datetime.now(KST).replace(tzinfo=None)


def today_kst() -> date:
    """한국 날짜 기준 '오늘' — 서버 PC 시간대가 UTC 여도 결석(출결) 날짜가 하루 어긋나지 않게"""
    return datetime.now(KST).date()
