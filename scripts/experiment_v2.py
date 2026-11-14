#!/usr/bin/env python3
"""Run real HTTP/FISCO rounds and verify one 12000-record group per VPP.

Manual ticks accelerate the demo without changing the automatic 30s interval.
All measured evidence is exported to data/reports/v2/experiment.json.
"""
import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.common.config import load_config
from src.common.models import utc_iso


def call(url, method='GET', body=None, timeout=210):
    response = requests.request(method, url, json=body, timeout=timeout)
    response.raise_for_status()
    return response.json()


def run(vpp, target, fresh=False):
    base = f'http://127.0.0.1:{vpp["port"]}'
    state = call(base+'/api/v2/state')
    if (state.get('session') or {}).get('running'):
        raise RuntimeError(f'{vpp["vppId"]}: pause automatic sampling before this experiment')
    for _ in range(target+1):
        groups = call(base+'/api/v2/groups')['items']
        if fresh:
            groups=[g for g in groups if g.get('chainAnchor',{}).get('timingVersion')=='CHAIN-STATE-V1']
        if groups:
            return groups[0]
        result = call(base+'/api/v2/collection/tick','POST',{})
        if not result.get('ok'):
            raise RuntimeError(str(result))
        state = call(base+'/api/v2/state')
        latest = state.get('latestBatch') or {}
        if latest.get('status') != 'DELIVERED':
            raise RuntimeError(f'{vpp["vppId"]}: {latest}')
        latency=latest['chainAnchor'].get('commitLatencyMs')
        text=f'{latency:.2f} ms' if latency is not None else 'not measured (historical)'
        print(f'{vpp["vppId"]}: batch #{latest["sequence"]}, 300 records, on-chain confirmation {text}',flush=True)
    raise RuntimeError('no complete group after requested rounds')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vpp', choices=['all','vpp-a','vpp-b','vpp-c'], default='all')
    parser.add_argument('--fresh-timing',action='store_true',help='complete a new group measured with the committed-state clock')
    args=parser.parse_args()
    config=load_config()
    specs=[v for v in config['vpps'] if args.vpp=='all' or v['vppId']==args.vpp]
    started=time.perf_counter()
    with ThreadPoolExecutor(max_workers=len(specs)) as pool:
        groups=list(pool.map(lambda v:run(v,40,args.fresh_timing),specs))
    evidence=[]
    for group in groups:
        gid=group['groupId']
        report=call(f'http://127.0.0.1:8301/api/v2/groups/{gid}/verify','POST',{})
        assert (report['status'],report['batchCount'],report['recordCount'])==('PASS',40,12000),report
        assert report['computedRootHash']==report['chainRootHash']
        evidence.append({'vppId':group['vppId'],'groupId':gid,'chainAnchor':group['chainAnchor'],'verification':report})
        print(f'{group["vppId"]}: PASS, 12000 original records, root comparison {report["comparisonNs"]/1000:.3f} us; full processing {report["totalVerificationMs"]:.2f} ms',flush=True)
    output=ROOT/'data/reports/v2'
    output.mkdir(parents=True,exist_ok=True)
    result={'checkedAt':utc_iso(),'mode':'REAL_FISCO_HTTP_MANUAL_TICKS','elapsedSeconds':time.perf_counter()-started,
            'grid':call('http://127.0.0.1:8301/api/v2/state'),'groups':evidence}
    path=output/('timing-v2.json' if args.fresh_timing else 'experiment.json')
    path.write_text(json.dumps(result,indent=2,ensure_ascii=False)+'\n')
    print('Evidence:',path,flush=True)


if __name__=='__main__':
    main()
