"""Bounded aggregate telemetry: never stores arguments, URLs, IPs or messages."""
from collections import defaultdict, deque
from contextvars import ContextVar
from functools import wraps
import json
import threading
import time
from typing import get_type_hints

CONTEXT = ContextVar('telemetry_context', default=('unknown', False))
TOOLS = {'search_rackets','get_racket','compare_rackets','get_offers','list_racket_brands'}
CLIENTS = {'unknown','claude','chatgpt','inspector','internal'}
LOCK = threading.Lock()
BUCKETS = {}


def record(tool, success, elapsed_ms, size, version, *, client='unknown', internal=False):
    tool = tool if tool in TOOLS else 'other'
    client = client if client in CLIENTS else 'unknown'
    key = (tool, bool(success), client, bool(internal))
    # Only fixed dimensions; dataset version is one latest value, never a label.
    version = version if isinstance(version,str) and len(version)<=100 and all(c.isalnum() or c in ':._-' for c in version) else 'unknown'
    with LOCK:
        bucket = BUCKETS.setdefault(key, {'count':0,'bytes':0,'latencies':deque(maxlen=2048),'dataset_version':'unknown'})
        bucket['count'] += 1
        bucket['bytes'] += max(0,int(size))
        bucket['latencies'].append(max(0,float(elapsed_ms)))
        bucket['dataset_version'] = version


def snapshot():
    with LOCK:
        result=[]
        for (tool,success,client,internal), bucket in sorted(BUCKETS.items()):
            values=sorted(bucket['latencies'])
            result.append({'method':'tools/call','tool':tool,'success':success,'isError':not success,
                           'client_family':client,'internal_probe':internal,'count':bucket['count'],
                           'output_bytes':bucket['bytes'],'dataset_version':bucket['dataset_version'],
                           'latency_window_n':len(values),'latency_p50_ms':values[int((len(values)-1)*.5)],
                           'latency_p95_ms':values[int((len(values)-1)*.95)]})
        return {'schema_version':1,'scope':'process_lifetime','latency_window_max':2048,'buckets':result}


def observe_tool(function):
    @wraps(function)
    def wrapped(*args,**kwargs):
        start=time.perf_counter();success=False;size=0;version='unknown'
        try:
            result=function(*args,**kwargs)
            serialized=result.model_dump_json() if hasattr(result,'model_dump_json') else json.dumps(result)
            size=len(serialized.encode())
            dataset=getattr(result,'dataset',None)
            version=getattr(dataset,'dataset_version','unknown')
            success=not getattr(result,'isError',False)
            return result
        finally:
            client,internal=CONTEXT.get()
            record(function.__name__,success,(time.perf_counter()-start)*1000,size,version,client=client,internal=internal)
    wrapped.__annotations__=get_type_hints(function,include_extras=True)
    return wrapped
