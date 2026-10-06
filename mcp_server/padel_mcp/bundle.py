"""Pure, fail-closed release bundle and stable public identity registry."""
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import re

ARTIFACTS = {'catalog': 'rackets.json', 'offers': 'data/offers.json', 'identity': 'data/v2-rackets.json', 'policy': 'data/bundle-policy.json'}

def digest(raw):
    return sha256(raw).hexdigest()


def identity_registry(payload):
    """Preserve supplied IDs including inactive entries and explicit aliases."""
    by_slug, by_id = {}, {}
    for row in payload['rackets']:
        rid, slug = row['racket_id'], row['slug']
        if not isinstance(rid, str) or not re.fullmatch(r'[a-zA-Z0-9_-]+', rid) or rid in by_id:
            raise ValueError('invalid or duplicate identity ID')
        by_id[rid] = dict(row)
        for name in [slug, *row.get('aliases', [])]:
            if not isinstance(name, str) or not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', name) or name in by_slug:
                raise ValueError('invalid or duplicate identity slug')
            by_slug[name] = rid
    return by_slug, by_id


@dataclass(frozen=True)
class Bundle:
    root: Path
    version: str
    manifest: dict
    artifacts: dict
    raw: dict

    def handshake(self):
        return {'bundle_version': self.version, 'artifact_versions': {k: digest(v) for k, v in self.raw.items()}, 'identity_count': len(self.artifacts['identity']['rackets'])}


def load_bundle(root):
    # Resolve once before any I/O: switching current cannot mix releases.
    root = Path(root).resolve(strict=True)
    manifest = json.loads((root / 'data/bundle-manifest.json').read_bytes())
    if manifest.get('schema_version') != 1 or set(manifest.get('artifacts', {})) != set(ARTIFACTS):
        raise ValueError('invalid bundle manifest schema')
    paths = {key: (root / path).resolve(strict=True) for key, path in ARTIFACTS.items()}
    if any(not path.is_relative_to(root) for path in paths.values()):
        raise ValueError('bundle artifact escapes immutable root')
    raw = {key: path.read_bytes() for key, path in paths.items()}
    for key, value in raw.items():
        if manifest['artifacts'][key] != {'path': ARTIFACTS[key], 'sha256': digest(value)}:
            raise ValueError('bundle artifact hash mismatch')
    artifacts = {k: json.loads(v) for k, v in raw.items()}
    from .bundle_schema import validate_artifacts
    validate_artifacts(artifacts)
    catalog, offers = artifacts['catalog'], artifacts['offers']
    slugs = [row['slug'] for row in catalog['rackets']]
    if len(set(slugs)) != len(slugs) or catalog['total'] != len(slugs):
        raise ValueError('invalid catalog identities')
    identity, _ = identity_registry(artifacts['identity'])
    if set(slugs) - set(identity):
        raise ValueError('missing catalog identity')
    if not isinstance(artifacts['policy'], dict) or not artifacts['policy'].get('source_admission_receipt'):
        raise ValueError('missing admission receipt')
    if offers['offer_count'] != len(offers['offers']) or set(offers['offers']) - set(slugs):
        raise ValueError('mixed catalog and offer identities')
    from .models import AffiliateOffer, RacketRecord
    from .catalog import public_racket
    for row in catalog['rackets']:
        RacketRecord.model_validate(public_racket(row))
    for slug, offer in offers['offers'].items():
        items = offers.get('offer_lists', {}).get(slug)
        if not items or items[0] != offer:
            raise ValueError('invalid offer mirror')
        for item in items:
            AffiliateOffer.model_validate(item)
    version = 'sha256:' + digest(json.dumps(manifest['artifacts'], sort_keys=True).encode())
    return Bundle(root, version, manifest, artifacts, raw)


def build_manifest(root, *, generated_at):
    root = Path(root)
    return {'schema_version': 1, 'generated_at': generated_at, 'artifacts': {key: {'path': path, 'sha256': digest((root / path).read_bytes())} for key, path in ARTIFACTS.items()}}
