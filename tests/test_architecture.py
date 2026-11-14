"""Machine-checked V2 topology and ownership of local root publishers."""
import unittest
from pathlib import Path

from src.common.config import load_config


class V2ArchitectureTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config()

    def test_topology_3_by_10_by_30(self):
        cfg = self.config
        self.assertEqual(len(cfg['vpps']), 3)
        self.assertEqual(len(cfg['edges']), 30)
        devices = [d['deviceId'] for e in cfg['edges'] for d in e['devices']]
        self.assertEqual(len(devices), 900)
        self.assertEqual(len(set(devices)), 900)
        for vpp in cfg['vpps']:
            edges = [e for e in cfg['edges'] if e['vppId'] == vpp['vppId']]
            self.assertEqual(len(edges), 10)
            self.assertEqual(set(vpp['trustedEdges']), {e['edgeId'] for e in edges})
            self.assertTrue(all(len(e['devices']) == 30 for e in edges))
            self.assertTrue(all(e['intervalSeconds'] == 30 for e in edges))

    def test_all_publishers_have_distinct_chain_identities(self):
        cfg = self.config
        roles = [v['chainAccount'] for v in cfg['vpps']] + [e['chainAccount'] for e in cfg['edges']]
        self.assertEqual(len(roles), len(set(roles)))
        self.assertTrue(all(role in cfg['accounts'] for role in roles))
        self.assertEqual(len(set(a.lower() for a in cfg['subjects'].values())), 34)
        for module in ('edge', 'vpp', 'grid'):
            self.assertIn('.anchors import Anchors', (Path(__file__).parents[1]/'src/pipeline'/f'{module}.py').read_text())

    def test_legacy_history_and_v2_storage_are_separate(self):
        cfg = self.config
        self.assertEqual(cfg['pipeline']['version'], 2)
        self.assertEqual(cfg['pipeline']['dataRoot'], 'data/v2')
        self.assertEqual(cfg['chain']['evidenceFile'], 'config/chain-v2.json')
        self.assertEqual(cfg['chain']['contract']['name'], 'RootHashRegistryV2')
        self.assertTrue((Path(__file__).parents[1]/'config/legacy/system.json').exists())


if __name__ == '__main__':
    unittest.main()
