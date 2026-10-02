"""Validated, extensible bank column mappings."""

import codecs
import os
import re
from pathlib import Path
from string import Formatter
from typing import Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

Kind = Literal["card", "transfer", "fee", "topup", "exchange", "refund", "other"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Detect(StrictModel):
    required_headers: list[str] = Field(min_length=1)


class Csv(StrictModel):
    delimiter: str = Field(default=",", min_length=1, max_length=1)
    encoding: str = "utf-8-sig"
    decimal: Literal[".", ","] = "."
    thousands: str = Field(default="", max_length=1)
    skip_rows: int = Field(default=0, ge=0)
    skip_footer_matching: str | None = None

    @field_validator("encoding")
    @classmethod
    def valid_encoding(cls, value: str) -> str:
        try:
            codecs.lookup(value)
        except LookupError:
            raise ValueError("unknown encoding") from None
        return value

    @field_validator("skip_footer_matching")
    @classmethod
    def valid_footer(cls, value: str | None) -> str | None:
        if value:
            try:
                re.compile(value)
            except re.error:
                raise ValueError("invalid footer regex") from None
        return value

    @model_validator(mode="after")
    def valid(self) -> Self:
        if self.thousands == self.decimal:
            raise ValueError("thousands must differ from decimal")
        return self


class Timestamp(StrictModel):
    column: str
    fallback_column: str | None = None
    format: str


class Amount(StrictModel):
    column: str | None = None
    debit: str | None = None
    credit: str | None = None
    fee: str | None = None
    sign: Literal["invert"] | None = None

    @model_validator(mode="after")
    def valid(self) -> Self:
        if not (
            (self.column and not self.debit and not self.credit)
            or (not self.column and self.debit and self.credit)
        ):
            raise ValueError("specify column OR debit and credit")
        return self


class Currency(StrictModel):
    column: str | None = None
    fixed: str | None = None

    @model_validator(mode="after")
    def valid(self) -> Self:
        if bool(self.column) == bool(self.fixed):
            raise ValueError("specify column OR fixed")
        return self


class Counterparty(StrictModel):
    column: str | None = None
    regex: str | None = None

    @field_validator("regex")
    @classmethod
    def valid_regex(cls, value: str | None) -> str | None:
        if value:
            try:
                compiled = re.compile(value)
            except re.error:
                raise ValueError("invalid counterparty regex") from None
            if "name" not in compiled.groupindex:
                raise ValueError("regex needs named group name")
        return value

    @model_validator(mode="after")
    def valid(self) -> Self:
        if bool(self.column) == bool(self.regex):
            raise ValueError("specify column OR regex")
        return self


class KindMap(StrictModel):
    column: str | None = None
    map: dict[str, Kind] = Field(default_factory=dict)
    default: Kind = "other"


class Include(StrictModel):
    column: str
    allowed: list[str] = Field(min_length=1)


class Mapping(StrictModel):
    id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    detect: Detect
    csv: Csv = Field(default_factory=Csv)
    ts: Timestamp
    amount: Amount
    currency: Currency
    description: str
    balance: str | None = None
    account: str = Field(min_length=1)
    counterparty: Counterparty | None = None
    kind: KindMap = Field(default_factory=KindMap)
    include: Include | None = None
    category: str | None = None

    @field_validator("account")
    @classmethod
    def valid_account(cls, value: str) -> str:
        for _, field, spec, conversion in Formatter().parse(value):
            if field is not None and (
                not field or spec or conversion or "." in field or "[" in field
            ):
                raise ValueError("account requires simple {column} placeholders")
        return value


class MappingError(ValueError):
    """A mapping file or export does not satisfy the declared shape."""


def load_mapping(path: Path) -> Mapping:
    try:
        return Mapping.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except ValidationError as error:
        fields = ", ".join(".".join(str(p) for p in e["loc"]) or "mapping" for e in error.errors())
        raise MappingError(f"{path}: invalid fields: {fields}") from None
    except (ValueError, LookupError, re.error, yaml.YAMLError) as error:
        raise MappingError(f"{path}: invalid mapping ({type(error).__name__})") from None


def load_mappings() -> list[Mapping]:
    builtins = Path(__file__).parent / "mappings"
    user = Path(os.environ.get("SHERD_HOME", str(Path.home() / ".sherd"))) / "bank_mappings"
    mappings: list[Mapping] = []
    ids: set[str] = set()
    for root in (builtins, user):
        for path in sorted(root.glob("*.yaml")):
            mapping = load_mapping(path)
            if mapping.id in ids:
                raise MappingError(f"{path}: invalid field id (duplicate mapping id)")
            ids.add(mapping.id)
            mappings.append(mapping)
    return mappings
