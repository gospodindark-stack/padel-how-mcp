import json
import shutil
import os
from pathlib import Path
import pytest
try:
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:
    from mcp.server.fastmcp.exceptions import ToolError
from mcp_server.padel_mcp import server
from mcp_server.padel_mcp.catalog import RacketCatalog
from mcp_server.padel_mcp.bundle import build_manifest, load_bundle, identity_registry
ROOT = Path(__file__).resolve().parents[2]

@pytest.mark.parametrize('count', [0,1,24,25,26,100,125,126,217])
def test_complete_walk(count, tmp_path, monkeypatch):
    payload = json.loads((ROOT/'public/rackets.json').read_bytes())
    payload['rackets'] = payload['rackets'][:count]
    payload['total'] = len(payload['rackets'])
    (tmp_path/'rackets.json').write_text(json.dumps(payload))
    offer = tmp_path/'empty.json'
    offer.write_text(json.dumps({'generated':'fixture','offers':{},'offer_count':0}))
    monkeypatch.setenv('PADEL_MCP_OFFER_PATH', str(offer))
    monkeypatch.setattr(server, 'catalog', RacketCatalog(tmp_path))
    seen = []
    version = server.catalog.snapshot().version
    for offset in range(0, count+26, 25):
        result = server.search_rackets(limit=25, offset=offset, snapshot_version=version).result
        seen.extend(row.slug for row in result.items)
        assert result.total_matches == count
        assert result.has_more == (offset+len(result.items)<count)
    assert len(seen) == len(set(seen)) == count
    with pytest.raises(ToolError, match='stale'):
        server.search_rackets(snapshot_version='old')

@pytest.mark.parametrize('kwargs', [{'offset':True}, {'limit':True}, {'min_score':float('nan')}, {'max_score':float('inf')}])
def test_invalid(kwargs):
    with pytest.raises(ToolError): server.search_rackets(**kwargs)

def make_bundle(root):
    (root/'data').mkdir(parents=True)
    for path in ['rackets.json','data/offers.json','data/v2-rackets.json']:
        shutil.copy2(ROOT/'public'/path, root/path)
    (root/'data/bundle-policy.json').write_text(json.dumps({'source_admission_receipt':'fixture-only'}))
    (root/'data/bundle-manifest.json').write_text(json.dumps(build_manifest(root, generated_at='fixture')))

def test_bundle_swap_rollback_and_tamper(tmp_path):
    a,b = tmp_path/'a', tmp_path/'b'
    make_bundle(a); make_bundle(b)
    identity = json.loads((b/'data/v2-rackets.json').read_bytes())
    identity['rackets'][0]['aliases'] = ['old-name']
    (b/'data/v2-rackets.json').write_text(json.dumps(identity))
    (b/'data/bundle-manifest.json').write_text(json.dumps(build_manifest(b, generated_at='fixture')))
    current=tmp_path/'current';current.symlink_to(a)
    one=load_bundle(current)
    current.unlink();current.symlink_to(b)
    two=load_bundle(current)
    assert one.version != two.version and one.root == a
    current.unlink();current.symlink_to(a)
    assert load_bundle(current).version == one.version
    path=a/'rackets.json'; stat=path.stat();raw=path.read_bytes();path.write_bytes(raw.replace(b'Adidas',b'ADIDAS',1));os.utime(path,ns=(stat.st_atime_ns,stat.st_mtime_ns))
    with pytest.raises(ValueError, match='hash'):load_bundle(current)

def test_identity_alias_inactive():
    slugs,ids=identity_registry({'rackets':[{'racket_id':'stable1','slug':'new-name','aliases':['old-name'],'active':False}]})
    assert slugs['new-name']==slugs['old-name']=='stable1'
    assert ids['stable1']['active'] is False

def test_offer_walk_and_snapshot_change(monkeypatch):
    catalog=RacketCatalog(ROOT/'public')
    monkeypatch.setattr(server,'catalog',catalog)
    first=server.get_offers(limit=25)
    seen=[]
    for offset in range(0, first.result.total_matches+25,25):
        result=server.get_offers(limit=25,offset=offset,snapshot_version=first.dataset.dataset_version).result
        seen.extend(item.slug for item in result.items)
    assert len(seen)==len(set(seen))==first.result.total_matches
    with pytest.raises(ToolError,match='stale'):server.get_offers(snapshot_version='changed')

def test_missing_and_mixed_identity(tmp_path):
    make_bundle(tmp_path)
    path=tmp_path/'data/v2-rackets.json'
    data=json.loads(path.read_bytes());data['rackets']=[];path.write_text(json.dumps(data))
    (tmp_path/'data/bundle-manifest.json').write_text(json.dumps(build_manifest(tmp_path,generated_at='test')))
    with pytest.raises(ValueError,match='missing catalog identity'):load_bundle(tmp_path)


def test_private_nested_offer_rejected(tmp_path):
    make_bundle(tmp_path)
    path=tmp_path/'data/offers.json';data=json.loads(path.read_bytes());slug=next(iter(data['offer_lists']))
    data['offer_lists'][slug][0]['private_eligibility']='member-only'
    path.write_text(json.dumps(data))
    (tmp_path/'data/bundle-manifest.json').write_text(json.dumps(build_manifest(tmp_path,generated_at='test')))
    with pytest.raises(ValueError):load_bundle(tmp_path)


def test_last_good_retained_and_recovers(tmp_path):
    make_bundle(tmp_path)
    catalog=RacketCatalog(tmp_path)
    good=catalog.snapshot()
    path=tmp_path/'data/offers.json'; raw=path.read_bytes()
    path.write_bytes(b'{}')
    assert catalog.snapshot() is good
    assert catalog.reload_degraded is True
    with pytest.raises(ValueError): RacketCatalog(tmp_path).snapshot()
    path.write_bytes(raw)
    assert catalog.snapshot() is good
    assert catalog.reload_degraded is False
