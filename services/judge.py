"""미하차 판정 로직 (두 단계)

검출된 사람은 신뢰도에 따라 두 종류로 나뉜다.
  - 확실한 검출 : conf >= LOW_CONF_THRESHOLD (.env, 기본 0.5)
  - 약한 검출   : CONF_THRESHOLD <= conf < LOW_CONF_THRESHOLD   (예: 0.2 ~ 0.5)

종점 잔류 인원(remaining_count) 중
  확실한 잔류 1명 이상            -> MISSED      (긴급: 미하차 의심)
  약한 잔류만 있음                -> NEEDS_CHECK (확인 필요: 재촬영 또는 직접 확인)
  잔류 0 이지만 약한 검출이 있었음 -> NEEDS_CHECK (기사·선생님 제외 후 남은 게 애매함)
  그 외                           -> OK

exclude_count(기사·선생님)는 카메라 가까이 크게 찍혀 '확실한 검출'일 가능성이 높으므로
확실한 검출에서 먼저 뺀다고 가정한다.
  확실한 잔류 = max(remaining_count - low_conf, 0)

testset(64장) 기준 v3 모델:
  conf 0.2 한 가지로만 판정하면  빈 차 오경보 3장 (모두 긴급 경고)
  두 단계 판정                    빈 차 긴급 경고 0장, 대신 '확인 필요'로 표시
"""
from models.models import AlertLevel, TripResult


def judge(boarding_count: int, remaining_count: int, low_conf: int) -> dict:
    alighted = max(boarding_count - remaining_count, 0)

    sure = max(remaining_count - low_conf, 0)     # 확실한 잔류
    weak = remaining_count - sure                 # 약한 잔류

    if sure >= 1:
        result, level = TripResult.MISSED, AlertLevel.URGENT
        message = f"미하차 의심 {sure}명"
        if weak:
            message += f" (추가 확인 필요 {weak}명)"
    elif weak >= 1:
        result, level = TripResult.NEEDS_CHECK, AlertLevel.CHECK
        message = f"확인 필요: 신뢰도 낮은 검출 {weak}명 · 재촬영하거나 차량 내부를 직접 확인하세요"
    elif low_conf >= 1:
        result, level = TripResult.NEEDS_CHECK, AlertLevel.CHECK
        message = f"확인 필요: 신뢰도 낮은 검출 {low_conf}건 · 재촬영하거나 차량 내부를 직접 확인하세요"
    else:
        result, level, message = TripResult.OK, None, "하차 확인 완료"

    return {
        "result": result,
        "level": level,
        "message": message,
        "alighted_count": alighted,
    }
