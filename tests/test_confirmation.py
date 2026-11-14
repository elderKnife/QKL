"""Commit acknowledgement timing must require matching committed chain state."""
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.chain.client import ChainClient, SubmittedTransaction
from src.chain.registry import ChainBatch
from src.common.models import ProtocolError
from src.pipeline.anchors import Anchors
from src.pipeline.protocol import BATCH_SCHEMA


class ConfirmationTests(unittest.TestCase):
    def anchors(self):
        anchor=Anchors.__new__(Anchors)
        anchor.config={'subjects':{'vpp-a':'0x'+'a'*40},'chain':{'confirmation':{'pollIntervalMs':20,'timeoutSeconds':.1}}}
        anchor.registry=Mock(address='0x'+'b'*40,abi=[])
        anchor.client=Mock()
        anchor.client.call.return_value=['0x'+'a'*40]
        anchor.registry.broadcast_batch.return_value=SubmittedTransaction('0x'+'1'*64,1000,1100)
        anchor.parser=Mock()
        return anchor

    def test_acknowledgement_is_not_success_poll_until_matching_chain_state(self):
        anchor=self.anchors()
        committed=ChainBatch('ID','c'*64,300,1,'vpp-a',BATCH_SCHEMA,True)
        anchor.registry.get_batch.side_effect=[None,None,committed]
        anchor.client.get_receipt.return_value=None
        with patch('src.pipeline.anchors.time.sleep') as sleep, patch('src.pipeline.anchors.time.perf_counter_ns',return_value=2000):
            result=anchor.submit('ID','c'*64,300,'vpp-a',BATCH_SCHEMA)
        sleep.assert_called_once()
        self.assertEqual(result['commitLatencyMs'],.001)
        self.assertIsNone(result['receiptLatencyMs'])
        self.assertEqual(result['confirmationMethod'],'CHAIN_STATE')
        self.assertEqual(result['confirmationPollCount'],2)
        anchor.client.get_receipt.assert_called_once_with('0x'+'1'*64)

    def test_wrong_root_or_actual_publisher_cannot_confirm(self):
        for wrong_root,publisher in [('d'*64,'a'*40),('c'*64,'d'*40)]:
            anchor=self.anchors()
            anchor.registry.get_batch.side_effect=[None,ChainBatch('ID',wrong_root,300,1,'vpp-a',BATCH_SCHEMA,True)]
            anchor.client.call.return_value=['0x'+publisher]
            with self.assertRaises(ProtocolError):
                anchor.submit('ID','c'*64,300,'vpp-a',BATCH_SCHEMA)
            anchor.client.get_receipt.assert_not_called()

    def test_pending_or_timed_out_submission_is_not_reported_confirmed(self):
        anchor=self.anchors()
        anchor.registry.get_batch.return_value=None
        with patch('src.pipeline.anchors.time.monotonic',side_effect=[0,1]):
            with self.assertRaisesRegex(ProtocolError,'no matching committed root') as result:
                anchor.submit('ID','c'*64,300,'vpp-a',BATCH_SCHEMA)
        self.assertEqual(result.exception.code,'TX_OUTCOME_UNKNOWN')

    def test_receipt_fetch_is_after_commit_clock_and_validated_when_present(self):
        anchor=self.anchors()
        anchor.registry.get_batch.side_effect=[None,ChainBatch('ID','c'*64,300,1,'vpp-a',BATCH_SCHEMA,True)]
        anchor.client.get_receipt.return_value={'status':'0x0'}
        anchor.receipt=Mock(return_value={'status':'PASS','blockNumber':9})
        with patch('src.pipeline.anchors.time.perf_counter_ns',side_effect=[2000,9000]):
            result=anchor.submit('ID','c'*64,300,'vpp-a',BATCH_SCHEMA)
        self.assertEqual(result['commitLatencyMs'],.001)
        self.assertEqual(result['receiptLatencyMs'],.008)
        self.assertEqual(result['blockNumber'],9)
        anchor.receipt.assert_called_once()

    def test_recovered_history_does_not_invent_new_timing(self):
        anchor=self.anchors()
        anchor.registry.get_batch.return_value=ChainBatch('ID','c'*64,300,1,'vpp-a',BATCH_SCHEMA,True)
        result=anchor.submit('ID','c'*64,300,'vpp-a',BATCH_SCHEMA)
        self.assertIsNone(result['commitLatencyMs'])
        self.assertTrue(result['recovered'])
        anchor.registry.broadcast_batch.assert_not_called()

    def test_receiver_confirms_committed_root_even_when_receipt_unavailable(self):
        anchor=self.anchors()
        anchor.registry.get_batch.return_value=ChainBatch('ID','c'*64,300,1,'vpp-a',BATCH_SCHEMA,True)
        anchor.client.get_receipt.side_effect=ProtocolError('CHAIN_UNAVAILABLE','receipt temporarily unavailable')
        evidence={'registryAddress':anchor.address,'txHash':'0x'+'1'*64,'confirmationMethod':'CHAIN_STATE'}
        chain,receipt=anchor.confirm(evidence,'ID','c'*64,300,'vpp-a',BATCH_SCHEMA)
        self.assertEqual(chain['rootHash'],'c'*64)
        self.assertEqual(receipt['status'],'NOT_AVAILABLE')
        # A chain-state label supplied over HTTP does not bypass independent chain checks.
        with self.assertRaises(ProtocolError):
            anchor.confirm(evidence,'ID','d'*64,300,'vpp-a',BATCH_SCHEMA)

    def test_available_reverted_or_other_transaction_receipt_is_still_rejected(self):
        anchor=self.anchors()
        anchor.registry.get_batch.return_value=ChainBatch('ID','c'*64,300,1,'vpp-a',BATCH_SCHEMA,True)
        evidence={'registryAddress':anchor.address,'txHash':'0x'+'1'*64,'confirmationMethod':'CHAIN_STATE'}
        anchor.client.get_receipt.return_value={'transactionHash':'0x'+'1'*64,'status':'0x1'}
        with self.assertRaisesRegex(ProtocolError,'execution failed'):
            anchor.confirm(evidence,'ID','c'*64,300,'vpp-a',BATCH_SCHEMA)
        anchor.client.get_receipt.return_value={'transactionHash':'0x'+'2'*64,'status':'0x0'}
        with self.assertRaisesRegex(ProtocolError,'receipt not found'):
            anchor.confirm(evidence,'ID','c'*64,300,'vpp-a',BATCH_SCHEMA)

    def test_sdk_broadcast_returns_only_submission_acknowledgement(self):
        client=ChainClient.__new__(ChainClient)
        client._lock=threading.RLock()
        client._client=Mock()
        client._client.sendRawTransaction.return_value='0x'+'1'*64
        with patch('src.chain.client.time.perf_counter_ns',side_effect=[100,350]):
            result=client.broadcast('contract',[],'registerBatch',[])
        self.assertEqual(result.broadcast_latency_ms,.00025)
        self.assertFalse(hasattr(result,'ok'))
        client._client.sendRawTransactionGetReceipt.assert_not_called()
        client._client.sendRawTransaction.return_value='pending'
        with self.assertRaises(ProtocolError):
            client.broadcast('contract',[],'registerBatch',[])


if __name__=='__main__':
    unittest.main()
