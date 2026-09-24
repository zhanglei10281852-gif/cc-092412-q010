from __future__ import annotations

from dataclasses import dataclass
from datetime import date

# GB 11643-1999 公民身份号码校验位权重与校验码表
_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_CHECK_CODES = "10X98765432"


@dataclass(frozen=True, slots=True)
class IdCardInfo:
    number: str
    birth_date: date
    gender: str  # "男" 或 "女"


def check_digit(first_seventeen: str) -> str:
    """计算 17 位本体的校验码，供校验与测试数据生成共用。"""
    total = sum(int(digit) * weight for digit, weight in zip(first_seventeen, _WEIGHTS))
    return _CHECK_CODES[total % 11]


def parse_id_card(value: str) -> tuple[IdCardInfo | None, str | None]:
    """校验 18 位身份证号。

    返回 (信息, None) 或 (None, 错误消息)。只接受 18 位大陆居民身份证号，
    校验位、出生日期合法性都会检查。
    """
    number = value.strip().upper()
    if len(number) != 18:
        return None, "身份证号必须为 18 位"
    if not number[:17].isdigit():
        return None, "身份证号前 17 位必须为数字"
    if number[17] not in "0123456789X":
        return None, "身份证号校验位只能是数字或 X"
    if check_digit(number[:17]) != number[17]:
        return None, "身份证号校验位不正确"
    try:
        birth = date(int(number[6:10]), int(number[10:12]), int(number[12:14]))
    except ValueError:
        return None, "身份证号中的出生日期无效"
    gender = "男" if int(number[16]) % 2 == 1 else "女"
    return IdCardInfo(number=number, birth_date=birth, gender=gender), None
