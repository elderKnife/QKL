"""30-record edge packets: hash/sign, anchor, then deliver the originals."""
import threading
import time

import requests

from ..edge.service import EdgeRuntime as LegacyEdge
from ..common.config import data_path
from ..common.models import new_id, utc_iso
from .anchors import Anchors
from .protocol import EDGE_SCHEMA, records_root, edge_batch_id, sign, require, identifier, positive
from .store import Store


class EdgeRuntime(LegacyEdge):
    def __init__(self, config, spec, anchors=None):
        super().__init__(config,spec)
        self.anchors = anchors or Anchors(config,spec['chainAccount'])
        self.store = Store(data_path(config,self.edge_id,'pipeline.db'))
        self.job_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = None
        self.session_id = self.store.setting('sessionId')
        self.start_ms = self.store.setting('startMs')
        self.collection_rounds = self.store.setting('lastRound',0)
        self.last_packet_id = self.store.setting('lastPacketId')
        self.last_round_at = self.store.setting('lastRoundAt')

    def collection_state(self):
        return {'edgeId':self.edge_id,'running':self.collection_running,'intervalSeconds':30,
                'deviceCount':30,'rounds':self.collection_rounds,'sessionId':self.session_id,
                'lastRoundAt':self.last_round_at,'lastPacketId':self.last_packet_id,
                'busy':self.job_lock.locked()}

    def start(self, body):
        with self.lock:
            if self.collection_running:
                require(body.get('sessionId',self.session_id)==self.session_id,'edge is already collecting another session','IDEMPOTENCY_CONFLICT')
                return self.collection_state()
            require(not self.thread or not self.thread.is_alive(),'previous collection is stopping; retry shortly','IDEMPOTENCY_CONFLICT')
            self.session_id = identifier(body['sessionId'])
            self.start_ms = positive(body['startMs'])
            if self.store.setting('sessionId') != self.session_id:
                self.collection_rounds = 0
            self.store.set_setting('sessionId',self.session_id)
            self.store.set_setting('startMs',self.start_ms)
            self.collection_running = True
            self.stop_event = threading.Event()
            self.thread = threading.Thread(target=self.loop,daemon=True,name=self.edge_id+'-sampling')
            self.thread.start()
            return self.collection_state()

    def pause_collection(self):
        self.collection_running = False
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=0.1)
        return self.collection_state()

    def loop(self):
        try:
            while not self.stop_event.is_set():
                now_ms = time.time_ns()//1_000_000
                if now_ms < self.start_ms:
                    self.stop_event.wait((self.start_ms-now_ms)/1000)
                    continue
                slot = max(1,(now_ms-self.start_ms)//30000+1)
                if slot > self.collection_rounds and now_ms >= self.start_ms:
                    try:
                        self.collect_once({'sessionId':self.session_id,'sequence':slot})
                    except Exception as exc:
                        self.store.set_setting('lastError',str(exc))
                target = self.start_ms + max(slot,self.collection_rounds)*30000
                self.stop_event.wait(max(0.1,(target-time.time_ns()//1_000_000)/1000))
        finally:
            self.collection_running = False

    def collect_once(self, body=None):
        body = body or {}
        with self.job_lock:
            session = identifier(body.get('sessionId') or self.session_id or new_id('RUN'))
            seq = positive(body.get('sequence',self.collection_rounds+1))
            if self.session_id != session:
                self.session_id = session
                self.store.set_setting('sessionId',session)
                self.collection_rounds = 0
            slot_key = f'{session}:{seq}'
            existing_id = self.store.setting('slot:'+slot_key)
            if existing_id:
                self.collection_rounds = max(self.collection_rounds, seq)
                return self.deliver(existing_id)
            moment = utc_iso()
            self.run_id = session
            records = [{'recordId':f'R-{session}-{device_id}-{seq:06d}',
                        'deviceId':device_id,'deviceType':device_type,'metric':'active_power_kw',
                        'value':self._simulate(device_type,self.device_state[device_id],device_id,seq),
                        'unit':'kW','timestamp':moment}
                       for device_id,device_type in self.devices.items()]
            require(len(records)==30,'edge must have exactly 30 devices')
            packet_id = new_id('PKT2')
            packet = {'schemaVersion':EDGE_SCHEMA,'packetId':packet_id,'batchId':edge_batch_id(packet_id),
                      'edgeId':self.edge_id,'vppId':self.spec['vppId'],'sessionId':session,
                      'sequence':seq,'collectedAt':moment,'recordCount':30,
                      'rootHash':records_root(records),'records':records}
            body = {'packet':packet,'signature':sign(self.signer,packet)}
            self.store.insert('packet',packet_id,body,{'status':'BUILT'})
            self.store.set_setting('slot:'+slot_key,packet_id)
            self.collection_rounds = seq
            self.store.set_setting('lastRound',seq)
            self.last_round_at = moment
            self.last_packet_id = packet_id
            self.store.set_setting('lastPacketId',packet_id)
            self.store.set_setting('lastRoundAt',moment)
            return self.deliver(packet_id)

    def deliver(self, packet_id):
        row = self.store.get('packet',packet_id)
        require(row is not None,'packet not found','NOT_FOUND',packet_id)
        if row['state'].get('status')=='DELIVERED':
            return self.packet(packet_id)
        packet = row['payload']['packet']
        try:
            anchor = row['state'].get('chainAnchor') or self.anchors.submit(packet['batchId'],packet['rootHash'],30,self.edge_id,EDGE_SCHEMA)
            self.store.update('packet',packet_id,chainAnchor=anchor,status='ANCHORED')
            response = requests.post(self.vpp_url+'/api/v1/edge-packets',json={**row['payload'],'chainAnchor':anchor},timeout=120)
            result = response.json()
            self.store.update('packet',packet_id,status='DELIVERED' if response.ok else 'DELIVERY_FAILED',delivery={'httpStatus':response.status_code,'response':result},lastError=None if response.ok else str(result))
        except Exception as exc:
            self.store.update('packet',packet_id,status='PENDING',lastError=str(exc))
            raise
        return self.packet(packet_id)

    def packet(self, packet_id):
        row = self.store.get('packet',packet_id)
        require(row is not None,'packet not found','NOT_FOUND',packet_id)
        return {**row['payload'],**row['state'],'createdAt':row['createdAt']}

    def summaries(self):
        return [{'packetId':r['id'],'batchId':r['payload']['packet']['batchId'],
                 'sequence':r['payload']['packet']['sequence'],'sessionId':r['payload']['packet']['sessionId'],
                 'rootHash':r['payload']['packet']['rootHash'],'recordCount':30,
                 **r['state'],'createdAt':r['createdAt']} for r in self.store.all('packet',('packet.records',))]

    def state(self):
        summaries = self.summaries()
        latest = summaries[-1] if summaries else None
        return {'edgeId':self.edge_id,'vppId':self.spec['vppId'],'deviceCount':30,
                'devices':list(self.devices),'deviceState':self.device_state,
                'collection':self.collection_state(),'blockchain':'EDGE ROOT ANCHOR V2',
                'chainAccount':self.config['subjects'].get(self.edge_id),
                'lastError':self.store.setting('lastError'),
                'latest':latest,'packetCount':len(summaries)}
