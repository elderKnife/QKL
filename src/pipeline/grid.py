"""GRID stores its own originals and independently checks FISCO commitments."""
import threading
import time

from ..common.config import data_path
from ..common.models import ProtocolError, utc_iso, new_id, validate_record
from .anchors import Anchors
from .protocol import (BATCH_SCHEMA, GROUP_SCHEMA, require, identifier, positive,
                       records_root, verify_signature, descriptor, group_root,
                       vpp_batch_id, group_id)
from .store import Store
from .command_runtime import GridCommands


def compare_roots(computed, on_chain):
    """Time one equality operation only; tree construction and I/O are outside."""
    if computed is None or on_chain is None:
        return {'rootsEqual':None,'comparisonNs':None,'comparisonMs':None}
    started = time.perf_counter_ns()
    equal = computed == on_chain
    elapsed = time.perf_counter_ns()-started
    return {'rootsEqual':equal,'comparisonNs':elapsed,'comparisonMs':elapsed/1e6}


class GridRuntime(GridCommands):
    def __init__(self, config, anchors=None):
        self.config = config
        self.spec = config['grid']
        self.vpps = {v['vppId']: v for v in config['vpps']}
        self.anchors = anchors or Anchors(config, 'grid')
        self.store = Store(data_path(config, 'grid', 'pipeline.db'))
        self.lock = threading.RLock()

    def identity(self, payload, signature, schema):
        require(isinstance(payload, dict) and payload.get('schemaVersion') == schema, 'unsupported protocol')
        for field in ('vppId', 'sessionId'):
            identifier(payload.get(field))
        require(payload['vppId'] in self.vpps, 'unknown VPP', 'IDENTITY_NOT_ALLOWED')
        verify_signature(payload, signature, self.spec['trustedVpps'][payload['vppId']])

    def receive_batch(self, body):
        started = time.perf_counter_ns()
        require(isinstance(body, dict), 'body must be an object')
        batch = body.get('batch')
        self.identity(batch, body.get('signature'), BATCH_SCHEMA)
        bid = identifier(batch.get('batchId'))
        seq = positive(batch.get('sequence'))
        require(bid == vpp_batch_id(batch['vppId'], batch['sessionId'], seq), 'batch ID does not match round')
        records = batch.get('records')
        require(isinstance(records, list) and len(records) == 300 and batch.get('recordCount') == 300,
                'a VPP batch must contain exactly 300 records')
        allowed = {d['deviceId']: d['deviceType'] for e in self.config['edges']
                   if e['vppId'] == batch['vppId'] for d in e['devices']}
        for record in records:
            validate_record(record)
        require({r['deviceId'] for r in records} == set(allowed), 'VPP device roster mismatch', 'TARGET_MISMATCH')
        require(all(allowed[r['deviceId']] == r['deviceType'] for r in records), 'device type mismatch')
        hash_started = time.perf_counter_ns()
        root = records_root(records)
        hash_ms = (time.perf_counter_ns()-hash_started)/1e6
        require(root == batch.get('rootHash'), 'received originals do not match VPP root', 'ROOT_MISMATCH', bid)
        chain_started = time.perf_counter_ns()
        chain, receipt = self.anchors.confirm(body.get('chainAnchor'), bid, root, 300, batch['vppId'], BATCH_SCHEMA)
        chain_ms = (time.perf_counter_ns()-chain_started)/1e6
        report = {'status': 'VERIFIED', 'batchId': bid, 'vppId': batch['vppId'], 'recordCount': 300,
                  'computedRootHash': root, 'chainRootHash': chain['rootHash'], 'chainReceipt': receipt,
                  'hashComputeMs': hash_ms, 'chainQueryMs': chain_ms,
                  'verificationMs': (time.perf_counter_ns()-started)/1e6, 'verifiedAt': utc_iso()}
        with self.lock:
            self.store.insert('batch', bid, {'batch': batch, 'signature': body['signature']},
                              {'status': 'VERIFIED', 'chainAnchor': body['chainAnchor'], 'verification': report})
        return self.batch(bid, False)['verification']

    def receive_group(self, body):
        require(isinstance(body, dict), 'body must be an object')
        group = body.get('group')
        self.identity(group, body.get('signature'), GROUP_SCHEMA)
        gid = identifier(group.get('groupId'))
        start = positive(group.get('startSequence'))
        require(gid == group_id(group['vppId'], group['sessionId'], start), 'group ID mismatch')
        require(group.get('batchCount') == 40 and group.get('recordCount') == 12000
                and group.get('endSequence') == start+39, 'group count or range mismatch')
        items = group.get('batches')
        root = group_root(items)
        require(items[0]['sequence'] == start and items[0]['vppId'] == group['vppId']
                and items[0]['sessionId'] == group['sessionId'], 'group manifest identity mismatch')
        require(root == group.get('rootHash'), 'group manifest root mismatch', 'ROOT_MISMATCH', gid)
        for item in items:
            stored = self.store.get('batch', item['batchId'])
            require(stored is not None, 'GRID is missing a batch in this group', 'NOT_FOUND', item['batchId'])
            require(descriptor(stored['payload']['batch']) == descriptor(item), 'stored batch differs from manifest', 'ROOT_MISMATCH', item['batchId'])
        chain, receipt = self.anchors.confirm(body.get('chainAnchor'), gid, root, 12000, group['vppId'], GROUP_SCHEMA)
        # Receipt/manifest checks do not stand in for a full 12000-record rehash.
        self.store.insert('group', gid, {'group': group, 'signature': body['signature']},
                          {'status': 'READY', 'chainAnchor': body['chainAnchor'], 'chainReceipt': receipt,
                           'chainRootHash': chain['rootHash'], 'verification': None})
        return self.group(gid)

    def verify_group(self, gid):
        started = time.perf_counter_ns()
        row = self.store.get('group', gid)
        require(row is not None, 'group not found', 'NOT_FOUND', gid)
        group = row['payload']['group']
        hash_started = time.perf_counter_ns()
        rebuilt, failures, batch_checks = [], [], []
        record_count = 0
        checked_batches = 0
        for item in group['batches']:
            stored = self.store.get('batch', item['batchId'])
            if stored is None:
                failures.append({'batchId': item['batchId'], 'reason': 'missing originals'})
                continue
            batch = stored['payload']['batch']
            raw = batch.get('records', [])
            record_count += len(raw)
            checked_batches += 1
            try:
                # Deliberately rebuild every tree from GRID's raw records. Never use cached rootHash.
                computed = records_root(raw)
                fresh = {**descriptor(batch), 'rootHash': computed, 'recordCount': len(raw)}
                rebuilt.append(fresh)
                batch_checks.append({'batchId':item['batchId'],'sequence':item['sequence'],
                                     'computedRootHash':computed,'expectedRootHash':item['rootHash'],
                                     'matches':fresh==descriptor(item)})
                if fresh != descriptor(item):
                    failures.append({'batchId': item['batchId'], 'reason': 'recomputed batch differs from committed descriptor',
                                     'computedRootHash': computed, 'expectedRootHash': item['rootHash']})
            except (ProtocolError, KeyError, TypeError) as exc:
                failures.append({'batchId': item['batchId'], 'reason': str(exc)})
        computed_root = None
        try:
            computed_root = group_root(rebuilt)
        except (ProtocolError, KeyError, TypeError) as exc:
            failures.append({'batchId': gid, 'reason': str(exc)})
        hash_ms = (time.perf_counter_ns()-hash_started)/1e6
        chain_started = time.perf_counter_ns()
        chain_root, chain_error = None, None
        try:
            chain = self.anchors.read(gid, count=12000, subject=group['vppId'], schema=GROUP_SCHEMA)
            chain_root = chain['rootHash']
        except Exception as exc:
            chain_error = str(exc)
        chain_ms = (time.perf_counter_ns()-chain_started)/1e6
        comparison = compare_roots(computed_root, chain_root)
        if comparison['rootsEqual'] is False:
            failures.append({'batchId': gid, 'reason': 'group root differs from independently queried chain root'})
        total_ms = (time.perf_counter_ns()-started)/1e6
        report = {'verificationId': new_id('CHECK2'), 'groupId': gid, 'vppId': group['vppId'],
                  'status': 'ERROR' if chain_error else ('FAIL' if failures else 'PASS'),
                  'batchCount': checked_batches, 'recordCount': record_count,
                  'computedRootHash': computed_root, 'chainRootHash': chain_root,
                  'hashComputeMs': hash_ms, 'chainQueryMs': chain_ms,
                  'verificationMs':comparison['comparisonMs'],'totalVerificationMs':total_ms,
                  **comparison,'timingVersion':'ROOT-COMPARE-V1',
                  'primaryTimingScope':'ROOT_EQUALITY_ONLY','verifiedAt': utc_iso(),
                  'batchChecks':batch_checks,'failures': failures, 'chainError': chain_error}
        self.store.insert('verification', report['verificationId'], report)
        self.store.update('group', gid, status=report['status'], verification=report)
        return report

    def batch(self, bid, include_records=True):
        row = self.store.get('batch', bid)
        require(row is not None, 'batch not found', 'NOT_FOUND', bid)
        batch = row['payload']['batch']
        return {**{k: v for k, v in batch.items() if include_records or k not in ('records', 'edgePackets')},
                **({'signature':row['payload']['signature'],
                    'deviceOrigins':{d['deviceId']:e['edgeId'] for e in self.config['edges']
                                     if e['vppId']==batch['vppId'] for d in e['devices']}} if include_records else {}),
                **row['state'], 'createdAt': row['createdAt']}

    def group(self, gid):
        row = self.store.get('group', gid)
        require(row is not None, 'group not found', 'NOT_FOUND', gid)
        return {**row['payload']['group'], 'signature':row['payload']['signature'],
                **row['state'], 'createdAt': row['createdAt']}

    def state(self, vpp_id=None):
        batches = [r for r in self.store.all('batch', ('batch.records', 'batch.edgePackets'))
                   if vpp_id is None or r['payload']['batch']['vppId'] == vpp_id]
        groups = [r for r in self.store.all('group') if vpp_id is None or r['payload']['group']['vppId'] == vpp_id]
        return {'vppId': vpp_id, 'batchCount': len(batches), 'recordCount': sum(r['payload']['batch']['recordCount'] for r in batches),
                'groupCount': len(groups), 'verifiedGroupCount': sum(r['state']['status'] == 'PASS' for r in groups),
                'registryAddress': self.anchors.address, 'rpcUrl': self.anchors.client.rpc_url}
