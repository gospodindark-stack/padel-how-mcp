import json
from padel_mcp import telemetry


def test_redaction_and_bounded_cardinality():
    telemetry.BUCKETS.clear()
    for i in range(3000):
        telemetry.record('private-question-'+str(i),True,1,10,'private email@example.invalid',client='secret-token-'+str(i))
    result=telemetry.snapshot();raw=json.dumps(result)
    assert len(result['buckets'])==1
    assert result['buckets'][0]['latency_window_n']==2048
    assert result['buckets'][0]['count']==3000
    assert 'private' not in raw and '@' not in raw and 'secret-token' not in raw


def test_error_and_probe_are_separate():
    telemetry.BUCKETS.clear()
    telemetry.record('get_racket',False,2,0,'sha256:fixture',client='internal',internal=True)
    telemetry.record('get_racket',True,1,42,'sha256:fixture')
    buckets=telemetry.snapshot()['buckets']
    assert len(buckets)==2
    assert any(b['internal_probe'] and b['isError'] for b in buckets)
    assert any(not b['internal_probe'] and b['success'] for b in buckets)


def test_wrapper_preserves_error_without_raw_details():
    from padel_mcp import server
    telemetry.BUCKETS.clear()
    try:
        server.get_racket('invalid/slash?private=secret')
    except Exception:
        pass
    result=telemetry.snapshot()
    assert result['buckets'][0]['isError']
    assert 'private' not in json.dumps(result)
    assert 'invalid/slash' not in json.dumps(result)
