"""Dynamic service launcher. Refuses to overwrite a live PID file."""
import os
import subprocess
import sys
import time
from pathlib import Path

import requests
from src.common.config import load_config


def main():
    cfg = load_config()
    logs = Path('logs/services')
    logs.mkdir(parents=True, exist_ok=True)
    pidfile = logs/'pids.txt'
    if pidfile.exists():
        for line in pidfile.read_text().splitlines():
            try:
                os.kill(int(line.split()[0]), 0)
            except (ProcessLookupError, ValueError):
                continue
            raise SystemExit('Services already running; stop them with scripts/stop_services.sh first.')
    services = [('grid', 'src.grid.service', cfg['grid']['port'], {})]
    services += [(v['vppId'], 'src.vpp.service', v['port'], {'VPP_ID': v['vppId']}) for v in cfg['vpps']]
    services += [(e['edgeId'], 'src.edge.service', e['port'], {'EDGE_ID': e['edgeId']}) for e in cfg['edges']]
    services += [('console', 'src.console.service', int(os.environ.get('CONSOLE_PORT', '8400')), {})]
    processes = []
    try:
        with pidfile.open('w') as pids:
            for name, module, port, extra in services:
                with (logs/(name+'.log')).open('a') as logfile:
                    child = subprocess.Popen([sys.executable, '-u', '-m', module], env={**os.environ, **extra}, stdout=logfile, stderr=subprocess.STDOUT, start_new_session=True)
                pids.write(f'{child.pid} {name}\n')
                pids.flush()
                processes.append(child)
        deadline = time.monotonic()+60
        for (name, _, port, _), child in zip(services, processes):
            while time.monotonic() < deadline:
                if child.poll() is not None:
                    raise RuntimeError(f'{name} exited; inspect logs/services/{name}.log')
                try:
                    response = requests.get(f'http://127.0.0.1:{port}/healthz', timeout=1)
                    if response.ok and response.json().get('protocolVersion') == 2:
                        print(f'Healthy {name}: http://127.0.0.1:{port}', flush=True)
                        break
                except (requests.RequestException, ValueError):
                    pass
                time.sleep(.25)
            else:
                raise RuntimeError(f'{name} health check timed out')
        console_port = int(os.environ.get('CONSOLE_PORT', '8400'))
        print(f'All {len(services)} business services ready. UI: http://127.0.0.1:{console_port}', flush=True)
    except BaseException:
        for child in processes:
            child.terminate()
        pidfile.unlink(missing_ok=True)
        raise


if __name__ == '__main__':
    main()
