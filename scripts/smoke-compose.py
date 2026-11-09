#!/usr/bin/env python3
"""Exercise an isolated Compose installation against a fake GitHub API.

Run with the development environment: .venv/bin/python scripts/smoke-compose.py
Never reads the real .env, calls GitHub, or mounts the production database.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests.test_collector import OWNER, routes  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', default='repowatch:selfhost-check')
    parser.add_argument('--build', action='store_true')
    args = parser.parse_args()
    if args.build:
        subprocess.run(['docker', 'build', '-t', args.image, str(ROOT)], check=True)
    project = 'repowatch-smoke-' + uuid4().hex[:8]
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
    # Inherited shell variables must not override the deliberately fake settings below.
    environment = {k: v for k, v in os.environ.items() if not k.startswith((
        'GITHUB_', 'DASHBOARD_', 'REPOWATCH_', 'COLLECT_', 'BACKUP_', 'INCLUDE_',
        'EXCLUDE_', 'PYPI_', 'CI_JOBS', 'SPIKE_',
    ))}
    with tempfile.TemporaryDirectory(prefix=project + '-') as directory:
        tmp = Path(directory)
        mock = tmp / 'mock'
        mock.mkdir(mode=0o755)
        (mock / 'responses.json').write_text(json.dumps(routes(datetime.now(timezone.utc), stars=['bob'])))
        (mock / 'server.py').write_text('''import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlsplit
from pathlib import Path
responses=json.loads(Path('/mock/responses.json').read_text())
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path=urlsplit(self.path).path
        self.send_response(200 if path in responses else 404)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(responses.get(path, {'message':'unknown fixture'})).encode())
    def log_message(self, *args): pass
HTTPServer(('0.0.0.0',8000), Handler).serve_forever()
''')
        env_file = tmp / 'test.env'
        def write_env(auth='basic', password='smoke-only-$-#-password'):
            env_file.write_text('\n'.join([
                f'REPOWATCH_IMAGE={args.image}', f'REPOWATCH_PORT={port}', 'REPOWATCH_BIND=127.0.0.1',
                f'GITHUB_OWNER={OWNER}', 'GITHUB_TOKEN=smoke-only-not-a-real-token',
                'GITHUB_API_URL=http://fake-github:8000', 'COLLECT_INTERVAL_SECONDS=60',
                f'DASHBOARD_AUTH={auth}', 'DASHBOARD_USER=smoke', f"DASHBOARD_PASSWORD='{password}'",
            ]) + '\n')
            env_file.chmod(0o600)
        write_env()
        override = tmp / 'compose.test.json'
        override.write_text(json.dumps({'services': {'fake-github': {
            'image': args.image, 'entrypoint': ['python', '/mock/server.py'],
            'volumes': [f'{mock}:/mock:ro'], 'read_only': True,
            'cap_drop': ['ALL'], 'security_opt': ['no-new-privileges:true'],
        }}}))
        compose = ['docker', 'compose', '--env-file', str(env_file), '-p', project,
                   '-f', str(ROOT / 'compose.yaml'), '-f', str(override)]
        def run(*arguments, check=True):
            result = subprocess.run([*compose, *arguments], env=environment, capture_output=True, text=True)
            if check and result.returncode:
                raise RuntimeError(f'Compose {arguments[0]} failed:\n{result.stdout}\n{result.stderr}')
            return result
        def request(path='api/status', authenticated=True):
            headers = {}
            if authenticated:
                headers['Authorization'] = 'Basic ' + base64.b64encode(b'smoke:smoke-only-$-#-password').decode()
            try:
                with urlopen(Request(f'http://127.0.0.1:{port}/{path}', headers=headers), timeout=5) as response:
                    return response.status, response.read()
            except HTTPError as error:
                return error.code, error.read()
        def wait_for_status(predicate=lambda value: True):
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                try:
                    status, body = request()
                    value = json.loads(body)
                    if status == 200 and predicate(value):
                        return value
                except (URLError, OSError, ValueError):
                    pass
                time.sleep(.25)
            raise AssertionError('API did not reach the expected state within 60s')
        try:
            config = json.loads(run('config', '--format', 'json').stdout)
            assert 'GITHUB_TOKEN' not in config['services']['web']['environment']
            run('up', '-d', '--no-build', 'web', 'fake-github')
            wait_for_status()
            assert request(authenticated=False)[0] == 401
            assert request('app.js', authenticated=False)[0] == 401
            assert request('api/trends?days=7', authenticated=False)[0] == 401
            run('run', '--rm', '--no-deps', 'collect')
            first = wait_for_status(lambda v: v['last_run'] and v['last_run']['finished_at'])
            assert first['last_run']['repos'] == 1 and first['last_run']['errors'] == 0
            assert json.loads(request('api/summary?days=7')[1])[0]['downloads'] == 3
            assert len(json.loads(request('api/trends?days=7')[1])['dates']) == 7
            run('up', '-d', '--no-build', 'collector')
            wait_for_status(lambda v: v['last_run'] and v['last_run']['id'] > first['last_run']['id'] and v['last_run']['finished_at'])
            run('run', '--rm', '--no-deps', 'backup')
            probe = """import gzip,os,sqlite3,tempfile
from pathlib import Path
assert os.getuid()==10001
assert not any(Path('/app',p).exists() for p in ('.env','.git','data','.aws','.codex'))
assert not os.environ.get('GITHUB_TOKEN')
files=list(Path('/data/backups').glob('repowatch-*.db.gz'))
assert files
with tempfile.NamedTemporaryFile(suffix='.db') as f:
    f.write(gzip.decompress(files[0].read_bytes())); f.flush()
    c=sqlite3.connect(f.name)
    assert c.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    assert c.execute('SELECT COUNT(*) FROM repos').fetchone()[0]==1
print(files[0].name)
"""
            backup_name = run('exec', '-T', 'web', 'python', '-c', probe).stdout.strip()
            run('exec', '-T', 'web', 'python', '-m', 'repowatch.healthcheck')
            run('stop', 'web', 'collector')
            # Exercise the exact restore recipe in the user guide.
            guide = (ROOT / 'docs/self-hosting.md').read_text()
            marker = "docker compose run --rm --no-deps -T --entrypoint python backup -c '\n"
            restore = guide.split(marker, 1)[1].split("\n'\n", 1)[0]
            restore = restore.replace('repowatch-<timestamp>.db.gz', backup_name)
            run('run', '--rm', '--no-deps', '--entrypoint', 'python', 'backup', '-c', restore)
            run('up', '-d', '--no-build', '--force-recreate', '--no-deps', 'web')
            wait_for_status(lambda v: v['last_run'] is not None)
            write_env(auth='external', password='')
            run('up', '-d', '--no-build', '--force-recreate', '--no-deps', 'web')
            wait_for_status()
            assert request(authenticated=False)[0] == 200
            write_env(password='')
            refused = run('run', '--rm', '--no-deps', 'web', check=False)
            assert refused.returncode != 0
            assert 'requires DASHBOARD_USER and DASHBOARD_PASSWORD' in refused.stderr
            print('PASS: Basic login, protected API/assets, external auth, fail-closed startup, collection loop, persistent data, verified backup/restore, non-root image without local secrets.')
        finally:
            # Only resources belonging to this randomly named test project are removed.
            run('down', '--volumes', '--remove-orphans', check=False)


if __name__ == '__main__':
    main()
