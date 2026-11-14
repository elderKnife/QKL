#!/usr/bin/env python3
"""Create the 3 x 10 x 30 topology; retain the original v1 configuration."""
import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    path = ROOT/'config/system.json'
    old = json.loads(path.read_text())
    if old.get('pipeline', {}).get('version') == 2:
        print('V2 topology already configured; keeping current keys and configuration.')
        return
    archive = ROOT/'config/legacy'
    archive.mkdir(exist_ok=True)
    for name in ('system.json', 'chain.json'):
        target = archive/name
        if not target.exists():
            target.write_text((ROOT/'config'/name).read_text())
    cfg = copy.deepcopy(old)
    cfg['experiment'] = {'name': '3 VPP / 30 edges / 900 devices: root-only FISCO validation', 'version': '2.0.0'}
    cfg['pipeline'] = {'version': 2, 'dataRoot': 'data/v2', 'edgeIntervalSeconds': 30,
                       'edgesPerVpp': 10, 'devicesPerEdge': 30, 'batchesPerGroup': 40}
    cfg['chain']['contract'] = {'name': 'RootHashRegistryV2', 'address': '', 'abiFile': 'contracts/build/RootHashRegistryV2.abi'}
    cfg['chain']['evidenceFile'] = 'config/chain-v2.json'
    cfg['chain']['confirmation'] = {'method':'CHAIN_STATE','pollIntervalMs':20,'timeoutSeconds':15}
    cfg['accounts'] = {'grid': {'pem': 'config/keys/grid.pem', 'rpcNode': 'node0'}}
    cfg['grid']['trustedVpps'] = {}
    cfg['edges'], cfg['vpps'] = [], []
    cfg['edge'] = {'defaultIntervalSeconds': 30, 'maxBufferRecords': 900}
    for index, letter in enumerate('abc'):
        vpp_id = 'vpp-'+letter
        role = 'vpp'+letter.upper()
        vpp = {'vppId': vpp_id, 'port': 8201+index, 'chainAccount': role,
               'signingKey': f'config/keys/{vpp_id}.pem', 'keyId': vpp_id+'-key-v1',
               'gridUrl': 'http://127.0.0.1:8301', 'trustedEdges': {},
               'trustedGrid': {'gridId': 'grid', 'keyId': 'grid-key-v1', 'publicKey': ''}}
        cfg['accounts'][role] = {'pem': vpp['signingKey'], 'rpcNode': f'node{index+1}'}
        cfg['grid']['trustedVpps'][vpp_id] = {'keyId': vpp['keyId'], 'publicKey': ''}
        for number in range(index*10+1, index*10+11):
            edge_id, edge_role = f'edge-{number:02d}', f'edge{number:02d}'
            edge = {'edgeId': edge_id, 'vppId': vpp_id, 'port': 8100+number,
                    'vppUrl': f'http://127.0.0.1:{vpp["port"]}',
                    'signingKey': f'config/keys/{edge_id}.pem', 'keyId': edge_id+'-key-v1',
                    'chainAccount': edge_role, 'intervalSeconds': 30, 'recordsPerPacket': 30,
                    'trustedGrid': copy.deepcopy(vpp['trustedGrid']), 'devices': []}
            for kind, prefix in (('pv', 'PV'), ('storage', 'ES'), ('charger', 'CP')):
                edge['devices'].extend({'deviceId': f'{prefix}-{number:02d}-{d:02d}', 'deviceType': kind} for d in range(1, 11))
            cfg['accounts'][edge_role] = {'pem': edge['signingKey'], 'rpcNode': f'node{(number-1)%5}'}
            vpp['trustedEdges'][edge_id] = {'keyId': edge['keyId'], 'publicKey': ''}
            cfg['edges'].append(edge)
        cfg['vpps'].append(vpp)
    path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False)+'\n')
    print('Configured 3 VPPs, 30 edges, 900 unique devices. V1 retained in config/legacy/.')


if __name__ == '__main__':
    main()
