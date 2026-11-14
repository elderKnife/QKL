"""Independent edge checks, one 300-record batch/round, one group/40 batches."""
import threading
import time

import requests

from ..common.config import data_path, resolve_path
from ..common.crypto import Signer
from ..common.models import utc_iso, new_id, ProtocolError, validate_record
from .anchors import Anchors
from .protocol import (EDGE_SCHEMA,BATCH_SCHEMA,GROUP_SCHEMA,require,identifier,positive,
                       records_root,verify_signature,sign,descriptor,group_root,vpp_batch_id,group_id,edge_batch_id)
from .store import Store
from .command_runtime import VppCommands


class VppRuntime(VppCommands):
    def __init__(self,config,spec,anchors=None):
        self.config,self.spec = config,spec
        self.vpp_id = spec['vppId']
        self.edges = {e['edgeId']:e for e in config['edges'] if e['vppId']==self.vpp_id}
        self.signer = Signer.from_file(resolve_path(spec['signingKey']),spec['keyId'])
        self.anchors = anchors or Anchors(config,spec['chainAccount'])
        self.store = Store(data_path(config,self.vpp_id,'pipeline.db'))
        self.lock = threading.RLock()
        previous=self.store.setting('collection')
        if previous:
            previous['running']=False
            self.store.set_setting('collection',previous)

    def edge_packet(self,body):
        started = time.perf_counter_ns()
        require(isinstance(body,dict) and isinstance(body.get('packet'),dict),'packet required')
        packet = body['packet']
        require(packet.get('schemaVersion')==EDGE_SCHEMA,'unsupported edge protocol')
        for name in ('packetId','batchId','edgeId','vppId','sessionId'):
            identifier(packet.get(name))
        seq = positive(packet.get('sequence'))
        require(packet['vppId']==self.vpp_id and packet['edgeId'] in self.edges,'wrong VPP or edge','TARGET_MISMATCH')
        require(packet['batchId']==edge_batch_id(packet['packetId']),'edge batchId mismatch')
        edge = self.edges[packet['edgeId']]
        records = packet.get('records')
        require(isinstance(records,list) and len(records)==30 and packet.get('recordCount')==30,'edge packet must contain 30 records')
        allowed = {d['deviceId']:d['deviceType'] for d in edge['devices']}
        require({r.get('deviceId') for r in records if isinstance(r,dict)}==set(allowed),'device roster mismatch','TARGET_MISMATCH')
        for record in records:
            validate_record(record)
            require(allowed[record['deviceId']]==record['deviceType'],'device type mismatch')
        verify_signature(packet,body.get('signature'),self.spec['trustedEdges'][packet['edgeId']])
        recomputed = records_root(records)
        require(recomputed==packet.get('rootHash'),'edge declared root mismatch','ROOT_MISMATCH')
        chain,receipt = self.anchors.confirm(body.get('chainAnchor'),packet['batchId'],recomputed,30,packet['edgeId'],EDGE_SCHEMA)
        verification = {'status':'VERIFIED','rootHash':recomputed,'chainRootHash':chain['rootHash'],
                        'chainReceipt':receipt,'durationMs':(time.perf_counter_ns()-started)/1e6,'verifiedAt':utc_iso()}
        with self.lock:
            slot = f"{packet['sessionId']}:{seq}:{packet['edgeId']}"
            existing = self.store.setting('edge-slot:'+slot)
            require(existing is None or existing==packet['packetId'],'another packet already occupies this edge round','IDEMPOTENCY_CONFLICT')
            self.store.insert('edge',packet['packetId'],{'packet':packet,'signature':body['signature']},
                              {'status':'VERIFIED','verification':verification,'chainAnchor':body['chainAnchor']})
            self.store.set_setting('edge-slot:'+slot,packet['packetId'])
            result = self.try_round(packet['sessionId'],seq)
        return {'packetId':packet['packetId'],'status':'VERIFIED','verification':verification,'round':result}

    def try_round(self,session,seq):
        packets=[]
        missing=[]
        for edge_id in sorted(self.edges):
            pid=self.store.setting(f'edge-slot:{session}:{seq}:{edge_id}')
            row=self.store.get('edge',pid) if pid else None
            if row is None:
                missing.append(edge_id)
            else:
                packets.append(row)
        if missing:
            return {'status':'WAITING_EDGES','sequence':seq,'receivedEdges':len(packets),'missingEdges':missing}
        batch_id=vpp_batch_id(self.vpp_id,session,seq)
        existing=self.store.get('batch',batch_id)
        if existing is None:
            records=[record for row in packets for record in row['payload']['packet']['records']]
            require(len(records)==300,'VPP batch requires 300 records')
            batch={'schemaVersion':BATCH_SCHEMA,'batchId':batch_id,'vppId':self.vpp_id,'sessionId':session,
                   'sequence':seq,'recordCount':300,'rootHash':records_root(records),'records':records,
                   'edgePackets':[{'edgeId':r['payload']['packet']['edgeId'],'packetId':r['id'],
                                   'batchId':r['payload']['packet']['batchId'],'rootHash':r['payload']['packet']['rootHash'],
                                   'chainAnchor':r['state']['chainAnchor'],'verification':r['state']['verification']} for r in packets],
                   'builtAt':utc_iso()}
            self.store.insert('batch',batch_id,batch,{'status':'BUILT'})
        self.send_batch(batch_id)
        self.try_group(session,seq)
        return self.batch(batch_id,False)

    def send_batch(self,batch_id):
        row=self.store.get('batch',batch_id)
        if row['state'].get('status')=='DELIVERED':
            return
        batch=row['payload']
        try:
            anchor=row['state'].get('chainAnchor') or self.anchors.submit(batch_id,batch['rootHash'],300,self.vpp_id,BATCH_SCHEMA)
            self.store.update('batch',batch_id,chainAnchor=anchor,status='ANCHORED')
            response=requests.post(self.spec['gridUrl']+'/api/v2/batches',
                                   json={'batch':batch,'signature':sign(self.signer,batch),'chainAnchor':anchor},timeout=120)
            delivered=200<=response.status_code<300
            self.store.update('batch',batch_id,status='DELIVERED' if delivered else 'GRID_REJECTED',
                              delivery={'httpStatus':response.status_code},
                              lastError=None if delivered else f'GRID HTTP {response.status_code}')
        except Exception as exc:
            self.store.update('batch',batch_id,status='PENDING',lastError=str(exc))
            raise

    def try_group(self,session,seq):
        start=(seq-1)//40*40+1
        batches=[self.store.get('batch',vpp_batch_id(self.vpp_id,session,i)) for i in range(start,start+40)]
        if any(b is None or b['state'].get('status')!='DELIVERED' for b in batches):
            return None
        gid=group_id(self.vpp_id,session,seq)
        row=self.store.get('group',gid)
        if row and row['state'].get('status')=='DELIVERED':
            return self.group(gid)
        if row:
            group=row['payload']
        else:
            items=[descriptor(b['payload']) for b in batches]
            group={'schemaVersion':GROUP_SCHEMA,'groupId':gid,'vppId':self.vpp_id,'sessionId':session,
                   'startSequence':start,'endSequence':start+39,'batchCount':40,'recordCount':12000,
                   'rootHash':group_root(items),'batches':items,'builtAt':utc_iso()}
            self.store.insert('group',gid,group,{'status':'BUILT'})
        row=self.store.get('group',gid)
        try:
            anchor=row['state'].get('chainAnchor') or self.anchors.submit(gid,group['rootHash'],12000,self.vpp_id,GROUP_SCHEMA)
            self.store.update('group',gid,chainAnchor=anchor,status='ANCHORED')
            response=requests.post(self.spec['gridUrl']+'/api/v2/groups',json={'group':group,'signature':sign(self.signer,group),'chainAnchor':anchor},timeout=60)
            delivered=200<=response.status_code<300
            self.store.update('group',gid,status='DELIVERED' if delivered else 'GRID_REJECTED',
                              delivery={'httpStatus':response.status_code},
                              lastError=None if delivered else f'GRID HTTP {response.status_code}')
        except Exception as exc:
            self.store.update('group',gid,status='PENDING',lastError=str(exc))
            raise
        return self.group(gid)

    def batch(self,key,include_records=True):
        row=self.store.get('batch',key)
        require(row is not None,'batch not found','NOT_FOUND',key)
        return {**{k:v for k,v in row['payload'].items() if include_records or k not in ('records','edgePackets')},**row['state'],'createdAt':row['createdAt']}

    def group(self,key):
        row=self.store.get('group',key)
        require(row is not None,'group not found','NOT_FOUND',key)
        return {**row['payload'],**row['state'],'createdAt':row['createdAt']}

    def state(self):
        edges=self.store.all('edge',('packet.records',))
        batches=self.store.all('batch',('records','edgePackets'))
        groups=self.store.all('group')
        return {'vppId':self.vpp_id,'edgeCount':10,'deviceCount':300,'receivedEdgePackets':len(edges),
                'batchCount':len(batches),'groupCount':len(groups),'rpcUrl':self.anchors.client.rpc_url,
                'registryAddress':self.anchors.address,'session':self.store.setting('collection'),
                'latestBatch':self.batch(batches[-1]['id'],False) if batches else None,
                'latestGroup':self.group(groups[-1]['id']) if groups else None,
                'groupProgress':sum(b['state'].get('status')=='DELIVERED' for b in batches if b['payload']['sessionId']==(self.store.setting('collection') or {}).get('sessionId'))%40}

    def collection(self,operation):
        from concurrent.futures import ThreadPoolExecutor
        require(operation in ('start','pause','tick'),'unknown collection operation')
        with self.lock:
            state=self.store.setting('collection') or {}
            if operation=='start':
                if not state.get('sessionId'):
                    state={'sessionId':new_id('RUN2'),'startMs':time.time_ns()//1_000_000+2000,'running':True}
                else:
                    if state.get('running'):
                        return {'vppId':self.vpp_id,'operation':operation,'session':state,'ok':True,'results':[]}
                    actual=[r['payload']['sequence'] for r in self.store.all('batch',('records','edgePackets')) if r['payload']['sessionId']==state['sessionId']]
                    # Shift the shared clock on resume: paused time never skips batch numbers.
                    state['startMs']=time.time_ns()//1_000_000+2000-max(actual,default=0)*30000
                    state['running']=True
                self.store.set_setting('collection',state)
                payload={'sessionId':state['sessionId'],'startMs':state['startMs']}
            elif operation=='tick':
                if not state.get('sessionId') or state.get('running'):
                    require(not state.get('running'),'pause continuous collection before manual sampling','IDEMPOTENCY_CONFLICT')
                    state={'sessionId':new_id('RUN2'),'startMs':time.time_ns()//1_000_000,'running':False,'manualSequence':0}
                # Resume at the next sequence even when this session has run automatically.
                actual=[r['payload']['sequence'] for r in self.store.all('batch',('records','edgePackets')) if r['payload']['sessionId']==state['sessionId']]
                state['manualSequence']=max(state.get('manualSequence',0),max(actual,default=0))+1
                self.store.set_setting('collection',state)
                payload={'sessionId':state['sessionId'],'sequence':state['manualSequence']}
            else:
                state['running']=False
                self.store.set_setting('collection',state)
                payload={}
        def call(edge):
            try:
                response=requests.post(f"http://127.0.0.1:{edge['port']}/api/v1/collection/{operation}",json=payload,timeout=180)
                return {'edgeId':edge['edgeId'],'httpStatus':response.status_code,'result':response.json()}
            except Exception as exc:
                return {'edgeId':edge['edgeId'],'httpStatus':None,'error':str(exc)}
        with ThreadPoolExecutor(max_workers=10) as pool:
            results=list(pool.map(call,self.edges.values()))
        return {'vppId':self.vpp_id,'operation':operation,'session':state,'results':results,
                'ok':all(r['httpStatus'] is not None and r['httpStatus']<300 for r in results)}
