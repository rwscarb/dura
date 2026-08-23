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
import contextlib
import io
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

try:
    import qrcode  # pip install qrcode -- same package ott.py's own `ott qr` already uses
    _HAS_QR = True
except ImportError:
    _HAS_QR = False

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'web')
DEFAULT_RELAY = 'http://127.0.0.1:9101'


class _JobStdout:
    """Replaces sys.stdout for this whole process so a background job
    thread (host/download) can be muted without touching a single print()
    in node.py -- those are correct and wanted when the same functions
    run from dura.py or the interactive shell, where a human is actually
    watching the terminal. They're just noise here: nothing reads this
    process's stdout, and a job's real status already goes through
    _jobs[job_id]/_hosts[host_id], which the API/frontend actually poll.

    Routes by the *calling* thread, not a single global swap -- a
    threading.local() flag, so two jobs running concurrently in different
    threads (or a job running while the main thread prints its own
    [web:PORT] startup line) can't clobber each other's output."""

    def __init__(self, real):
        self._real = real
        self._local = threading.local()

    def write(self, s):
        buf = getattr(self._local, 'buf', None)
        (buf if buf is not None else self._real).write(s)

    def flush(self):
        self._real.flush()

    def __getattr__(self, name):
        return getattr(self._real, name)


sys.stdout = _JobStdout(sys.stdout)


@contextlib.contextmanager
def _quiet():
    """Mute node.py's CLI-oriented prints for the current thread only,
    for the duration of the with-block."""
    sys.stdout._local.buf = io.StringIO()
    try:
        yield
    finally:
        sys.stdout._local.buf = None

_hosts = {}   # host_id -> dict describing an actively-hosted file
_jobs = {}    # job_id -> dict describing a download's progress/result
_lock = threading.Lock()
_lan_url = None   # set once in run_web_ui() -- the base URL a phone on the
                   # same LAN can actually reach this server at, or None if
                   # it can't (bound to 127.0.0.1). The client can't compute
                   # this itself: window.location.origin only reflects
                   # whatever address the *desktop* browser used to load the
                   # page, which is 127.0.0.1 the moment someone opens it via
                   # localhost -- exactly the bug this fixes.


def _identity():
    return node.load_or_create_identity()


def _as_list(value, default):
    if not value:
        return default
    return [value] if isinstance(value, str) else list(value)


def _detect_lan_ip():
    """Best-effort outbound-facing LAN IP. UDP connect() here never sends a
    packet -- it just makes the kernel pick a route -- but that's enough to
    read back the interface address that route would use. Needed because
    a startup/QR URL naming the literal 0.0.0.0 wildcard bind address is
    useless: nothing can connect *to* 0.0.0.0 from another device."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def _run_host_job(host_id, archive_dir, file_name, port, price, relay_urls, advertise_host, tunnel):
    try:
        with _quiet():
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
                node.publish(identity, relay_url, entry['sha256'], entry['name'], host_addr,
                              tunnel=tunnel)
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
        with _quiet():
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
        if path == '/api/lan-url':
            return self._json({'url': _lan_url})
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
        if path.startswith('/api/stream/'):
            return self._handle_stream(path[len('/api/stream/'):])
        if path == '/api/qr':
            data = (qs.get('data') or [''])[0]
            if not data:
                return self._json({'error': 'data query param required'}, status=400)
            return self._handle_qr(data)
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

    def _handle_stream(self, job_id):
        """Serve an already-downloaded job's file with real HTTP range
        support, so a <video> tag can seek/scrub instead of just
        downloading the whole thing blind — the actual gap behind
        'play/open media': the web UI could already trigger a download,
        but the bytes only ever landed on this server's disk, never made
        it to the browser. This is a thin BaseHTTPRequestHandler, so
        range parsing is done by hand rather than pulled in from a
        framework — same "no new dependency" choice as the rest of this
        file."""
        with _lock:
            job = _jobs.get(job_id)
        if not job or job.get('status') != 'done' or not job.get('path'):
            return self._json({'error': 'no completed download for that job id'}, status=404)
        path = job['path']
        if not os.path.isfile(path):
            return self._json({'error': f'{path} no longer exists on disk'}, status=404)

        file_size = os.path.getsize(path)
        ctype, _ = mimetypes.guess_type(path)
        ctype = ctype or 'application/octet-stream'

        range_header = self.headers.get('Range')
        if range_header:
            try:
                _, _, range_spec = range_header.partition('=')
                start_s, _, end_s = range_spec.partition('-')
                start = int(start_s) if start_s else 0
                end = int(end_s) if end_s else file_size - 1
                end = min(end, file_size - 1)
            except ValueError:
                return self._json({'error': f'malformed Range header: {range_header!r}'}, status=400)
            if start > end or start >= file_size:
                self.send_response(416)
                self.send_header('Content-Range', f'bytes */{file_size}')
                self.end_headers()
                return
            length = end - start + 1
            self.send_response(206)
            self.send_header('Content-Range', f'bytes {start}-{end}/{file_size}')
        else:
            start, length = 0, file_size
            self.send_response(200)

        self.send_header('Content-Type', ctype)
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Content-Length', str(length))
        self.end_headers()

        with open(path, 'rb') as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(262144, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return  # client seeked or closed mid-stream -- not an error
                remaining -= len(chunk)

    def _handle_qr(self, data):
        """PNG QR code of an arbitrary string -- same qrcode package and
        same 'encode a URL so a phone can scan instead of type it' idea as
        ott's own `ott qr`, just rendered as an image for the page to
        embed instead of printed as terminal ASCII."""
        if not _HAS_QR:
            return self._json({'error': 'qrcode not installed on the server -- pip install qrcode'},
                               status=501)
        import io
        qr = qrcode.QRCode(border=1)
        qr.add_data(data)
        qr.make(fit=True)
        img = qr.make_image()
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        body = buf.getvalue()
        self.send_response(200)
        self.send_header('Content-Type', 'image/png')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
    global _lan_url
    srv = ThreadingHTTPServer((bind_host, port), Handler)

    reachable_host = _detect_lan_ip() if bind_host == '0.0.0.0' else bind_host
    if bind_host != '127.0.0.1' and reachable_host:
        _lan_url = f'http://{reachable_host}:{port}/'

    if not quiet:
        print(f"[web:{port}] dura control UI at http://{bind_host}:{port}/", flush=True)
        if bind_host == '127.0.0.1':
            print("  bound to localhost only -- pass --bind 0.0.0.0 to reach this from your "
                  "phone (and get a scan-to-open QR here)", flush=True)
        elif _lan_url:
            if _HAS_QR:
                print(f"  scan to open on your phone ({_lan_url}):", flush=True)
                qr = qrcode.QRCode(border=1)
                qr.add_data(_lan_url)
                qr.make(fit=True)
                qr.print_ascii(invert=True)
            else:
                print(f"  scan-to-open URL: {_lan_url}  (pip install qrcode for a terminal "
                      f"QR code)", flush=True)
        else:
            print("  couldn't detect a LAN IP -- phone QR codes in the web UI won't work "
                  "until you restart with reachable networking", flush=True)
    srv.serve_forever()


def main():
    import argparse
    parser = argparse.ArgumentParser(description='dura local control UI')
    parser.add_argument('port', nargs='?', type=int, default=8080,
                         help='port to listen on (default: 8080)')
    parser.add_argument('--port', dest='port_flag', type=int,
                         help='same as the positional port arg, --port form')
    parser.add_argument('--bind', default='127.0.0.1',
                         help='bind address (default: 127.0.0.1, local only -- no auth is '
                              'built, so only widen this on a network you trust)')
    args = parser.parse_args()
    port = args.port_flag if args.port_flag is not None else args.port
    run_web_ui(port, bind_host=args.bind)


if __name__ == '__main__':
    main()
