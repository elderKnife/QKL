#!/usr/bin/env python3
"""Read-only V2 deployment acceptance: five sealers, bindings and 35 services."""
import json
import os
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.common.config import contract_address, load_config


def rpc(url, method, params):
    response = requests.post(url, json={'jsonrpc': '2.0', 'method': method, 'params': params, 'id': 1}, timeout=10)
    response.raise_for_status()
    body = response.json()
    if 'error' in body or 'result' not in body:
        raise RuntimeError(f'{method} at {url}: {body}')
    return body['result']


def main():
    cfg = load_config()
    console_port = int(os.environ.get('CONSOLE_PORT', '8400'))
    assert cfg.get('pipeline', {}).get('version') == 2, 'Expected the V2 configuration'
    address = contract_address(cfg)
    nodes = []
    common_sealers = None
    for name, url in cfg['chain']['rpcNodes'].items():
        sealers = set(rpc(url, 'getSealerList', [cfg['chain']['groupId']]))
        assert len(sealers) == 5, f'{name}: expected five sealers'
        if common_sealers is None:
            common_sealers = sealers
        assert sealers == common_sealers, f'{name}: sealer lists differ'
        code = rpc(url, 'getCode', [cfg['chain']['groupId'], address])
        assert isinstance(code, str) and len(code.removeprefix('0x')) > 0, f'{name}: contract not deployed here'
        nodes.append({'node': name, 'blockNumber': rpc(url, 'getBlockNumber', [cfg['chain']['groupId']]), 'sealers': len(sealers)})
    # The chain adapter checks key permissions and uses the machine's GRID account.
    from src.chain.factory import build_registry
    client, registry = build_registry(cfg, 'grid')
    try:
        for subject, publisher in cfg['subjects'].items():
            actual = client.call(address, registry.abi, 'subjects', [publisher])[0]
            assert actual == subject, f'Publisher binding mismatch: {subject}; verify local keys and deployment'
    finally:
        client.close()
    specs = [('grid', cfg['grid']['port'])] + [(v['vppId'], v['port']) for v in cfg['vpps']]
    specs += [(e['edgeId'], e['port']) for e in cfg['edges']] + [('console', console_port)]
    for name, port in specs:
        response = requests.get(f'http://127.0.0.1:{port}/healthz', timeout=10)
        response.raise_for_status()
        assert response.json().get('protocolVersion') == 2, f'{name}: expected V2 service'
    console_url = f'http://127.0.0.1:{console_port}/'
    response = requests.get(console_url, timeout=10)
    response.raise_for_status()
    assert '<div id="app"></div>' in response.text, 'Build the Vue frontend first'
    print(json.dumps({'status': 'PASS', 'nodes': nodes, 'registryAddress': address,
                      'publisherBindings': len(cfg['subjects']), 'healthyServices': len(specs),
                      'consoleUrl': console_url}, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (AssertionError, OSError, ValueError, RuntimeError, requests.RequestException) as exc:
        print(f'Deployment check failed: {exc}', file=sys.stderr)
        sys.exit(1)
