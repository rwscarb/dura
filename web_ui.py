#!/usr/bin/env python3
"""
Local control UI for dura: a small stdlib JSON API (same tool
discovery_relay.py already uses — ThreadingHTTPServer, no new dependency)
plus a static frontend (web/), so hosting/discovering/downloading/
liking/subscribing don't require memorizing dura.py's CLI flags. Every
endpoint is a thin wrapper over the real node.py functions the CLI
already calls — no reimplementation of any protocol logic.

Binds 127.0.0.1 by default on purpose — this is a *local* control
surface, not something meant to face the internet, and there's no auth
built (same "reachability is on you" honesty --advertise-host's docs
already apply elsewhere in this repo). Pass --bind to expose it on a LAN
at your own risk.
"""
import json
import mimetypes
import os
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import node

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'web')
DEFAULT_RELAY = 'http://127.0.0.1:9101'

_hosts = {}   # host_id -> dict describing an actively-hosted file
_jobs = {}    # job_id -> dict describing a download's progress/result
_lock = threading.Lock()


def _identity():
    return node.load_or_create_identity()


def _as_list(value, default):
    if not value:
        return default
    return [value] if isinstance(value, str) else list(value)


def _run_host_job(host_id, archive_dir, file_name, port, price, relay_urls, advertise_host, tunnel):
    try:
        identity = _identity()
        entry = node.find_manifest_entry(archive_dir, file_name)
        # fail fast, before announcing anything — a manifest entry with no
        # matching chunk data would otherwise get announced to the relay
        # and only fail later, deep in a background thread with no way
        # for the UI to ever find out
        leaves = node.load_leaves(archive_dir, entry['sha256'])
        announced = []
        for relay_url in relay_urls:
            host_addr = f'{advertise_host}:{port}'
            node.publish(identity, relay_url, entry['sha256'], entry['name'], host_addr, tunnel=tunnel)
            announced.append(relay_url)
        with _lock:
            _hosts[host_id].update(name=entry['name'], content_hash=entry['sha256'],
                                    announced_on=announced, status='running')
        if tunnel:
            relay_host, relay_port = tunnel.rsplit(':', 1)
            expanded_dir = os.path.expanduser(archive_dir)
            file_path = entry.get('last_path') or os.path.join(expanded_dir, entry['name'])
            threading.Thread(target=node.run_host_tunnel,
                              args=(relay_host, int(relay_port), entry['sha256'], entry, leaves,
                                    file_path, price),
                              kwargs={'quiet': True}, daemon=True).start()
        node.run_host_server(archive_dir, file_name, port, quiet=True, price=price)
    except SystemExit as e:
        with _lock:
            _hosts[host_id].update(status='error', error=str(e))
    except Exception as e:
        with _lock:
            _hosts[host_id].update(status='error', error=f'{type(e).__name__}: {e}')


def _run_download_job(job_id, content_hash, relay_urls, out_path, k, use_lightning):
    def on_progress(idx, n_chunks):
        with _lock:
            _jobs[job_id].update(idx=idx, n_chunks=n_chunks)

    try:
        path = node.download_with_auction(content_hash, relay_urls, out_path=out_path, k=k,
                                           use_lightning=use_lightning, on_progress=on_progress)
        with _lock:
            _jobs[job_id].update(status='done', path=path)
    except SystemExit as e:
        with _lock:
            _jobs[job_id].update(status='error', error=str(e))
    except Exception as e:
        with _lock:
            _jobs[job_id].update(status='error', error=f'{type(e).__name__}: {e}')


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # quiet — this is a local UI, not a service worth logging every hit for

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self):
        length = int(self.headers.get('Content-Length', 0))
        return json.loads(self.rfile.read(length)) if length else {}

    def do_GET(self):
        parsed = urlparse(self.path)
        path, qs = parsed.path, parse_qs(parsed.query)

        if path == '/api/whoami':
            return self._json({'pubkey': _identity().pubkey_hex()})
        if path == '/api/discover':
            return self._json({'results': node.discover(qs.get('relay') or [DEFAULT_RELAY])})
        if path == '/api/hosts':
            with _lock:
                return self._json({'hosts': list(_hosts.values())})
        if path.startswith('/api/download/'):
            with _lock:
                job = _jobs.get(path[len('/api/download/'):])
            return self._json(job) if job else self._json({'error': 'no such job'}, status=404)
        if path.startswith('/api/reputation/'):
            from poc_reputation import ReputationStore
            pubkey = path[len('/api/reputation/'):]
            reputation = ReputationStore(os.path.expanduser('~/.dura_reputation.json'))
            score, why = reputation.trust_score(pubkey)
            return self._json({'pubkey': pubkey, 'score': score, 'why': why})
        self._serve_static(path)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = self._read_json_body()
        except Exception as e:
            return self._json({'error': f'bad JSON body: {e}'}, status=400)

        handlers = {
            '/api/host': self._handle_host, '/api/download': self._handle_download,
            '/api/like': self._handle_like, '/api/subscribe': self._handle_subscribe,
        }
        handler = handlers.get(path)
        if not handler:
            return self._json({'error': 'not found'}, status=404)
        try:
            handler(body)
        except Exception as e:
            self._json({'error': f'{type(e).__name__}: {e}'}, status=400)

    def _handle_host(self, body):
        archive_dir = body.get('archive_dir')
        if not archive_dir:
            return self._json({'error': 'archive_dir required'}, status=400)
        port = int(body.get('port') or 9201)
        price = int(body.get('price') or 0)
        relay_urls = _as_list(body.get('relay'), [DEFAULT_RELAY])
        advertise_host = body.get('advertise_host') or '127.0.0.1'
        tunnel = body.get('tunnel') or None

        host_id = uuid.uuid4().hex[:12]
        with _lock:
            _hosts[host_id] = {'id': host_id, 'archive_dir': archive_dir, 'port': port,
                                'price': price, 'tunnel': tunnel, 'status': 'starting'}
        threading.Thread(target=_run_host_job,
                          args=(host_id, archive_dir, body.get('file_name'), port, price,
                                relay_urls, advertise_host, tunnel),
                          daemon=True).start()
        self._json({'host_id': host_id})

    def _handle_download(self, body):
        content_hash = body.get('content_hash')
        if not content_hash:
            return self._json({'error': 'content_hash required'}, status=400)
        relay_urls = _as_list(body.get('relay'), [DEFAULT_RELAY])
        out_path = body.get('out_path') or f'download_{content_hash[:16]}'
        k = int(body.get('k') or 3)
        use_lightning = bool(body.get('lightning'))

        job_id = uuid.uuid4().hex[:12]
        with _lock:
            _jobs[job_id] = {'status': 'running', 'idx': 0, 'n_chunks': None,
                              'content_hash': content_hash, 'path': None, 'error': None}
        threading.Thread(target=_run_download_job,
                          args=(job_id, content_hash, relay_urls, out_path, k, use_lightning),
                          daemon=True).start()
        self._json({'job_id': job_id})

    def _handle_like(self, body):
        content_hash = body.get('content_hash')
        if not content_hash:
            return self._json({'error': 'content_hash required'}, status=400)
        identity = _identity()
        event = identity.sign_event('like', content_hash=content_hash)
        self._json({'result': node.post_event(body.get('relay') or DEFAULT_RELAY, event)})

    def _handle_subscribe(self, body):
        target_pubkey = body.get('target_pubkey')
        if not target_pubkey:
            return self._json({'error': 'target_pubkey required'}, status=400)
        identity = _identity()
        event = identity.sign_event('subscribe', target_pubkey=target_pubkey)
        self._json({'result': node.post_event(body.get('relay') or DEFAULT_RELAY, event)})

    def _serve_static(self, path):
        if path == '/':
            path = '/index.html'
        safe_path = os.path.normpath(path).lstrip('/')
        full_path = os.path.join(WEB_DIR, safe_path)
        if not os.path.abspath(full_path).startswith(os.path.abspath(WEB_DIR)) \
                or not os.path.isfile(full_path):
            return self._json({'error': 'not found'}, status=404)
        ctype, _ = mimetypes.guess_type(full_path)
        with open(full_path, 'rb') as f:
            body = f.read()
        self.send_response(200)
        self.send_header('Content-Type', ctype or 'application/octet-stream')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def run_web_ui(port=8080, bind_host='127.0.0.1', quiet=False):
    srv = ThreadingHTTPServer((bind_host, port), Handler)
    if not quiet:
        print(f"[web:{port}] dura control UI at http://{bind_host}:{port}/", flush=True)
    srv.serve_forever()


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    run_web_ui(port)


if __name__ == '__main__':
    main()
