"""Explicit V2 schemas; old MERKLE-V1 record hashes remain unchanged.

VPP root = Merkle of 300 full records. A group commits exactly 40 ordered
descriptors (batchId, VPP, session, sequence, count, root). No cached batch
roots may substitute for hashing all 12000 raw records during verification.
"""
import hashlib

from ..common.canonical import canonical_bytes
from ..common.crypto import verify_bytes
from ..common.merkle import build_tree, node_hash
from ..common.models import ProtocolError, validate_record

EDGE_SCHEMA = 'EDGE-ROOT-V2'
BATCH_SCHEMA = 'VPP-ROOT-V2'
GROUP_SCHEMA = 'VPP-GROUP-ROOT-V2'
GROUP_PREFIX = b'QKL-GROUP-LEAF-V2:'
ROOT_RULE = 'MERKLE-V1/CANON-JSON-V1'


def require(condition, message, code='INVALID_SCHEMA', object_id=None):
    if not condition:
        raise ProtocolError(code,message,object_id=object_id)


def identifier(value):
    import re
    require(isinstance(value,str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}',value) is not None,'invalid identifier')
    return value


def positive(value):
    require(type(value) is int and value > 0,'positive integer required')
    return value


def digest(value):
    require(isinstance(value,str) and len(value)==64,'root must be 64 hex characters')
    try:
        require(len(bytes.fromhex(value))==32,'invalid root length')
    except ValueError:
        raise ProtocolError('INVALID_SCHEMA','root is not hex')
    return value


def records_root(records):
    require(isinstance(records,list) and bool(records),'records must be a nonempty list')
    for record in records:
        validate_record(record)
    require(len({r['recordId'] for r in records})==len(records),'duplicate recordId','DUPLICATE_ID')
    return build_tree(records,canonical_bytes).root_hex


def verify_signature(payload, signature, trusted):
    require(isinstance(signature,dict) and signature.get('keyId')==trusted['keyId'],'unregistered signing key','IDENTITY_NOT_ALLOWED')
    require(signature.get('algorithm')=='ECDSA-secp256k1-SHA256' and signature.get('ruleVersion')=='BUSINESS-SIG-V1','unsupported signature rules')
    ok,reason = verify_bytes(canonical_bytes(payload),signature.get('value',''),trusted['publicKey'])
    require(ok,reason,'SIGNATURE_INVALID')


def sign(signer, payload):
    return {'algorithm':'ECDSA-secp256k1-SHA256','ruleVersion':'BUSINESS-SIG-V1','keyId':signer.key_id,'value':signer.sign_bytes(canonical_bytes(payload))}


def descriptor(batch):
    return {name:batch[name] for name in ('batchId','vppId','sessionId','sequence','recordCount','rootHash')}


def group_root(items):
    require(isinstance(items,list) and len(items)==40,'group must contain exactly 40 batches')
    fields=('batchId','vppId','sessionId','sequence','recordCount','rootHash')
    require(all(isinstance(b,dict) and all(k in b for k in fields) for b in items),'invalid group descriptor')
    require(len({b['batchId'] for b in items})==40,'duplicate batchId','DUPLICATE_ID')
    require(len({b['vppId'] for b in items})==1 and len({b['sessionId'] for b in items})==1,'mixed VPPs or sessions')
    start = positive(items[0]['sequence'])
    require((start-1)%40==0,'group must start on a 40-batch boundary')
    for i,item in enumerate(items):
        identifier(item['batchId'])
        identifier(item['vppId'])
        identifier(item['sessionId'])
        require(item['sequence']==start+i and item['recordCount']==300,'wrong batch order or count')
        digest(item['rootHash'])
    nodes = [hashlib.sha256(GROUP_PREFIX+canonical_bytes(descriptor(item))).digest() for item in items]
    while len(nodes)>1:
        nodes = [node_hash(nodes[i],nodes[i+1]) if i+1<len(nodes) else nodes[i] for i in range(0,len(nodes),2)]
    return nodes[0].hex()


def edge_batch_id(packet_id):
    return identifier('E2-'+packet_id)


def vpp_batch_id(vpp_id,session_id,seq):
    return identifier(f'V2-{vpp_id}-{session_id}-{seq:06d}')


def group_id(vpp_id,session_id,seq):
    return identifier(f'G2-{vpp_id}-{session_id}-{(seq-1)//40+1:05d}')
