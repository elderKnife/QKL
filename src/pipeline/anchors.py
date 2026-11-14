"""Root-only FISCO anchoring with signer binding and receipt event checks."""
import copy
import time

from ..chain.factory import build_registry
from ..chain.client import DatatypeParser
from ..common.models import ProtocolError, utc_now_ms
from .protocol import require


class Anchors:
    def __init__(self, config, role, rpc_node=None):
        self.config = config
        self.client, self.registry = build_registry(config,role,rpc_node)
        self.parser = DatatypeParser()
        self.parser.set_abi(self.registry.abi)

    @property
    def address(self):
        return self.registry.address

    def read(self, batch_id, root=None, count=None, subject=None, schema=None, *, observed_batch=None):
        batch = observed_batch if observed_batch is not None else self.registry.get_batch(batch_id)
        require(batch is not None,'batch is not on chain','ROOT_MISMATCH',batch_id)
        info = batch.to_dict()
        publisher = self.client.call(self.address,self.registry.abi,'getPublisher',[batch_id])[0]
        info['publisher'] = str(publisher)
        checks = (('rootHash',root),('recordCount',count),('vppId',subject),('schemaVersion',schema))
        for field, expected in checks:
            if expected is not None:
                require(info[field]==expected,f'on-chain {field} mismatch','ROOT_MISMATCH',batch_id)
        expected_address = self.config['subjects'].get(subject or info['vppId'])
        require(expected_address and str(publisher).lower()==expected_address.lower(),'on-chain publisher mismatch','IDENTITY_NOT_ALLOWED',batch_id)
        return info

    def receipt(self, anchor, batch_id, root, count, subject, schema, *, raw_receipt=None):
        tx_hash = anchor.get('txHash')
        if not tx_hash:
            require(anchor.get('recovered') is True,'missing transaction hash','TX_OUTCOME_UNKNOWN',batch_id)
            return {'status':'RECOVERED','txHash':None,'blockNumber':None}
        require(isinstance(tx_hash,str) and len(tx_hash)==66 and tx_hash.startswith('0x'), 'invalid transaction hash')
        try:
            raw = raw_receipt if raw_receipt is not None else self.client.get_receipt(tx_hash)
        except ProtocolError:
            if anchor.get('confirmationMethod')=='CHAIN_STATE':
                return {'status':'NOT_AVAILABLE','txHash':tx_hash,'blockNumber':None,
                        'note':'committed root independently verified; transaction receipt unavailable'}
            raise
        if raw is None and anchor.get('confirmationMethod')=='CHAIN_STATE':
            return {'status':'NOT_AVAILABLE','txHash':tx_hash,'blockNumber':None,
                    'note':'committed root independently verified; transaction receipt not yet returned'}
        require(isinstance(raw,dict) and raw.get('transactionHash','').lower()==tx_hash.lower(),'receipt not found','TX_OUTCOME_UNKNOWN',batch_id)
        status = raw.get('status')
        try:
            status_code = int(status,16) if isinstance(status,str) else int(status) if type(status) is int else -1
        except ValueError:
            status_code = -1
        require(status_code==0,'transaction execution failed','ROOT_MISMATCH',batch_id)
        require(str(raw.get('to','')).lower()==self.address.lower(),'receipt targets another contract','ROOT_MISMATCH',batch_id)
        require(str(raw.get('from','')).lower()==self.config['subjects'][subject].lower(),'receipt sender mismatch','IDENTITY_NOT_ALLOWED',batch_id)
        events = self.parser.parse_event_logs(copy.deepcopy(raw.get('logs',[])))
        matched = False
        for event in events:
            if event.get('eventname')!='BatchRegistered':
                continue
            args = event['eventdata']
            event_root = args[1].hex() if isinstance(args[1],bytes) else str(args[1]).removeprefix('0x')
            if args[0]==batch_id and event_root==root and int(args[2])==count and args[4]==subject and args[5]==schema and str(args[6]).lower()==self.config['subjects'][subject].lower():
                matched = True
        require(matched,'receipt event does not match this batch','ROOT_MISMATCH',batch_id)
        block = raw.get('blockNumber')
        block = int(block,16) if isinstance(block,str) else block
        require(type(block) is int,'missing transaction block','TX_OUTCOME_UNKNOWN',batch_id)
        return {'status':'PASS','txHash':tx_hash,'blockNumber':block,'receiptStatus':status,'from':raw.get('from'),'to':raw.get('to')}

    def confirm(self, anchor, batch_id, root, count, subject, schema):
        require(isinstance(anchor,dict) and anchor.get('registryAddress')==self.address,'unexpected registry address','ROOT_MISMATCH',batch_id)
        chain = self.read(batch_id,root,count,subject,schema)
        receipt = self.receipt(anchor,batch_id,root,count,subject,schema)
        return chain,receipt

    def submit(self, batch_id, root, count, subject, schema):
        recovered = {'status':'CONFIRMED','txHash':None,'blockNumber':None,
                     'registryAddress':self.address,'receiptLatencyMs':None,
                     'commitLatencyMs':None,'broadcastLatencyMs':None,'recovered':True,
                     'confirmationMethod':'RECOVERED_CHAIN_STATE','timingVersion':'CHAIN-STATE-V1'}
        existing = self.registry.get_batch(batch_id)
        if existing:
            self.read(batch_id,root,count,subject,schema)
            return recovered
        try:
            submitted = self.registry.broadcast_batch(batch_id,root,count,utc_now_ms(),subject,schema)
        except ProtocolError:
            existing = self.registry.get_batch(batch_id)
            if existing:
                self.read(batch_id,root,count,subject,schema)
                return recovered
            raise ProtocolError('TX_OUTCOME_UNKNOWN','submission outcome unknown; retry with the same batchId',object_id=batch_id,retryable=True)

        # FISCO eth_call reads committed state. A returned tx hash or a tx-pool
        # entry alone never satisfies this check. Polling granularity is real
        # measurement overhead and is explicitly included in commitLatencyMs.
        options = self.config.get('chain',{}).get('confirmation',{})
        interval = max(.005, float(options.get('pollIntervalMs',20))/1000)
        timeout = max(.1, float(options.get('timeoutSeconds',15)))
        deadline = time.monotonic()+timeout
        polls = 0
        while True:
            batch = self.registry.get_batch(batch_id)
            polls += 1
            if batch is not None:
                self.read(batch_id,root,count,subject,schema,observed_batch=batch)
                confirmed_ns = time.perf_counter_ns()
                break
            remaining = deadline-time.monotonic()
            if remaining<=0:
                raise ProtocolError('TX_OUTCOME_UNKNOWN',
                                    f'no matching committed root before timeout; txHash={submitted.tx_hash}; retry with the same batchId',
                                    object_id=batch_id,retryable=True)
            time.sleep(min(interval,remaining))

        anchor = {'status':'CONFIRMED','txHash':submitted.tx_hash,'blockNumber':None,
                  'registryAddress':self.address,'receiptLatencyMs':None,'recovered':False,
                  'commitLatencyMs':(confirmed_ns-submitted.started_ns)/1e6,
                  'broadcastLatencyMs':submitted.broadcast_latency_ms,
                  'confirmationMethod':'CHAIN_STATE','timingVersion':'CHAIN-STATE-V1',
                  'confirmationPollIntervalMs':interval*1000,'confirmationPollCount':polls}
        # Additional receipt evidence is acquired after stopping the on-chain
        # confirmation clock. Missing receipts do not downgrade committed state.
        try:
            raw = self.client.get_receipt(submitted.tx_hash)
        except ProtocolError:
            raw = None
        if raw is not None:
            anchor['receiptLatencyMs']=(time.perf_counter_ns()-submitted.started_ns)/1e6
            proof = self.receipt(anchor,batch_id,root,count,subject,schema,raw_receipt=raw)
            anchor['blockNumber']=proof['blockNumber']
            anchor['receiptStatus']=proof['status']
        else:
            anchor['receiptStatus']='NOT_AVAILABLE'
        return anchor
