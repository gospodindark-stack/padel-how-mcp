"""Publication-only schemas. Private source records never enter a public bundle."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictBool, model_validator
from .models import AffiliateOffer

class Strict(BaseModel):
    model_config=ConfigDict(extra='forbid', allow_inf_nan=False)

class Identity(Strict):
    racket_id: str = Field(pattern=r'^[a-zA-Z0-9_-]+$')
    slug: str = Field(pattern=r'^[a-z0-9]+(?:-[a-z0-9]+)*$')
    name: str
    canonical_url: str = Field(pattern=r'^https://padel\.how/equipment/racket/[a-z0-9-]+/$')
    category: Literal['racket']
    price_hint_eur: str | None = None
    aliases: list[str] = []
    active: StrictBool = True

class IdentityArtifact(Strict):
    generated: str
    rackets: list[Identity]

class OfferArtifact(Strict):
    schema_version: Literal['1.0.0']
    generated: str = Field(min_length=1)
    offer_count: StrictInt = Field(ge=0)
    offers: dict[str, AffiliateOffer]
    offer_list_count: StrictInt = Field(ge=0)
    offer_lists: dict[str,list[AffiliateOffer]]

    @model_validator(mode='after')
    def mirrors(self):
        if self.offer_count!=len(self.offers) or set(self.offers)!=set(self.offer_lists):
            raise ValueError('offer identity/count mismatch')
        if self.offer_list_count!=sum(map(len,self.offer_lists.values())):
            raise ValueError('offer list count mismatch')
        for slug,items in self.offer_lists.items():
            if not items or items[0]!=self.offers[slug]:raise ValueError('offer mirror mismatch')
            for item in items:
                if (item.state=='link_only') != (item.price is None):raise ValueError('offer price/state mismatch')
                if item.price and ((item.state=='verified_price') != (item.price.freshness=='current')):
                    raise ValueError('offer freshness/state mismatch')
        return self

class PolicyArtifact(Strict):
    schema_version: Literal[1] = 1
    source_admission_receipt: str = Field(min_length=1)
    admission_sha256: str | None = Field(default=None, pattern=r'^[0-9a-f]{64}$')
    catalog_source_generated: str | None = None
    offer_source_generated: str | None = None

# Legacy catalog is intentionally retained verbatim, but its complete published
# field allowlist prevents accidental source/private metadata publication.
CATALOG_KEYS={'generated','schema_version','total','category','methodology','hub','license','endpoints','score_categories','fields','retailer_note','rackets'}
RACKET_KEYS={'balance_cm','brand','core','court_side','demand','face_material','feel','gender','handle_length_cm','image','json_url','junior','markdown_url','name','play_style','player_level','price_range','retailers','retailers_updated','score','scores','shape','slug','surface','thickness_mm','url','weight_g','year'}

def validate_artifacts(artifacts):
    IdentityArtifact.model_validate(artifacts['identity'])
    OfferArtifact.model_validate(artifacts['offers'])
    PolicyArtifact.model_validate(artifacts['policy'])
    cat=artifacts['catalog']
    if set(cat)-CATALOG_KEYS:raise ValueError('private or unknown catalog field')
    if type(cat.get('total')) is not int or not isinstance(cat.get('score_categories'),list) or not cat['score_categories']:
        raise ValueError('invalid catalog schema')
    if len(set(cat['score_categories']))!=len(cat['score_categories']):raise ValueError('duplicate score category')
    for row in cat['rackets']:
        if set(row)-RACKET_KEYS:raise ValueError('private or unknown racket field')
        if set(row['scores'])!=set(cat['score_categories']):raise ValueError('score categories mismatch')
        for value,maximum in [(row['score'],100), *[(x,10) for x in row['scores'].values()]]:
            if type(value) not in (int,float) or not 0<=value<=maximum:raise ValueError('invalid catalog score')
