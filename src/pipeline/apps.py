"""V2 HTTP entry points. Each role owns its state and its chain client."""
from flask import Flask, jsonify, request

from ..common.http import read_json, error_response
from ..common.models import ProtocolError


def base_app(name, runtime):
    app = Flask(name)
    app.config['MAX_CONTENT_LENGTH'] = 2*1024*1024
    app.config['runtime'] = runtime
    app.register_error_handler(ProtocolError, error_response)

    @app.errorhandler(413)
    def too_large(_):
        return error_response(ProtocolError('INVALID_SCHEMA', 'request exceeds 2 MiB'))

    @app.get('/healthz')
    def health():
        return jsonify({'status': 'ok', 'service': name, 'protocolVersion': 2})

    @app.get('/api/v2/state')
    def state():
        return jsonify(runtime.state())
    return app


def page(rows):
    try:
        number = max(1, int(request.args.get('page', 1)))
        size = min(100, max(1, int(request.args.get('pageSize', 25))))
    except ValueError:
        raise ProtocolError('INVALID_SCHEMA', 'invalid pagination')
    vpp = request.args.get('vppId')
    if vpp:
        rows = [r for r in rows if r.get('vppId') == vpp]
    rows = list(reversed(rows))
    return jsonify({'items': rows[(number-1)*size:number*size], 'total': len(rows), 'page': number, 'pageSize': size})


def edge_app(config, spec):
    from .edge import EdgeRuntime
    rt = EdgeRuntime(config, spec)
    app = base_app('edge-v2', rt)

    @app.post('/api/v1/collection/<operation>')
    def collection(operation):
        if operation == 'pause':
            result = rt.pause_collection()
        elif operation == 'start':
            result = rt.start(read_json(request))
        elif operation == 'tick':
            if rt.collection_running:
                raise ProtocolError('IDEMPOTENCY_CONFLICT', 'pause continuous sampling first')
            result = rt.collect_once(read_json(request))
        else:
            raise ProtocolError('NOT_FOUND', 'unknown collection operation')
        return jsonify(result)

    @app.get('/api/v2/packets')
    def packets():
        return page(rt.summaries())

    @app.get('/api/v2/packets/<pid>')
    def packet(pid):
        return jsonify(rt.packet(pid))

    @app.post('/api/v2/packets/<pid>/retry')
    def retry(pid):
        return jsonify(rt.deliver(pid))
    return app


def vpp_app(config, spec):
    from .vpp import VppRuntime
    rt = VppRuntime(config, spec)
    app = base_app('vpp-v2', rt)

    @app.post('/api/v2/commands')
    def receive_commands():
        return jsonify(rt.receive_commands(read_json(request)))

    @app.post('/api/v1/edge-packets')
    def receive():
        return jsonify(rt.edge_packet(read_json(request)))

    @app.post('/api/v2/collection/<operation>')
    def collection(operation):
        result = rt.collection(operation)
        return jsonify(result), 200 if result['ok'] else 502

    @app.get('/api/v2/edge-packets')
    def packets():
        rows = [{**r['payload']['packet'], **r['state'], 'createdAt': r['createdAt']}
                for r in rt.store.all('edge', ('packet.records',))]
        return page(rows)

    @app.get('/api/v2/batches')
    def batches():
        return page([{**r['payload'], **r['state'], 'createdAt': r['createdAt']}
                     for r in rt.store.all('batch', ('records', 'edgePackets'))])

    @app.get('/api/v2/batches/<bid>')
    def batch(bid):
        return jsonify(rt.batch(bid))

    @app.get('/api/v2/groups')
    def groups():
        return page([{**r['payload'], **r['state'], 'createdAt': r['createdAt']} for r in rt.store.all('group')])

    @app.get('/api/v2/groups/<gid>')
    def group(gid):
        return jsonify(rt.group(gid))
    return app


def grid_app(config):
    from .grid import GridRuntime
    rt = GridRuntime(config)
    app = base_app('grid-v2', rt)

    @app.get('/api/v2/command-targets')
    def command_targets():
        return jsonify(rt.command_targets())

    @app.post('/api/v2/commands')
    def release_commands():
        return jsonify(rt.release_commands(read_json(request)))

    @app.get('/api/v2/commands')
    def commands():
        return page(rt.command_releases())

    @app.get('/api/v2/commands/<release_id>')
    def command_release(release_id):
        return jsonify(rt.command_release(release_id))

    @app.post('/api/v2/batches')
    def receive_batch():
        rt.receive_batch(read_json(request))
        # Verification evidence stays on GRID; the sender only needs HTTP delivery status.
        return '', 204

    @app.post('/api/v2/groups')
    def receive_group():
        rt.receive_group(read_json(request))
        return '', 204

    @app.post('/api/v2/groups/<gid>/verify')
    def verify_group(gid):
        return jsonify(rt.verify_group(gid))

    @app.get('/api/v2/batches')
    def batches():
        return page([{**r['payload']['batch'], **r['state'], 'createdAt': r['createdAt']}
                     for r in rt.store.all('batch', ('batch.records', 'batch.edgePackets'))])

    @app.get('/api/v2/batches/<bid>')
    def batch(bid):
        return jsonify(rt.batch(bid))

    @app.get('/api/v2/groups')
    def groups():
        return page([{**r['payload']['group'], **r['state'], 'createdAt': r['createdAt']} for r in rt.store.all('group')])

    @app.get('/api/v2/groups/<gid>')
    def group(gid):
        return jsonify(rt.group(gid))

    @app.get('/api/v2/vpps/<vpp_id>/state')
    def vpp_state(vpp_id):
        if vpp_id not in rt.vpps:
            raise ProtocolError('NOT_FOUND', 'VPP not found')
        return jsonify(rt.state(vpp_id))
    return app
