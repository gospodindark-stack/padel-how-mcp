"""Validated, reloadable access to the public racket catalogue."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from .bundle import load_bundle
from threading import RLock
from typing import Any
from urllib.parse import urlparse


SAFE_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
UNKNOWN_VALUES = {"", "not specified", "unknown", "n/a", "na", "none", "null"}
SPEC_FIELDS = (
    ("shape", "shape"),
    ("thickness_mm", "thickness_mm"),
    ("weight_g", "weight_g"),
    ("balance_cm", "balance_cm"),
    ("face_material", "face_material"),
    ("core", "core"),
    ("surface", "surface"),
)


class CatalogError(ValueError):
    """Raised when the local public dataset is invalid."""


@dataclass(frozen=True)
class CatalogSnapshot:
    generated: str | None
    methodology_url: str
    score_categories: tuple[str, ...]
    version: str
    rackets: tuple[dict[str, Any], ...]
    by_slug: dict[str, dict[str, Any]]
    offers: dict[str, dict[str, Any]]
    offer_lists: dict[str, list[dict[str, Any]]]
    offer_generated: str | None
    signature: tuple[str, str, str]


class RacketCatalog:
    """Loads `rackets.json` only after validating its published identity fields."""

    def __init__(self, data_root: str | Path | None = None) -> None:
        default_root = Path(__file__).resolve().parents[2] / "public"
        self.data_root = Path(data_root or os.getenv("PADEL_MCP_DATA_ROOT", default_root))
        self._snapshot: CatalogSnapshot | None = None
        self._lock = RLock()
        self.reload_degraded = False

    @property
    def data_path(self) -> Path:
        return self.data_root / "rackets.json"

    @property
    def offer_path(self) -> Path:
        configured = os.getenv("PADEL_MCP_OFFER_PATH")
        return Path(configured) if configured else Path(__file__).resolve().parents[1] / "data" / "offers.json"

    def snapshot(self) -> CatalogSnapshot:
        with self._lock:
            try:
                root = self.data_root.resolve(strict=True)
                bundle = load_bundle(root) if (root / "data/bundle-manifest.json").exists() else None
                if os.getenv("PADEL_MCP_REQUIRE_BUNDLE") == "1" and bundle is None:
                    raise CatalogError("verified bundle required")
                raw = bundle.raw['catalog'] if bundle else (root / 'rackets.json').read_bytes()
                offer_path = root / 'data/offers.json' if (root / 'data/offers.json').exists() else self.offer_path
                offer_raw = bundle.raw['offers'] if bundle else (offer_path.read_bytes() if offer_path.exists() else b'')
                signature = (sha256(raw).hexdigest(), sha256(offer_raw).hexdigest(), bundle.version if bundle else '')
                if self._snapshot is None or self._snapshot.signature != signature:
                    self._snapshot = self._load(signature, raw, offer_raw, bundle)
                self.reload_degraded = False
                return self._snapshot
            except (OSError, ValueError, KeyError, TypeError):
                self.reload_degraded = True
                if self._snapshot is None:
                    raise
                return self._snapshot

    def _load(self, signature, raw, offer_raw, bundle=None) -> CatalogSnapshot:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CatalogError("rackets.json is not valid JSON") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("rackets"), list):
            raise CatalogError("rackets.json must contain a rackets list")
        rackets = payload["rackets"]
        if payload.get("total") != len(rackets):
            raise CatalogError("rackets.json total does not match the racket list")

        score_categories = payload.get("score_categories")
        if (
            not isinstance(score_categories, list)
            or not score_categories
            or any(not isinstance(category, str) or not category for category in score_categories)
            or len(set(score_categories)) != len(score_categories)
        ):
            raise CatalogError("rackets.json must provide unique score_categories")
        score_category_keys = set(score_categories)

        by_slug: dict[str, dict[str, Any]] = {}
        for racket in rackets:
            if not isinstance(racket, dict):
                raise CatalogError("each racket must be an object")
            slug = racket.get("slug")
            if not isinstance(slug, str) or not SAFE_SLUG.fullmatch(slug):
                raise CatalogError("rackets.json contains an unsafe slug")
            if slug in by_slug:
                raise CatalogError("rackets.json contains duplicate slugs")
            review_url = racket.get("url")
            if not _is_padel_how_url(review_url) or slug not in urlparse(review_url).path.split("/"):
                raise CatalogError("rackets.json contains an invalid review URL")
            scores = racket.get("scores")
            if not isinstance(scores, dict) or set(scores) != score_category_keys:
                raise CatalogError("rackets.json scores must match score_categories")
            if any(not _is_number(score) or not 0 <= score <= 10 for score in scores.values()):
                raise CatalogError("rackets.json scores must be numeric values from 0 to 10")
            total_score = racket.get("score")
            if not _is_number(total_score) or not 0 <= total_score <= 100:
                raise CatalogError("rackets.json total scores must be numeric values from 0 to 100")
            by_slug[slug] = racket

        methodology = payload.get("methodology")
        if not _is_padel_how_url(methodology):
            raise CatalogError("rackets.json must provide a methodology URL")
        generated = payload.get("generated")
        if generated is not None and not isinstance(generated, str):
            raise CatalogError("rackets.json generated value must be a string")
        offers, offer_lists, offer_generated = self._load_offers(set(by_slug), offer_raw)
        return CatalogSnapshot(
            generated=generated,
            methodology_url=methodology,
            score_categories=tuple(score_categories),
            version=bundle.version if bundle else f"sha256:{sha256(raw + offer_raw).hexdigest()}",
            rackets=tuple(rackets),
            by_slug=by_slug,
            offers=offers,
            offer_lists=offer_lists,
            offer_generated=offer_generated,
            signature=signature,
        )

    def _load_offers(self, racket_slugs: set[str], offer_raw: bytes) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]], str | None]:
        if not offer_raw:
            return {}, {}, None
        payload = json.loads(offer_raw)
        offers = payload.get("offers")
        if not isinstance(offers, dict) or payload.get("offer_count") != len(offers):
            raise CatalogError("offers.json has an invalid offer count")
        if set(offers) - racket_slugs:
            raise CatalogError("offers contain unknown catalog identities")
        generated = payload.get("generated")
        if not isinstance(generated, str) or not generated:
            raise CatalogError("offers.json has no generated timestamp")
        raw_lists = payload.get("offer_lists")
        if raw_lists is None:
            normalized = {slug: {**offer, "store": offer.get("store")} for slug, offer in offers.items()}
            return normalized, {slug: [offer] for slug, offer in normalized.items()}, generated
        if not isinstance(raw_lists, dict) or set(raw_lists) != set(offers):
            raise CatalogError("offers.json has invalid offer_lists")
        if any(not isinstance(items, list) or not items or items[0] != offers[slug] for slug, items in raw_lists.items()):
            raise CatalogError("offers.json has an invalid legacy offer mirror")
        normalized_lists = {
            slug: [{**offer, "store": offer.get("store")} for offer in items]
            for slug, items in raw_lists.items()
        }
        normalized = {slug: items[0] for slug, items in normalized_lists.items()}
        return normalized, normalized_lists, generated

    @staticmethod
    def is_safe_slug(slug: str) -> bool:
        return isinstance(slug, str) and bool(SAFE_SLUG.fullmatch(slug))


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_padel_how_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlparse(value)
    return parsed.scheme == "https" and parsed.netloc == "padel.how"


def known_value(value: Any) -> dict[str, Any]:
    """Normalize legacy free-text specifications without inventing a value."""
    unknown = value is None or (isinstance(value, str) and value.strip().casefold() in UNKNOWN_VALUES)
    return {"value": None if unknown else value, "known": not unknown}


def audience_tags(racket: dict[str, Any]) -> list[str]:
    tags: list[str] = []
    if str(racket.get("gender", "")).casefold() == "women":
        tags.append("women")
    if racket.get("junior") is True:
        tags.append("junior")
    return tags


def public_racket(racket: dict[str, Any], offer: dict[str, Any] | None = None, offers: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Return the stable public MCP representation for one published record."""
    raw_level = racket.get("player_level")
    level = str(raw_level).casefold() if raw_level is not None else None
    editorial_level = level if level in {"beginner", "intermediate", "advanced"} else None
    return {
        "slug": racket["slug"],
        "name": racket.get("name"),
        "brand": racket.get("brand"),
        "year": racket.get("year"),
        "score": racket.get("score"),
        "review_url": racket.get("url"),
        "specifications": {public: known_value(racket.get(source)) for public, source in SPEC_FIELDS},
        "scores": racket.get("scores") if isinstance(racket.get("scores"), dict) else {},
        "editorial_player_level_tag": editorial_level,
        "audience_tags": audience_tags(racket),
        "offer": offer,
        "offers": offers or ([] if offer is None else [offer]),
    }
