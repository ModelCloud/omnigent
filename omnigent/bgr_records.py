"""Shared strict BGR wire primitives, independent of ledger and supervisor records."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Identifier = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
Label = Annotated[str, Field(min_length=1, max_length=256)]
Role = Literal["brain", "grunt", "reviewer"]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    @field_validator("protocol_version", mode="before", check_fields=False)
    @classmethod
    def strict_protocol_version(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            raise ValueError("protocol_version must be integer 1")
        return value
