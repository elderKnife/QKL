"""V2 downlink verification tests use a chain double; real FISCO is checked separately."""
import copy
import re
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tests.test_pipeline import FakeAnchors
from src.common.config import load_config, resolve_path
from src.common.crypto import Signer
from src.common.models import ProtocolError
from src.pipeline.apps import grid_app, vpp_app
from src.pipeline.command_protocol import command_root
from src.pipeline.console import create_app
from src.pipeline.protocol import sign
from src.pipeline.vpp import VppRuntime
from src.pipeline.grid import GridRuntime


class CommandPipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = copy.deepcopy(load_config())
        self.cfg['pipeline']['dataRoot'] = self.tmp.name
        self.chain = FakeAnchors()
        self.grid = GridRuntime(self.cfg, self.chain)
        self.signers = {v['vppId']: Signer.from_file(resolve_path(v['signingKey']), v['keyId']) for v in self.cfg['vpps']}
        self.vpps = {spec['vppId']: VppRuntime(self.cfg, spec, self.chain) for spec in self.cfg['vpps']}
        self.sent = []

    def tearDown(self):
        self.tmp.cleanup()

    def route(self, url, json, timeout):
        port = int(re.search(r':(\d+)/', url).group(1))
        spec = next(v for v in self.cfg['vpps'] if v['port'] == port)
        self.sent.append((url, copy.deepcopy(json)))
        result = self.vpps[spec['vppId']].receive_commands(json)
        return SimpleNamespace(status_code=200, json=lambda: result)

    def target(self, edge_index=0, device_index=0):
        edge = self.cfg['edges'][edge_index]
        return {'vppId': edge['vppId'], 'edgeId': edge['edgeId'], 'deviceId': edge['devices'][device_index]['deviceId']}

    def release(self, mode='SINGLE', targets=None):
        with patch('src.pipeline.command_runtime.requests.post', side_effect=self.route):
            return self.grid.release_commands({'mode': mode, 'targets': targets or [self.target()]})

    def test_command_returns_signed_verified_receipt_and_never_contacts_edge(self):
        result = self.release()
        self.assertEqual(result['status'], 'VERIFIED')
        self.assertEqual(result['verifiedVppCount'], 1)
        self.assertEqual(result['commandCount'], 1)
        self.assertRegex(result['issuedAt'], r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$')
        self.assertRegex(result['allVerifiedAt'], r'\.\d{3}Z$')
        dispatch = result['dispatches'][0]
        self.assertEqual(dispatch['receipt']['status'], 'VERIFIED')
        self.assertEqual(dispatch['receipt']['rootHash'], dispatch['rootHash'])
        self.assertEqual(dispatch['receipt']['chainRootHash'], dispatch['rootHash'])
        self.assertEqual(dispatch['receipt']['comparisonMs'], dispatch['receipt']['comparisonNs'] / 1e6)
        self.assertEqual(dispatch['receipt']['verificationMs'], dispatch['receipt']['comparisonMs'])
        self.assertGreater(dispatch['receipt']['totalVerificationMs'], dispatch['receipt']['verificationMs'])
        self.assertEqual(dispatch['receipt']['primaryTimingScope'], 'ROOT_EQUALITY_ONLY')
        self.assertEqual(result['verificationMs'], dispatch['receipt']['comparisonMs'])
        self.assertGreater(dispatch['publishToReceiptMs'], 0)
        self.assertGreaterEqual(result['publicationTotalMs'], result['publishToReceiptMs'])
        self.assertGreaterEqual(result['publishToReceiptMs'], dispatch['deliveryMs'])
        self.assertEqual(len(self.sent), 1)
        self.assertTrue(self.sent[0][0].endswith(':8201/api/v2/commands'))
        self.assertEqual(dispatch['commands'][0]['params'], {'activePowerKw': 10.0})
        self.assertTrue(dispatch['commands'][0]['simulation'])
        self.assertEqual(self.chain.values[dispatch['dispatchId']]['vppId'], 'grid')
        self.assertNotIn('commands', self.chain.values[dispatch['dispatchId']])
        self.assertEqual(len(self.vpps['vpp-a'].store.all('command')), 1)
        self.assertEqual(self.grid.command_release(result['releaseId'])['dispatches'], result['dispatches'])

    def test_bulk_from_multiple_edges_in_one_vpp_has_one_root_and_one_success(self):
        targets = [self.target(), self.target(0, 1), self.target(1), self.target(2), self.target(9)]
        result = self.release('BULK', targets)
        self.assertEqual((result['status'], result['vppCount'], result['commandCount']), ('VERIFIED', 1, 5))
        self.assertEqual(result['vppId'], 'vpp-a')
        self.assertEqual([d['recordCount'] for d in result['dispatches']], [5])
        self.assertEqual(len(self.chain.values), 1)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(result['verificationMs'], result['dispatches'][0]['receipt']['comparisonMs'])
        self.assertTrue(all(d['rootHash'] == command_root(d['commands']) for d in result['dispatches']))
        self.assertEqual(self.grid.command_releases()[0]['status'], 'VERIFIED')

    def test_bulk_all_300_devices_in_one_vpp_still_commits_one_root(self):
        targets = [self.target(edge, device) for edge in range(10) for device in range(30)]
        result = self.release('BULK', targets)
        self.assertEqual(result['status'], 'VERIFIED')
        self.assertEqual((result['commandCount'], len(result['dispatches'])), (300, 1))
        self.assertEqual(len(self.chain.values), 1)
        self.assertEqual(len(self.sent), 1)

    def test_invalid_targets_are_rejected_before_anchoring(self):
        target = self.target()
        for body in [
            {'mode': 'SINGLE', 'targets': []},
            {'mode': 'SINGLE', 'targets': [target, self.target(1)]},
            {'mode': 'BULK', 'targets': [target, target]},
            {'mode': 'BULK', 'targets': [target, self.target(10)]},
            {'mode': 'BULK', 'targets': [target] * 301},
            {'mode': 'SINGLE', 'targets': [{**target, 'vppId': 'vpp-b'}]},
            {'mode': 'SINGLE', 'targets': [{**target, 'deviceId': self.target(1)['deviceId']}]},
        ]:
            with self.assertRaises(ProtocolError):
                self.grid.release_commands(body)
        self.assertEqual(self.chain.values, {})
        self.assertEqual(self.grid.command_releases(), [])

    def test_verification_clock_counts_only_final_equality(self):
        self.release()
        body = self.sent[0][1]
        # Hashing takes 2 ms and querying takes 5 ms; equality takes only 250 ns.
        with patch('src.pipeline.command_runtime.time.perf_counter_ns',
                   side_effect=[0, 10, 2_000_010, 3_000_010, 8_000_010, 9_000_010, 9_000_260, 10_000_000]):
            receipt = self.vpps['vpp-a'].receive_commands(body)['receipt']
        self.assertEqual(receipt['hashComputeMs'], 2)
        self.assertEqual(receipt['chainQueryMs'], 5)
        self.assertEqual(receipt['totalVerificationMs'], 10)
        self.assertEqual(receipt['comparisonNs'], 250)
        self.assertEqual(receipt['verificationMs'], .00025)
        self.assertEqual(receipt['verificationMs'], receipt['comparisonMs'])

    def test_content_signature_target_and_independent_chain_checks_cannot_be_skipped(self):
        self.release()
        body = self.sent[0][1]
        vpp = self.vpps['vpp-a']
        with self.assertRaises(ProtocolError):
            self.vpps['vpp-b'].receive_commands(body)
        altered = copy.deepcopy(body)
        altered['envelope']['commands'][0]['params']['activePowerKw'] = 999.0
        with self.assertRaises(ProtocolError):
            vpp.receive_commands(altered)
        altered['signature'] = sign(self.signers['vpp-a'], altered['envelope'])
        with self.assertRaises(ProtocolError):
            vpp.receive_commands(altered)
        key = body['envelope']['dispatchId']
        self.chain.values[key]['rootHash'] = '0' * 64
        with self.assertRaisesRegex(ProtocolError, 'independently queried'):
            vpp.receive_commands(body)

    def test_http_200_without_bound_signed_receipt_is_never_success(self):
        for mutate in (lambda r: {}, lambda r: {'receipt': r['receipt']},
                       lambda r: {**r, 'receipt': {**r['receipt'], 'dispatchId': 'CMD2-wrong'}}):
            def route(url, json, timeout):
                response = self.route(url, json, timeout).json()
                return SimpleNamespace(status_code=200, json=lambda: mutate(response))
            with patch('src.pipeline.command_runtime.requests.post', side_effect=route):
                result = self.grid.release_commands({'mode': 'SINGLE', 'targets': [self.target()]})
            self.assertEqual(result['status'], 'ERROR')
            self.assertIsNone(result.get('allVerifiedAt'))
            self.assertNotIn('receivedAt', result['dispatches'][0])

    def test_vpp_failure_does_not_claim_bulk_success(self):
        with patch('src.pipeline.command_runtime.requests.post', return_value=SimpleNamespace(status_code=503)):
            result = self.grid.release_commands({'mode': 'BULK', 'targets': [self.target(), self.target(1)]})
        self.assertEqual(result['status'], 'ERROR')
        self.assertEqual(result['verifiedVppCount'], 0)
        self.assertIsNone(result['allVerifiedAt'])
        self.assertEqual([d['status'] for d in result['dispatches']], ['ERROR'])
        self.assertIsNone(result['verificationMs'])
        self.assertEqual(len(self.chain.values), 1)

    def test_only_grid_account_can_publish_and_http_routes_are_wired(self):
        app = create_app(self.cfg)
        client = app.test_client()
        body = {'mode': 'SINGLE', 'targets': [self.target()]}
        headers = {'X-Prototype-Request': '1'}
        self.assertEqual(client.post('/api/v2/grid/commands', json=body, headers=headers).status_code, 401)
        client.post('/api/auth/login', json={'username': 'vpp', 'password': 'vpp-local-password'})
        self.assertEqual(client.post('/api/v2/grid/commands', json=body, headers=headers).status_code, 403)
        client.post('/api/auth/login', json={'username': 'grid', 'password': 'grid-local-password'})
        with patch('src.pipeline.console.requests.request') as remote:
            remote.return_value = SimpleNamespace(ok=True, json=lambda: {'status': 'VERIFIED'})
            self.assertEqual(client.post('/api/v2/grid/commands', json=body, headers=headers).status_code, 200)
            self.assertEqual(remote.call_args.kwargs['json'], body)
        with patch('src.pipeline.grid.GridRuntime', return_value=self.grid):
            grid_client = grid_app(self.cfg).test_client()
        with patch('src.pipeline.vpp.VppRuntime', return_value=self.vpps['vpp-a']):
            vpp_client = vpp_app(self.cfg, self.cfg['vpps'][0]).test_client()
        def route(url, json, timeout):
            result = vpp_client.post('/api/v2/commands', json=json)
            return SimpleNamespace(status_code=result.status_code, json=lambda: result.json)
        with patch('src.pipeline.command_runtime.requests.post', side_effect=route):
            result = grid_client.post('/api/v2/commands', json=body)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json['status'], 'VERIFIED')
        self.assertEqual(grid_client.get('/api/v2/commands').json['total'], 1)
        self.assertEqual(len(grid_client.get('/api/v2/command-targets').json['vpps']), 3)
