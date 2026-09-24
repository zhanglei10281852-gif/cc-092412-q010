from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class ImportBatchSubmit(BaseModel):
    """居民导入批次提交：content_csv 与 rows 二选一，参数参与批次指纹计算。"""

    source_key: str = Field(min_length=1, max_length=100)
    file_name: str = Field(default="", max_length=200)
    params: dict[str, Any] = Field(default_factory=dict)
    content_csv: str | None = Field(default=None, max_length=2_000_000)
    rows: list[dict[str, Any]] | None = None

    @field_validator("source_key")
    @classmethod
    def normalize_source_key(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("source_key 不能为空")
        return normalized

    @model_validator(mode="after")
    def exactly_one_content_source(self):
        if (self.content_csv is None) == (self.rows is None):
            raise ValueError("content_csv 与 rows 必须且只能提供其一")
        return self


class ImportDecisionItem(BaseModel):
    row_number: int = Field(ge=1)
    decision: Literal["overwrite", "skip"]


class ImportDecisionsRequest(BaseModel):
    decisions: list[ImportDecisionItem] = Field(min_length=1, max_length=2000)
