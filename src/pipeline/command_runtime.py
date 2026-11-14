"""Direct GRID/VPP command delivery; only command roots are committed to FISCO."""
import math
import time

import requests

from ..common.crypto import Signer
from ..common.config import resolve_path
from ..common.models import ProtocolError, new_id, utc_iso
from .command_protocol import (COMMAND_BATCH_SCHEMA, COMMAND_RECEIPT_SCHEMA, COMMAND_ROOT_RULE,
                               command_root, now_seconds, simulate_command, valid_time,
                               validate_envelope, validate_targets)
from .protocol import identifier, require, sign, verify_signature


class VppCommands:
    def receive_commands(self, body):
        started = time.perf_counter_ns()
        require(isinstance(body, dict), 'command body must be an object')
        envelope = body.get('envelope')
        validate_envelope(self.config, envelope, self.vpp_id)
        verify_signature(envelope, body.get('signature'), self.spec['trustedGrid'])
        hash_started = time.perf_counter_ns()
        root = command_root(envelope['commands'])
        hash_ms = (time.perf_counter_ns() - hash_started) / 1e6
        require(root == envelope['rootHash'], 'command content differs from declared root', 'ROOT_MISMATCH')
        anchor = body.get('chainAnchor')
        require(isinstance(anchor, dict) and anchor.get('registryAddress') == self.anchors.address,
                'unexpected registry address', 'ROOT_MISMATCH')
        chain_started = time.perf_counter_ns()
        chain = self.anchors.read(envelope['dispatchId'], count=len(envelope['commands']),
                                  subject=envelope['gridId'], schema=COMMAND_BATCH_SCHEMA)
        chain_ms = (time.perf_counter_ns() - chain_started) / 1e6
        chain_root = chain['rootHash']
        compare_started = time.perf_counter_ns()
        equal = root == chain_root
        comparison_ns = time.perf_counter_ns() - compare_started
        require(equal, 'command root differs from independently queried chain root', 'ROOT_MISMATCH')
        verification_ms = (time.perf_counter_ns() - started) / 1e6
        receipt = {'schemaVersion': COMMAND_RECEIPT_SCHEMA, 'dispatchId': envelope['dispatchId'],
                   'releaseId': envelope['releaseId'], 'gridId': envelope['gridId'], 'vppId': self.vpp_id,
                   'commandCount': len(envelope['commands']), 'rootHash': root, 'chainRootHash': chain_root,
                   'status': 'VERIFIED', 'verifiedAt': utc_iso(), 'verificationMs': comparison_ns / 1e6,
                   'totalVerificationMs': verification_ms, 'timingVersion': 'ROOT-COMPARE-V1',
                   'primaryTimingScope': 'ROOT_EQUALITY_ONLY', 'rootsEqual': equal,
                   'hashComputeMs': hash_ms, 'chainQueryMs': chain_ms,
                   'comparisonNs': comparison_ns, 'comparisonMs': comparison_ns / 1e6}
        signature = sign(self.signer, receipt)
        self.store.insert('command', envelope['dispatchId'],
                          {'envelope': envelope, 'signature': body['signature']},
                          {'status': 'VERIFIED', 'chainAnchor': anchor, 'receipt': receipt, 'receiptSignature': signature})
        # No request is sent to any edge. This receipt certifies verification only.
        return {'receipt': receipt, 'signature': signature}


class GridCommands:
    def command_targets(self):
        return {'vpps': [{'vppId': v['vppId'], 'edges': [
            {'edgeId': e['edgeId'], 'devices': e['devices']} for e in self.config['edges'] if e['vppId'] == v['vppId']
        ]} for v in self.config['vpps']]}

    def release_commands(self, body):
        require(isinstance(body, dict), 'release body must be an object')
        mode = body.get('mode', 'SINGLE')
        targets = validate_targets(self.config, body.get('targets'), mode)
        started = time.perf_counter_ns()
        release_id = new_id('REL2')
        issued_at = now_seconds()
        grid_id = self.spec['gridId']
        signer = Signer.from_file(resolve_path(self.spec['signingKey']), self.spec['keyId'])
        vpp_id = targets[0]['vppId']
        dispatch_id = new_id('CMD2')
        commands = [simulate_command(t, command_id=new_id('INS2'), dispatch_id=dispatch_id,
                                     release_id=release_id, grid_id=grid_id, issued_at=issued_at)
                    for t in targets]
        envelope = {'schemaVersion': COMMAND_BATCH_SCHEMA, 'rootRule': COMMAND_ROOT_RULE,
                    'dispatchId': dispatch_id, 'releaseId': release_id, 'gridId': grid_id,
                    'vppId': vpp_id, 'mode': mode, 'recordCount': len(commands),
                    'rootHash': command_root(commands), 'issuedAt': issued_at, 'commands': commands}
        signature = sign(signer, envelope)
        self.store.insert('command', dispatch_id, {'envelope': envelope, 'signature': signature}, {'status': 'BUILT'})
        self.store.insert('command-release', release_id,
                          {'releaseId': release_id, 'mode': mode, 'issuedAt': issued_at,
                           'commandCount': len(targets), 'vppId': vpp_id, 'vppCount': 1,
                           'dispatchIds': [dispatch_id]}, {'status': 'PUBLISHING'})
        result = self.deliver_commands(dispatch_id, publication_started=started)
        elapsed_ms = (time.perf_counter_ns() - started) / 1e6
        passed = result['status'] == 'VERIFIED'
        received = result.get('receivedAt') if passed else None
        receipt = result.get('receipt', {})
        self.store.update('command-release', release_id, status='VERIFIED' if passed else 'ERROR', verifiedVppCount=int(passed),
                          receivedAt=received, allVerifiedAt=received,
                          publicationTotalMs=elapsed_ms, publishToReceiptMs=result.get('publishToReceiptMs'),
                          comparisonNs=receipt.get('comparisonNs'), comparisonMs=receipt.get('comparisonMs'),
                          verificationMs=receipt.get('verificationMs'), timingVersion=receipt.get('timingVersion'),
                          primaryTimingScope=receipt.get('primaryTimingScope'))
        return self.command_release(release_id)

    def verify_command_receipt(self, result, envelope):
        require(isinstance(result, dict) and isinstance(result.get('receipt'), dict), 'VPP verification receipt missing')
        receipt = result['receipt']
        verify_signature(receipt, result.get('signature'), self.spec['trustedVpps'][envelope['vppId']])
        expected = {'schemaVersion': COMMAND_RECEIPT_SCHEMA, 'dispatchId': envelope['dispatchId'],
                    'releaseId': envelope['releaseId'], 'gridId': envelope['gridId'], 'vppId': envelope['vppId'],
                    'commandCount': envelope['recordCount'], 'rootHash': envelope['rootHash'],
                    'chainRootHash': envelope['rootHash'], 'status': 'VERIFIED'}
        require(all(receipt.get(k) == value for k, value in expected.items()),
                'VPP receipt does not match command batch', 'ROOT_MISMATCH')
        valid_time(receipt.get('verifiedAt'))
        for key in ('verificationMs', 'totalVerificationMs', 'hashComputeMs', 'chainQueryMs', 'comparisonNs', 'comparisonMs'):
            value = receipt.get(key)
            require(type(value) in (int, float) and math.isfinite(value) and value >= 0, 'invalid VPP timing')
        require(type(receipt['comparisonNs']) is int and receipt['comparisonMs'] == receipt['comparisonNs'] / 1e6,
                'invalid root comparison timing')
        require(receipt.get('timingVersion') == 'ROOT-COMPARE-V1'
                and receipt.get('primaryTimingScope') == 'ROOT_EQUALITY_ONLY'
                and receipt.get('rootsEqual') is True
                and receipt['verificationMs'] == receipt['comparisonMs'], 'invalid verification timing scope')
        return receipt

    def deliver_commands(self, dispatch_id, publication_started=None):
        row = self.store.get('command', dispatch_id)
        require(row is not None, 'command batch not found', 'NOT_FOUND', dispatch_id)
        if row['state'].get('status') == 'VERIFIED':
            return self.command_dispatch(dispatch_id)
        envelope = row['payload']['envelope']
        started = time.perf_counter_ns()
        try:
            anchor = row['state'].get('chainAnchor') or self.anchors.submit(
                dispatch_id, envelope['rootHash'], envelope['recordCount'], self.spec['gridId'], COMMAND_BATCH_SCHEMA)
            self.store.update('command', dispatch_id, status='ANCHORED', chainAnchor=anchor)
            spec = self.vpps[envelope['vppId']]
            sent_at = utc_iso()
            delivery_started = time.perf_counter_ns()
            response = requests.post(f"http://127.0.0.1:{spec['port']}/api/v2/commands",
                                     json={**row['payload'], 'chainAnchor': anchor}, timeout=120)
            response_at = utc_iso()
            response_ns = time.perf_counter_ns()
            require(200 <= response.status_code < 300, f'VPP HTTP {response.status_code}', 'TARGET_MISMATCH')
            result = response.json()
            receipt = self.verify_command_receipt(result, envelope)
            self.store.update('command', dispatch_id, status='VERIFIED', receipt=receipt,
                              receiptSignature=result['signature'], sentAt=sent_at, receivedAt=response_at,
                              deliveryMs=(response_ns - delivery_started) / 1e6,
                              publishToReceiptMs=(response_ns - (started if publication_started is None else publication_started)) / 1e6,
                              httpStatus=response.status_code, lastError=None)
        except (requests.RequestException, ProtocolError, ValueError) as exc:
            self.store.update('command', dispatch_id, status='ERROR', lastError=str(exc), failedAt=utc_iso())
        return self.command_dispatch(dispatch_id)

    def command_dispatch(self, dispatch_id, include_commands=True):
        row = self.store.get('command', identifier(dispatch_id))
        require(row is not None, 'command batch not found', 'NOT_FOUND', dispatch_id)
        return {**{k: v for k, v in row['payload']['envelope'].items() if include_commands or k != 'commands'},
                **({'signature': row['payload']['signature']} if include_commands else {}),
                **row['state'], 'createdAt': row['createdAt']}

    def command_release(self, release_id):
        row = self.store.get('command-release', identifier(release_id))
        require(row is not None, 'command release not found', 'NOT_FOUND', release_id)
        return {**row['payload'], **row['state'], 'createdAt': row['createdAt'],
                'dispatches': [self.command_dispatch(key) for key in row['payload']['dispatchIds']]}

    def command_releases(self):
        return [{**row['payload'], **row['state'], 'createdAt': row['createdAt']}
                for row in self.store.all('command-release')]
