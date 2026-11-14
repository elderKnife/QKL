"""GRID to VPP command commitments, separate from telemetry and execution."""
import hashlib
import math
import re

from ..common.canonical import canonical_bytes
from ..common.merkle import node_hash
from ..common.models import parse_iso_ms, utc_iso, utc_now_ms
from .protocol import digest, identifier, require

COMMAND_SCHEMA = 'GRID-COMMAND-V2'
COMMAND_BATCH_SCHEMA = 'GRID-COMMAND-BATCH-V2'
COMMAND_RECEIPT_SCHEMA = 'VPP-COMMAND-VERIFIED-V2'
COMMAND_PREFIX = b'QKL-COMMAND-LEAF-V2:'
COMMAND_ROOT_RULE = 'COMMAND-MERKLE-V2/CANON-JSON-V1'
MAX_TARGETS = 300


def now_seconds():
    return utc_iso(utc_now_ms() // 1000 * 1000).replace('.000Z', 'Z')


def valid_time(value):
    require(isinstance(value, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{3})?Z', value),
            'timestamp must be UTC at second or millisecond precision')
    parse_iso_ms(value if '.' in value else value[:-1] + '.000Z')
    return value


def validate_targets(config, targets, mode):
    require(mode in ('SINGLE', 'BULK'), 'command mode must be SINGLE or BULK')
    require(isinstance(targets, list) and 1 <= len(targets) <= MAX_TARGETS, 'select 1..300 devices from one VPP')
    require(mode != 'SINGLE' or len(targets) == 1, 'single release requires exactly one device')
    roster = {e['edgeId']: e for e in config['edges']}
    result, seen = [], set()
    for target in targets:
        require(isinstance(target, dict) and set(target) == {'vppId', 'edgeId', 'deviceId'}, 'invalid command target')
        vpp, edge_id, device_id = (identifier(target[k]) for k in ('vppId', 'edgeId', 'deviceId'))
        edge = roster.get(edge_id)
        require(edge is not None and edge['vppId'] == vpp, 'edge does not belong to this VPP', 'TARGET_MISMATCH')
        device = next((d for d in edge['devices'] if d['deviceId'] == device_id), None)
        require(device is not None, 'device does not belong to this edge', 'TARGET_MISMATCH')
        require((vpp, edge_id, device_id) not in seen, 'duplicate command target', 'DUPLICATE_ID')
        seen.add((vpp, edge_id, device_id))
        result.append({**target, 'deviceType': device['deviceType']})
    require(len({t['vppId'] for t in result}) == 1,
            'each command release must target exactly one VPP', 'TARGET_MISMATCH')
    return sorted(result, key=lambda t: (t['vppId'], t['edgeId'], t['deviceId']))


def simulate_command(target, *, command_id, dispatch_id, release_id, grid_id, issued_at):
    # Values are only signed simulation content. No device/edge state is changed.
    watts = {'pv': 10.0, 'storage': -5.0, 'charger': 7.0}[target['deviceType']]
    return {'schemaVersion': COMMAND_SCHEMA, 'commandId': command_id,
            'dispatchId': dispatch_id, 'releaseId': release_id, 'gridId': grid_id,
            'vppId': target['vppId'], 'edgeId': target['edgeId'], 'deviceId': target['deviceId'],
            'deviceType': target['deviceType'], 'action': 'set_active_power_kw',
            'params': {'activePowerKw': watts}, 'issuedAt': issued_at, 'simulation': True}


def command_root(commands):
    require(isinstance(commands, list) and 1 <= len(commands) <= 300, 'a VPP command batch requires 1..300 commands')
    fields = {'schemaVersion', 'commandId', 'dispatchId', 'releaseId', 'gridId', 'vppId',
              'edgeId', 'deviceId', 'deviceType', 'action', 'params', 'issuedAt', 'simulation'}
    ids, targets = set(), set()
    for command in commands:
        require(isinstance(command, dict) and set(command) == fields, 'invalid command fields')
        require(command['schemaVersion'] == COMMAND_SCHEMA and command['simulation'] is True, 'unsupported command schema')
        for key in ('commandId', 'dispatchId', 'releaseId', 'gridId', 'vppId', 'edgeId', 'deviceId'):
            identifier(command[key])
        valid_time(command['issuedAt'])
        require(command['deviceType'] in ('pv', 'storage', 'charger'), 'invalid device type')
        require(command['action'] == 'set_active_power_kw', 'unsupported simulated action')
        params = command['params']
        require(isinstance(params, dict) and set(params) == {'activePowerKw'}, 'invalid command parameters')
        value = params['activePowerKw']
        require(type(value) in (int, float) and math.isfinite(value) and -1000 <= value <= 1000, 'invalid active power')
        require(command['commandId'] not in ids and command['deviceId'] not in targets, 'duplicate command or device', 'DUPLICATE_ID')
        ids.add(command['commandId'])
        targets.add(command['deviceId'])
    nodes = [hashlib.sha256(COMMAND_PREFIX + canonical_bytes(c)).digest()
             for c in sorted(commands, key=lambda c: c['commandId'])]
    while len(nodes) > 1:
        nodes = [node_hash(nodes[i], nodes[i+1]) if i+1 < len(nodes) else nodes[i]
                 for i in range(0, len(nodes), 2)]
    return nodes[0].hex()


def validate_envelope(config, envelope, vpp_id):
    fields = {'schemaVersion', 'rootRule', 'dispatchId', 'releaseId', 'gridId', 'vppId',
              'mode', 'recordCount', 'rootHash', 'issuedAt', 'commands'}
    require(isinstance(envelope, dict) and set(envelope) == fields, 'invalid command batch fields')
    require(envelope['schemaVersion'] == COMMAND_BATCH_SCHEMA and envelope['rootRule'] == COMMAND_ROOT_RULE, 'unsupported command batch schema')
    require(envelope['gridId'] == config['grid']['gridId'], 'unknown command publisher', 'IDENTITY_NOT_ALLOWED')
    require(envelope['vppId'] == vpp_id, 'command batch targets another VPP', 'TARGET_MISMATCH')
    identifier(envelope['dispatchId'])
    identifier(envelope['releaseId'])
    valid_time(envelope['issuedAt'])
    digest(envelope['rootHash'])
    commands = envelope['commands']
    require(isinstance(commands, list) and type(envelope['recordCount']) is int and envelope['recordCount'] == len(commands), 'command count mismatch')
    require(all(isinstance(c, dict) for c in commands), 'invalid command')
    for command in commands:
        require(all(command.get(k) == envelope[k] for k in ('dispatchId', 'releaseId', 'gridId', 'vppId', 'issuedAt')), 'command identity differs from batch', 'TARGET_MISMATCH')
    targets = validate_targets(config, [{k: c.get(k) for k in ('vppId', 'edgeId', 'deviceId')} for c in commands], envelope['mode'])
    types = {t['deviceId']: t['deviceType'] for t in targets}
    require(all(c.get('deviceType') == types[c['deviceId']] for c in commands), 'command device type mismatch', 'TARGET_MISMATCH')
