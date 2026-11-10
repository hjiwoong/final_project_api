import os

from cryptography.fernet import Fernet, InvalidToken


def _fernet() -> Fernet:
    key = os.getenv("LOCATION_ENCRYPTION_KEY")
    if not key:
        raise ValueError("LOCATION_ENCRYPTION_KEY 환경변수를 설정해야 합니다.")
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise ValueError("LOCATION_ENCRYPTION_KEY가 유효한 Fernet 키가 아닙니다.") from exc


def validate_location_encryption_key() -> None:
    _fernet()


def encrypt_location_value(value: str) -> str:
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_location_value(value: str) -> str:
    try:
        return _fernet().decrypt(value.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError("위치 데이터 복호화에 실패했습니다. 암호화 키를 확인하세요.") from exc


def decrypt_coordinate_value(value: str) -> float:
    return float(decrypt_location_value(value))
