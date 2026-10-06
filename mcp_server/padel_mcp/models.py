"""Strict structured-output contracts advertised by the padel.how MCP tools."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from typing import Annotated


Number = int | float
KnownScalar = str | int | float | bool


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class KnownValue(ContractModel):
    value: KnownScalar | None
    known: bool


class OfferPrice(ContractModel):
    amount: Annotated[float, Field(strict=True, gt=0, allow_inf_nan=False)]
    currency: str
    observed_at: str
    region: str | None
    availability: str | None
    freshness: Literal["current", "last_known"]


class AffiliateOffer(ContractModel):
    state: Literal["verified_price", "last_known_price", "link_only"]
    retailer: Literal["Amazon", "Padel Market", "PadelDogs", "Tennis Point", "Racketshop.com", "SPORTLET"]
    store: str | None = None
    url: str
    price: OfferPrice | None
    disclosure: str


class RacketRecord(ContractModel):
    slug: str
    name: str | None
    brand: str | None
    year: int | None
    score: Number | None
    review_url: str | None
    specifications: dict[str, KnownValue]
    scores: dict[str, Number]
    editorial_player_level_tag: Literal["beginner", "intermediate", "advanced"] | None
    audience_tags: list[Literal["women", "junior"]]
    offer: AffiliateOffer | None = None
    offers: list[AffiliateOffer] = []


class DatasetMetadata(ContractModel):
    source_endpoint: str
    source_generated: str | None
    record_count: int
    dataset_version: str
    methodology_url: str
    offer_source_generated: str | None = None
    offer_record_count: int = 0


class LocatorCitation(ContractModel):
    url: str
    label: str
    type: Literal["locator"] = "locator"


class SearchResult(ContractModel):
    items: list[RacketRecord]
    total_matches: int
    limit: int
    offset: int
    sort: Literal["score_desc", "name_asc"]
    has_more: bool
    applied_filters: dict[str, str | int | float]


class NumericDeltas(ContractModel):
    overall_score: list[Number]
    score_categories: dict[str, list[Number]]


class CompareResult(ContractModel):
    items: list[RacketRecord]
    numeric_deltas: NumericDeltas


class BrandGroup(ContractModel):
    normalized: str
    display_names: list[str]
    racket_count: int


class BrandListResult(ContractModel):
    brands: list[BrandGroup]


class OfferEntry(ContractModel):
    slug: str
    name: str | None
    review_url: str | None
    offers: list[AffiliateOffer]


class OfferListResult(ContractModel):
    items: list[OfferEntry]
    total_matches: int
    offer_count: int
    limit: int
    offset: int
    has_more: bool
    applied_filters: dict[str, str]


class SearchRacketsEnvelope(ContractModel):
    schema_version: Literal["1.2.0"]
    dataset: DatasetMetadata
    tool: Literal["search_rackets"]
    result: SearchResult
    citations: list[LocatorCitation]
    warnings: list[str]


class GetRacketEnvelope(ContractModel):
    schema_version: Literal["1.2.0"]
    dataset: DatasetMetadata
    tool: Literal["get_racket"]
    result: RacketRecord
    citations: list[LocatorCitation]
    warnings: list[str]


class CompareRacketsEnvelope(ContractModel):
    schema_version: Literal["1.2.0"]
    dataset: DatasetMetadata
    tool: Literal["compare_rackets"]
    result: CompareResult
    citations: list[LocatorCitation]
    warnings: list[str]


class ListRacketBrandsEnvelope(ContractModel):
    schema_version: Literal["1.2.0"]
    dataset: DatasetMetadata
    tool: Literal["list_racket_brands"]
    result: BrandListResult
    citations: list[LocatorCitation]
    warnings: list[str]


class GetOffersEnvelope(ContractModel):
    schema_version: Literal["1.2.0"]
    dataset: DatasetMetadata
    tool: Literal["get_offers"]
    result: OfferListResult
    citations: list[LocatorCitation]
    warnings: list[str]
