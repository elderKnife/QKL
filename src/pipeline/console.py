"""Local dashboard gateway: real topology, per-VPP controls and GRID checks."""
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import requests
from flask import Flask, jsonify, request, session, send_from_directory

from ..common.config import PROJECT_ROOT, contract_address
from ..common.http import read_json, error_response
from ..common.models import ProtocolError, utc_iso


def create_app(config):
    app = Flask('qkl-console-v2', static_folder=None)
    app.secret_key = os.urandom(32)
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Strict', PERMANENT_SESSION_LIFETIME=timedelta(hours=8))
    app.register_error_handler(ProtocolError, error_response)
    dist = os.path.join(PROJECT_ROOT, 'console-prototype', 'frontend', 'dist')
    vpps = {v['vppId']: v for v in config['vpps']}
    grid_url = 'http://127.0.0.1:'+str(config['grid']['port'])
    users = {os.environ.get('VPP_USERNAME','vpp'): (os.environ.get('VPP_PASSWORD','vpp-local-password'),'vpp'),
             os.environ.get('GRID_USERNAME','grid'): (os.environ.get('GRID_PASSWORD','grid-local-password'),'grid')}

    def remote(url, method='GET', body=None, timeout=8):
        try:
            result = requests.request(method, url, json=body if method=='POST' else None, timeout=timeout)
            payload = result.json()
            if not result.ok:
                error = payload.get('error', {})
                raise ProtocolError(error.get('code') if error.get('code') in ('NOT_FOUND','INVALID_SCHEMA','IDEMPOTENCY_CONFLICT','CHAIN_UNAVAILABLE','ROOT_MISMATCH') else 'CHAIN_UNAVAILABLE',
                                    error.get('message', str(payload)), retryable=True)
            return payload
        except requests.RequestException as exc:
            raise ProtocolError('CHAIN_UNAVAILABLE', '服务连接失败: '+str(exc), retryable=True) from exc

    def safe(url):
        try:
            return {'available': True, 'data': remote(url)}
        except (ProtocolError, ValueError) as exc:
            return {'available': False, 'error': str(exc)}

    def vpp_url(vpp_id):
        if vpp_id not in vpps:
            raise ProtocolError('NOT_FOUND', 'VPP not found')
        return 'http://127.0.0.1:'+str(vpps[vpp_id]['port'])

    @app.before_request
    def guard():
        if not request.path.startswith('/api/') or request.path in ('/api/auth/login','/api/auth/me','/api/auth/logout'):
            return None
        if not session.get('user'):
            return jsonify({'error': {'code':'UNAUTHENTICATED','message':'请先登录'}}),401
        if request.method == 'POST':
            if request.headers.get('X-Prototype-Request') != '1':
                return jsonify({'error': {'code':'FORBIDDEN','message':'缺少请求标识'}}),403
            if (request.path.endswith('/verify') or request.path == '/api/v2/grid/commands') and session['user']['role'] != 'grid':
                return jsonify({'error': {'code':'FORBIDDEN','message':'请使用 GRID 账户执行此操作'}}),403

    @app.get('/healthz')
    def health():
        return jsonify({'status':'ok','protocolVersion':2,'service':'console-v2'})

    @app.post('/api/auth/login')
    def login():
        body=read_json(request)
        name=body.get('username','')
        entry=users.get(name)
        if not entry or entry[0]!=body.get('password'):
            return jsonify({'error':{'code':'INVALID_CREDENTIALS','message':'用户名或密码错误'}}),401
        session.clear()
        session.permanent=True
        session['user']={'username':name,'role':entry[1]}
        return jsonify(session['user'])

    @app.get('/api/auth/me')
    def me():
        if not session.get('user'):
            return jsonify({'error':{'code':'UNAUTHENTICATED','message':'请先登录'}}),401
        return jsonify({'user':session['user'],**session['user']})

    @app.post('/api/auth/logout')
    def logout():
        session.clear()
        return jsonify({'ok':True})

    @app.get('/api/v2/topology')
    def topology():
        return jsonify({'vpps':[{'vppId':v['vppId'],'port':v['port'],
                        'edgeIds':[e['edgeId'] for e in config['edges'] if e['vppId']==v['vppId']],
                        'deviceCount':sum(len(e['devices']) for e in config['edges'] if e['vppId']==v['vppId'])} for v in config['vpps']],
                        'edgeCount':len(config['edges']),'deviceCount':sum(len(e['devices']) for e in config['edges']),
                        'intervalSeconds':30,'groupSize':40,'registryAddress':contract_address(config),
                        'chainName':'FISCO BCOS 2.11 / 5 PBFT nodes','protocolVersion':2})

    @app.get('/api/v2/overview')
    def overview():
        urls=[vpp_url(v)+'/api/v2/state' for v in vpps]+[grid_url+'/api/v2/state']
        with ThreadPoolExecutor(max_workers=4) as pool:
            states=list(pool.map(safe,urls))
        return jsonify({'checkedAt':utc_iso(),'vpps':[{'vppId':v,**state} for v,state in zip(vpps,states)],'grid':states[-1]})

    @app.get('/api/v2/vpps/<vpp_id>/workbench')
    def workbench(vpp_id):
        base=vpp_url(vpp_id)
        edges=[e for e in config['edges'] if e['vppId']==vpp_id]
        urls=[base+'/api/v2/state',grid_url+f'/api/v2/vpps/{vpp_id}/state']+[f"http://127.0.0.1:{e['port']}/api/v2/state" for e in edges]
        with ThreadPoolExecutor(max_workers=12) as pool:
            states=list(pool.map(safe,urls))
        return jsonify({'vpp':states[0],'grid':states[1],
                        'edges':[{'edgeId':e['edgeId'],'port':e['port'],'deviceCount':len(e['devices']),**state} for e,state in zip(edges,states[2:])],
                        'checkedAt':utc_iso()})

    @app.post('/api/v2/vpps/<vpp_id>/collection/<operation>')
    def collection(vpp_id,operation):
        if operation not in ('start','pause','tick'):
            raise ProtocolError('NOT_FOUND','unknown operation')
        return jsonify(remote(vpp_url(vpp_id)+'/api/v2/collection/'+operation,'POST',{},200))

    @app.get('/api/v2/vpps/<vpp_id>/<kind>')
    def vpp_list(vpp_id,kind):
        if kind not in ('batches','groups','edge-packets'):
            raise ProtocolError('NOT_FOUND','unknown list')
        query=requests.compat.urlencode(request.args.to_dict())
        return jsonify(remote(vpp_url(vpp_id)+'/api/v2/'+kind+'?'+query,timeout=15))

    @app.get('/api/v2/grid/<kind>')
    def grid_list(kind):
        if kind not in ('batches','groups','commands','command-targets'):
            raise ProtocolError('NOT_FOUND','unknown list')
        vpp=request.args.get('vppId','')
        if vpp:
            vpp_url(vpp)
        params=request.args.to_dict()
        query=requests.compat.urlencode(params)
        return jsonify(remote(grid_url+'/api/v2/'+kind+'?'+query,timeout=20))

    @app.get('/api/v2/grid/<kind>/<key>')
    def detail(kind,key):
        if kind not in ('batches','groups','commands'):
            raise ProtocolError('NOT_FOUND','unknown detail')
        return jsonify(remote(grid_url+'/api/v2/'+kind+'/'+key,timeout=15))

    @app.post('/api/v2/grid/commands')
    def release_commands():
        return jsonify(remote(grid_url+'/api/v2/commands','POST',read_json(request),180))

    @app.post('/api/v2/grid/groups/<gid>/verify')
    def verify(gid):
        return jsonify(remote(grid_url+'/api/v2/groups/'+gid+'/verify','POST',{},120))

    @app.get('/')
    def index():
        return send_from_directory(dist,'index.html')

    @app.get('/assets/<path:name>')
    def asset(name):
        return send_from_directory(os.path.join(dist,'assets'),name)
    return app
