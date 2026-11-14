#!/usr/bin/env python3
"""Deploy an immutable root registry and bind every publisher to its subject."""
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.common.config import load_config, resolve_path
from src.common.models import utc_iso
from src.chain.client import ChainClient
from src.chain.keys import load_artifacts


def main():
    cfg = load_config()
    output = Path(resolve_path(cfg['chain']['evidenceFile']))
    if output.exists():
        print('V2 deployment already exists. Keeping its immutable history:', output)
        return
    account = cfg['accounts']['grid']
    rpc = cfg['chain']['rpcNodes'][account['rpcNode']]
    client = ChainClient(rpc, resolve_path(account['pem']), group_id=cfg['chain']['groupId'], chain_id=cfg['chain']['chainId'])
    abi, bytecode = load_artifacts(str(ROOT/'contracts/build'), 'RootHashRegistryV2')
    deployed = client.deploy(abi, bytecode, [])
    if not deployed.ok or not deployed.contract_address:
        raise RuntimeError(f'deployment failed: {deployed.to_dict()}')
    address = deployed.contract_address
    print('V2 registry:', address, 'deployment tx:', deployed.tx_hash, flush=True)
    authorizations = []
    for subject, publisher in cfg['subjects'].items():
        result = client.send(address, abi, 'authorizeSubject', [publisher, subject])
        if not result.ok:
            raise RuntimeError(f'authorize {subject} failed: {result.to_dict()}')
        assert client.call(address, abi, 'subjects', [publisher])[0] == subject
        authorizations.append({'subjectId': subject, 'publisher': publisher, **result.to_dict()})
        print('Authorized', subject, result.tx_hash, flush=True)
    evidence = {'contractName': 'RootHashRegistryV2', 'address': address,
                'deployTxHash': deployed.tx_hash, 'deployBlockNumber': deployed.block_number,
                'deployedAt': utc_iso(), 'deployer': client.address(), 'rpcUrl': rpc,
                'groupId': cfg['chain']['groupId'], 'chainId': cfg['chain']['chainId'],
                'compiler': 'solc 0.6.10',
                'sourceSha256': hashlib.sha256((ROOT/'contracts/RootHashRegistryV2.sol').read_bytes()).hexdigest(),
                'authorizations': authorizations}
    output.write_text(json.dumps(evidence, indent=2)+'\n')
    client.close()


if __name__ == '__main__':
    main()
