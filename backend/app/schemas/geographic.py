"""Validated geographic-filter configuration.

A closed, plain-data description of which events belong to a source's coverage
area, evaluated after normalization and before persistence. It contains no
code and no expression: only lists of place names, postal codes/prefixes,
address substrings, a radius, a bounding box, and configured aliases.

Deliberate limits (Phase 8G contract):

* No fuzzy matching by default (`fuzzy` exists but defaults off and this phase
  never turns it on) — a place matches only by an exact, case-insensitive,
  word-boundary comparison, optionally broadened by explicitly configured
  aliases.
* No geocoding — radius and bounding-box rules apply only when the event
  already carries coordinates; they never look an address up.
* Inclusion is decided from the event's OWN geography, never from the city it
  was assigned to (that assignment is not evidence the event is in the area).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

_MAX_LIST = 200
_MAX_TERM = 200

MissingGeographyAction = Literal["reject", "keep_with_warning", "needs_review"]


class RadiusRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    center_latitude: float = Field(ge=-90.0, le=90.0)
    center_longitude: float = Field(ge=-180.0, le=180.0)
    radius_km: float = Field(gt=0.0, le=20_000.0)


class BoundingBoxRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_latitude: float = Field(ge=-90.0, le=90.0)
    max_latitude: float = Field(ge=-90.0, le=90.0)
    min_longitude: float = Field(ge=-180.0, le=180.0)
    max_longitude: float = Field(ge=-180.0, le=180.0)

    @model_validator(mode="after")
    def _ordered(self) -> BoundingBoxRule:
        if self.min_latitude > self.max_latitude:
            raise ValueError("min_latitude must not exceed max_latitude")
        if self.min_longitude > self.max_longitude:
            raise ValueError("min_longitude must not exceed max_longitude")
        return self


class GeographicFilterConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # "any": include when at least one configured rule group matches.
    # "all": include only when every configured rule group matches.
    mode: Literal["any", "all"] = "any"

    localities: list[str] = Field(default_factory=list, max_length=_MAX_LIST)
    regions: list[str] = Field(default_factory=list, max_length=_MAX_LIST)
    countries: list[str] = Field(default_factory=list, max_length=_MAX_LIST)
    postal_codes: list[str] = Field(default_factory=list, max_length=_MAX_LIST)
    postal_code_prefixes: list[str] = Field(default_factory=list, max_length=_MAX_LIST)
    address_contains: list[str] = Field(default_factory=list, max_length=_MAX_LIST)

    radius: RadiusRule | None = None
    bounding_box: BoundingBoxRule | None = None

    # Canonical term -> accepted variant spellings. Broadens name matching
    # without enabling fuzzy comparison.
    aliases: dict[str, list[str]] = Field(default_factory=dict)

    fuzzy: bool = False
    missing_geography_action: MissingGeographyAction = "keep_with_warning"

    @model_validator(mode="after")
    def _bound_terms(self) -> GeographicFilterConfig:
        for group in (
            self.localities, self.regions, self.countries,
            self.postal_codes, self.postal_code_prefixes, self.address_contains,
        ):
            for term in group:
                if len(term) > _MAX_TERM:
                    raise ValueError("geographic term too long")
        if len(self.aliases) > _MAX_LIST:
            raise ValueError("too many alias entries")
        for variants in self.aliases.values():
            if len(variants) > _MAX_LIST:
                raise ValueError("too many alias variants")
        return self

    def has_any_rule(self) -> bool:
        return bool(
            self.localities or self.regions or self.countries
            or self.postal_codes or self.postal_code_prefixes
            or self.address_contains or self.radius or self.bounding_box
        )
