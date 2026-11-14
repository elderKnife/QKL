"""V2 behavior tests use an explicit in-memory chain double, never chain evidence."""
import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.common.config import load_config, resolve_path
from src.common.crypto import Signer
from src.common.models import ProtocolError, utc_iso
from src.pipeline.protocol import (records_root, group_root, descriptor, sign, verify_signature,
                                   EDGE_SCHEMA, BATCH_SCHEMA, GROUP_SCHEMA, vpp_batch_id, group_id, edge_batch_id)
from src.pipeline.grid import GridRuntime
from src.pipeline.edge import EdgeRuntime
from src.pipeline.vpp import VppRuntime
from src.pipeline.store import Store
from src.pipeline.console import create_app
from src.pipeline.apps import grid_app
from src.chain.client import ChainClient


class FakeAnchors:
    address = '0x'+'2'*40
    client = SimpleNamespace(rpc_url='fake://unit-test-only')

    def __init__(self):
        self.values = {}
        self.reads = 0

    def submit(self, bid, root, count, subject, schema):
        self.values[bid] = dict(rootHash=root, recordCount=count, vppId=subject, schemaVersion=schema)
        return dict(txHash='unit-test-only', registryAddress=self.address, receiptLatencyMs=0.001, status='CONFIRMED')

    def read(self, bid, root=None, count=None, subject=None, schema=None):
        self.reads += 1
        if bid not in self.values:
            raise ProtocolError('ROOT_MISMATCH', 'missing anchor')
        value = self.values[bid]
        for key, expected in [('rootHash', root), ('recordCount', count), ('vppId', subject), ('schemaVersion', schema)]:
            if expected is not None and value[key] != expected:
                raise ProtocolError('ROOT_MISMATCH', key+' differs')
        return value.copy()

    def confirm(self, anchor, *args):
        if not isinstance(anchor, dict) or anchor.get('registryAddress') != self.address:
            raise ProtocolError('ROOT_MISMATCH', 'registry differs')
        return self.read(*args), dict(status='PASS', txHash='unit-test-only')


def records(config, vpp='vpp-a', seq=1):
    return [dict(recordId=f'R-{device["deviceId"]}-{seq}', deviceId=device['deviceId'], deviceType=device['deviceType'],
                 metric='active_power_kw', value=1.234, unit='kW', timestamp='2026-11-14T00:00:00.000Z')
            for edge in config['edges'] if edge['vppId'] == vpp for device in edge['devices']]


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = copy.deepcopy(load_config())
        self.cfg['pipeline']['dataRoot'] = self.tmp.name
        self.chain = FakeAnchors()
        self.cfg['chain']['contract']['address'] = self.chain.address
        self.grid = GridRuntime(self.cfg, self.chain)
        self.signers = {v['vppId']: Signer.from_file(resolve_path(v['signingKey']), v['keyId']) for v in self.cfg['vpps']}

    def tearDown(self):
        self.tmp.cleanup()

    def batch(self, seq, vpp='vpp-a', session='TEST'):
        raw = records(self.cfg, vpp, seq)
        bid = vpp_batch_id(vpp, session, seq)
        batch = dict(schemaVersion=BATCH_SCHEMA, batchId=bid, vppId=vpp, sessionId=session, sequence=seq,
                     recordCount=300, rootHash=records_root(raw), records=raw, builtAt=utc_iso())
        anchor = self.chain.submit(bid, batch['rootHash'], 300, vpp, BATCH_SCHEMA)
        return dict(batch=batch, signature=sign(self.signers[vpp], batch), chainAnchor=anchor)

    def group(self, vpp='vpp-a'):
        items = []
        for seq in range(1, 41):
            body = self.batch(seq, vpp)
            self.grid.receive_batch(body)
            items.append(descriptor(body['batch']))
        gid = group_id(vpp, 'TEST', 40)
        group = dict(schemaVersion=GROUP_SCHEMA, groupId=gid, vppId=vpp, sessionId='TEST', startSequence=1,
                     endSequence=40, batchCount=40, recordCount=12000, rootHash=group_root(items), batches=items, builtAt=utc_iso())
        anchor = self.chain.submit(gid, group['rootHash'], 12000, vpp, GROUP_SCHEMA)
        self.grid.receive_group(dict(group=group, signature=sign(self.signers[vpp], group), chainAnchor=anchor))
        return group

    def test_300_auto_verifies_against_independent_chain_and_is_idempotent(self):
        body = self.batch(1)
        result = self.grid.receive_batch(body)
        self.assertEqual(result['status'], 'VERIFIED')
        self.assertGreater(result['verificationMs'], 0)
        self.assertEqual(self.chain.reads, 1)
        self.grid.receive_batch(body)
        self.assertEqual(self.grid.state()['batchCount'], 1)
        self.assertEqual(self.grid.state('vpp-b')['batchCount'], 0)
        changed = copy.deepcopy(body)
        changed['batch']['records'][0]['value'] = 2.345
        changed['signature'] = sign(self.signers['vpp-a'], changed['batch'])
        with self.assertRaises(ProtocolError):
            self.grid.receive_batch(changed)

    def test_grid_keeps_reports_local_and_vpp_delivers_40_batches_with_empty_http_responses(self):
        with patch('src.pipeline.grid.GridRuntime', return_value=self.grid):
            client=grid_app(self.cfg).test_client()
        vpp=VppRuntime(self.cfg,self.cfg['vpps'][0],self.chain)
        responses=[]
        def deliver(url, json, timeout):
            path='/api/'+url.split('/api/',1)[1]
            result=client.post(path,json=json)
            responses.append((path,result.status_code,result.data))
            # No JSON parsing or verification report is available to the sender.
            return SimpleNamespace(status_code=result.status_code)
        with patch('src.pipeline.vpp.requests.post',side_effect=deliver):
            for seq in range(1,41):
                batch=self.batch(seq)['batch']
                bid=batch['batchId']
                vpp.store.insert('batch',bid,batch,{'status':'BUILT'})
                vpp.send_batch(bid)
                sent=vpp.batch(bid)
                self.assertEqual(sent['status'],'DELIVERED')
                self.assertEqual(sent['delivery'],{'httpStatus':204})
                self.assertNotIn('gridReport',sent)
                saved=client.get('/api/v2/batches/'+bid).json
                self.assertEqual(saved['status'],'VERIFIED')
                self.assertEqual(saved['verification']['computedRootHash'],batch['rootHash'])
            group=vpp.try_group('TEST',40)
        self.assertEqual(len(responses),41)
        self.assertTrue(all(status==204 and body==b'' for _,status,body in responses))
        self.assertEqual(responses[-1][0],'/api/v2/groups')
        self.assertEqual(group['status'],'DELIVERED')
        self.assertEqual(group['delivery'],{'httpStatus':204})
        self.assertNotIn('gridReport',group)
        self.assertEqual(self.grid.group(group['groupId'])['status'],'READY')
        self.assertEqual(self.grid.verify_group(group['groupId'])['status'],'PASS')

    def test_grid_rejects_bad_chain_root_and_vpp_does_not_count_it_as_delivered(self):
        with patch('src.pipeline.grid.GridRuntime', return_value=self.grid):
            client=grid_app(self.cfg).test_client()
        vpp=VppRuntime(self.cfg,self.cfg['vpps'][0],self.chain)
        body=self.batch(1)
        bid=body['batch']['batchId']
        self.chain.values[bid]['rootHash']='0'*64
        vpp.store.insert('batch',bid,body['batch'],{'status':'ANCHORED','chainAnchor':body['chainAnchor']})
        def deliver(url, json, timeout):
            result=client.post('/api/v2/batches',json=json)
            self.assertEqual(result.status_code,409)
            return SimpleNamespace(status_code=result.status_code)
        with patch('src.pipeline.vpp.requests.post',side_effect=deliver):
            vpp.send_batch(bid)
        sent=vpp.batch(bid)
        self.assertEqual(sent['status'],'GRID_REJECTED')
        self.assertEqual(sent['delivery'],{'httpStatus':409})
        self.assertNotIn('gridReport',sent)
        self.assertEqual(self.grid.state()['batchCount'],0)
        self.assertIsNone(vpp.try_group('TEST',40))

    def test_chain_root_cannot_be_replaced_by_declared_root(self):
        body = self.batch(1)
        self.chain.values[body['batch']['batchId']]['rootHash'] = '0'*64
        with self.assertRaisesRegex(ProtocolError, 'rootHash differs'):
            self.grid.receive_batch(body)

    def test_wrong_vpp_device_roster_and_signature_are_rejected(self):
        body = self.batch(1)
        body['batch']['records'][0] = records(self.cfg, 'vpp-b')[0]
        body['signature'] = sign(self.signers['vpp-a'], body['batch'])
        with self.assertRaisesRegex(ProtocolError, 'roster'):
            self.grid.receive_batch(body)
        body = self.batch(2)
        body['signature'] = sign(self.signers['vpp-b'], body['batch'])
        with self.assertRaises(ProtocolError):
            self.grid.receive_batch(body)

    def test_full_12000_rehash_and_tampering_even_with_cached_roots_unchanged(self):
        group = self.group()
        self.assertEqual(self.grid.group(group['groupId'])['status'], 'READY')
        result = self.grid.verify_group(group['groupId'])
        self.assertEqual((result['status'], result['batchCount'], result['recordCount']), ('PASS', 40, 12000))
        self.assertGreater(result['hashComputeMs'], 0)
        self.assertGreaterEqual(result['totalVerificationMs'], result['hashComputeMs']+result['chainQueryMs'])
        self.assertEqual(result['verificationMs'],result['comparisonMs'])
        self.assertEqual(result['comparisonMs'],result['comparisonNs']/1e6)
        self.assertTrue(result['rootsEqual'])
        self.assertEqual(len(result['batchChecks']),40)
        # Simulate disk corruption of two different GRID-owned originals, retaining all cached roots.
        for index in (0, 39):
            bid = group['batches'][index]['batchId']
            row = self.grid.store.get('batch', bid)
            payload = row['payload']
            payload['batch']['records'][0]['value'] = 999.123
            with self.grid.store.connect() as conn:
                conn.execute('UPDATE objects SET input=? WHERE kind=? AND id=?', (json.dumps(payload), 'batch', bid))
        result = self.grid.verify_group(group['groupId'])
        self.assertEqual(result['status'], 'FAIL')
        self.assertEqual(result['recordCount'], 12000)
        self.assertEqual(result['batchCount'], 40)
        self.assertTrue({group['batches'][i]['batchId'] for i in (0, 39)}.issubset({f['batchId'] for f in result['failures']}))
        self.assertEqual(len(self.grid.store.all('verification')), 2)

    def test_root_comparison_clock_excludes_tree_build_and_chain_query(self):
        from src.pipeline.grid import compare_roots
        with patch('src.pipeline.grid.time.perf_counter_ns',side_effect=[1000,1250]) as clock:
            result=compare_roots('abc','abc')
        self.assertEqual(clock.call_count,2)
        self.assertEqual(result,{'rootsEqual':True,'comparisonNs':250,'comparisonMs':.00025})
        with patch('src.pipeline.grid.time.perf_counter_ns') as clock:
            self.assertIsNone(compare_roots(None,'abc')['comparisonNs'])
            clock.assert_not_called()

    def test_detail_exposes_grid_originals_identity_signature_and_group_manifest(self):
        body=self.batch(1)
        self.grid.receive_batch(body)
        detail=self.grid.batch(body['batch']['batchId'])
        self.assertEqual(detail['records'],body['batch']['records'])
        self.assertEqual(detail['signature'],body['signature'])
        self.assertEqual(len(detail['deviceOrigins']),300)
        self.assertEqual(set(detail['deviceOrigins'].values()),{e['edgeId'] for e in self.cfg['edges'][:10]})
        self.assertNotIn('records',self.grid.batch(body['batch']['batchId'],False))

    def test_group_binds_order_identity_count_and_session(self):
        items = [descriptor(self.batch(i)['batch']) for i in range(1, 41)]
        root = group_root(items)
        for mutate in (lambda x: x.reverse(), lambda x: x[0].update(vppId='vpp-b'),
                       lambda x: x[0].update(sessionId='OTHER'), lambda x: x[0].update(recordCount=299),
                       lambda x: x.pop(), lambda x: x[1].update(batchId=x[0]['batchId'])):
            bad = copy.deepcopy(items)
            mutate(bad)
            with self.assertRaises(ProtocolError):
                group_root(bad)
        items[2]['rootHash'] = '0'*64
        self.assertNotEqual(root, group_root(items))

    def test_store_replay_conflict_and_summary_omit(self):
        store = Store(Path(self.tmp.name)/'store.db')
        self.assertTrue(store.insert('x','id',{'records':[1,2],'nested':{'records':[3]}}))
        self.assertFalse(store.insert('x','id',{'records':[1,2],'nested':{'records':[3]}}))
        with self.assertRaisesRegex(ProtocolError,'different original'):
            store.insert('x','id',{'records':[1]})
        row = store.all('x',('records','nested.records'))[0]
        self.assertEqual(row['payload'],{'nested':{}})
        self.assertEqual(store.get('x','id')['payload']['records'],[1,2])

    def test_edge_same_round_reuses_immutable_packet_and_pause_has_no_worker(self):
        edge = EdgeRuntime(self.cfg, self.cfg['edges'][0], self.chain)
        with patch.object(edge,'deliver',lambda pid: edge.packet(pid)):
            a = edge.collect_once(dict(sessionId='TEST', sequence=1))
            b = edge.collect_once(dict(sessionId='TEST', sequence=1))
        self.assertEqual(a['packet']['packetId'],b['packet']['packetId'])
        self.assertEqual(len(a['packet']['records']),30)
        edge.start(dict(sessionId='FUTURE',startMs=int(time.time()*1000)+60000))
        worker=edge.thread
        edge.start(dict(sessionId='FUTURE',startMs=int(time.time()*1000)+60000))
        self.assertIs(edge.thread,worker)
        edge.pause_collection()
        self.assertFalse(edge.collection_running)
        self.assertFalse(worker.is_alive())

    def test_ten_edge_packets_form_one_300_batch_and_duplicate_does_not_rebuild(self):
        spec=self.cfg['vpps'][0]
        vpp=VppRuntime(self.cfg,spec,self.chain)
        def delivered(bid):
            vpp.store.update('batch',bid,status='DELIVERED')
        with patch.object(vpp,'send_batch',delivered):
            for edge_spec in self.cfg['edges'][:10]:
                edge=EdgeRuntime(self.cfg,edge_spec,self.chain)
                with patch.object(edge,'deliver',lambda pid: edge.packet(pid)):
                    body=edge.collect_once(dict(sessionId='TEST',sequence=1))
                packet=body['packet']
                anchor=self.chain.submit(packet['batchId'],packet['rootHash'],30,edge_spec['edgeId'],EDGE_SCHEMA)
                input_body=dict(packet=packet,signature=body['signature'],chainAnchor=anchor)
                vpp.edge_packet(input_body)
            vpp.edge_packet(input_body)
        self.assertEqual(len(vpp.store.all('batch')),1)
        batch=vpp.batch(vpp_batch_id('vpp-a','TEST',1))
        self.assertEqual(len(batch['records']),300)
        self.assertEqual(len(batch['edgePackets']),10)
        self.assertEqual(records_root(batch['records']),batch['rootHash'])

    def test_missing_receipt_status_is_not_success(self):
        self.assertFalse(ChainClient._normalise({'transactionHash':'x'}).ok)
        self.assertTrue(ChainClient._normalise({'status':'0x0'}).ok)
        self.assertFalse(ChainClient._normalise({'status':'0x1'}).ok)

    def test_console_auth_roles_and_selected_vpp_proxy(self):
        app=create_app(self.cfg)
        app.testing=True
        client=app.test_client()
        self.assertEqual(client.get('/api/v2/topology').status_code,401)
        self.assertEqual(client.post('/api/auth/login',json={'username':'vpp','password':'wrong'}).status_code,401)
        client.post('/api/auth/login',json={'username':'vpp','password':'vpp-local-password'})
        self.assertEqual(client.get('/api/v2/topology').json['deviceCount'],900)
        self.assertEqual(client.post('/api/v2/grid/groups/g/verify',headers={'X-Prototype-Request':'1'}).status_code,403)
        with patch('src.pipeline.console.requests.request') as req:
            req.return_value=SimpleNamespace(ok=True,json=lambda:{'items':[]})
            client.get('/api/v2/grid/batches?vppId=vpp-c&page=2&pageSize=10')
            self.assertIn('vppId=vpp-c',req.call_args[0][1])
            self.assertIn('page=2',req.call_args[0][1])
            client.get('/api/v2/vpps/vpp-b/edge-packets?page=3')
            self.assertIn(':8202/api/v2/edge-packets?page=3',req.call_args[0][1])


if __name__=='__main__':
    unittest.main()
