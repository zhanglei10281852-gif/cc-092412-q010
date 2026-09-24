from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

SUPPORTED_PARAMS = {"default_village"}


class ImportSubmitRequest(BaseModel):
    source_name: str = Field(min_length=1, max_length=200)
    file_format: Literal["csv", "json"]
    file_content: str = Field(min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)
    batch_key: str | None = Field(default=None, min_length=3, max_length=100)

    @field_validator("params")
    @classmethod
    def validate_params(cls, value: dict[str, Any]) -> dict[str, Any]:
        unknown = sorted(set(value) - SUPPORTED_PARAMS)
        if unknown:
            raise ValueError(f"不支持的导入参数：{', '.join(unknown)}")
        default_village = value.get("default_village")
        if default_village is not None and not isinstance(default_village, str):
            raise ValueError("default_village 参数必须是字符串")
        return value

    @field_validator("batch_key")
    @classmethod
    def validate_batch_key(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
        if any(character not in allowed for character in normalized):
            raise ValueError("batch_key 只能包含字母、数字及 . _ -")
        return normalized


class RowResolutionRequest(BaseModel):
    resolution: Literal["insert", "update", "skip"]
