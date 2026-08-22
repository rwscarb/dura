#!/usr/bin/env python3
"""
The missing integration piece: a real node that hosts, discovers, and
downloads — not another isolated demo. Wires together pieces already built
tonight rather than reinventing them:

  - real chunk-serve protocol, extended from poc_network_challenge.py's
    holder (adds INFO/LEAVES so a downloader can learn the archive's shape
    before fetching)
  - real ott archives (same .ott/ format poc_real_archive_challenge.py
    read from, via `pip install btcvm`)
  - real signed events (Identity/sign_event from poc_reputation.py), now
    persisted to disk instead of regenerated fresh every run — a real node
    needs a stable identity across invocations
  - the same discovery relay protocol from discovery_relay.py

What's actually new here, not just wired: a real client-driven download —
every previous script verified chunks locally or over a network, none of
them reassembled a full file from a remote peer onto disk before this.
"""
import base64
import hashlib
import json
import os
import socket
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from poc_reputation import Identity, verify_attestation, attestation_id
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

IDENTITY_PATH = os.path.expanduser('~/.dura_identity.key')


def load_or_create_identity():
    """A real node needs a stable pubkey across runs — regenerating fresh
    every invocation (like every other script tonight) would mean nobody
    could ever build reputation or subscribe to a host's key for real."""
    identity = Identity('local')
    if os.path.exists(IDENTITY_PATH):
        with open(IDENTITY_PATH, 'rb') as f:
            key_bytes = f.read()
        identity._priv = Ed25519PrivateKey.from_private_bytes(key_bytes)
        identity.pub = identity._priv.public_key()
    else:
        key_bytes = identity._priv.private_bytes(
            serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
            serialization.NoEncryption())
        with open(IDENTITY_PATH, 'wb') as f:
            f.write(key_bytes)
        os.chmod(IDENTITY_PATH, 0o600)
    return identity


# ── wire protocol — text command line, JSON/base64 bodies where needed ─────

def recv_line(sock):
    buf = b''
    while not buf.endswith(b'\n'):
        chunk = sock.recv(65536)
        if not chunk:
            break
        buf += chunk
    return buf.decode().strip()


def find_manifest_entry(archive_dir, file_name=None):
    manifest_path = os.path.join(archive_dir, '.ott', 'manifest.jsonl')
    if not os.path.exists(manifest_path):
        sys.exit(f"no .ott/manifest.jsonl in {archive_dir} — archive a file with ott first")
    with open(manifest_path) as f:
        entries = [json.loads(line) for line in f if line.strip()]
    if file_name:
        entries = [e for e in entries if e['name'] == file_name]
    if not entries:
        sys.exit(f"no archived file found in {archive_dir}" + (f" matching {file_name}" if file_name else ""))
    return entries[-1]  # last-write-wins, same convention ott itself uses


def load_leaves(archive_dir, root_hash):
    chunks_path = os.path.join(archive_dir, '.ott', 'chunks', f'{root_hash}.json')
    with open(chunks_path) as f:
        return json.load(f)


def run_host_server(archive_dir, file_name, port, bind_host='0.0.0.0'):
    entry = find_manifest_entry(archive_dir, file_name)
    leaves = load_leaves(archive_dir, entry['sha256'])
    file_path = entry.get('last_path') or os.path.join(archive_dir, entry['name'])
    if not os.path.exists(file_path):
        sys.exit(f"archived file not found on disk at {file_path}")

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((bind_host, port))
    srv.listen(8)
    print(f"[host:{port}] serving {entry['name']} ({entry['size']:,} bytes, "
          f"{entry['n_chunks']} chunks x {entry['chunk_size']} bytes)")
    print(f"[host:{port}] sha256/merkle root: {entry['sha256']}")

    while True:
        conn, _ = srv.accept()
        with conn:
            line = recv_line(conn)
            parts = line.split()
            if not parts:
                continue
            if parts[0] == 'INFO':
                conn.sendall((json.dumps({
                    'name': entry['name'], 'sha256': entry['sha256'], 'size': entry['size'],
                    'n_chunks': entry['n_chunks'], 'chunk_size': entry['chunk_size'],
                }) + '\n').encode())
            elif parts[0] == 'LEAVES':
                conn.sendall((json.dumps(leaves) + '\n').encode())
            elif parts[0] == 'CHALLENGE':
                idx, nonce_hex = int(parts[1]), parts[2]
                with open(file_path, 'rb') as f:
                    f.seek(idx * entry['chunk_size'])
                    data = f.read(entry['chunk_size'])
                h = hashlib.sha256(data + bytes.fromhex(nonce_hex)).hexdigest()
                conn.sendall(f'HASH {h}\n'.encode())
            elif parts[0] == 'FETCH':
                idx = int(parts[1])
                with open(file_path, 'rb') as f:
                    f.seek(idx * entry['chunk_size'])
                    data = f.read(entry['chunk_size'])
                conn.sendall((f'DATA {base64.b64encode(data).decode()}\n').encode())


# ── client side ──────────────────────────────────────────────────────────

def _request(host, port, line, timeout=10):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect((host, port))
        s.sendall((line + '\n').encode())
        return recv_line(s)


def download(host, port, out_path):
    from ott import merkle_root  # pip install btcvm

    info = json.loads(_request(host, port, 'INFO'))
    print(f"downloading {info['name']} ({info['size']:,} bytes, {info['n_chunks']} chunks) "
          f"from {host}:{port}")
    leaves = json.loads(_request(host, port, 'LEAVES'))
    if len(leaves) != info['n_chunks']:
        sys.exit(f"host's LEAVES count ({len(leaves)}) doesn't match its own INFO "
                 f"({info['n_chunks']}) — refusing to trust an inconsistent host")

    # ott records a VIDEO's sha256 field as the Merkle root over its chunk
    # hashes, not a linear whole-file hash (see cmd_add: `digest =
    # merkle_root(chunks)`) — verify that BEFORE downloading a single byte,
    # so a host can't serve a self-consistent-but-fake leaves list.
    recomputed_root = merkle_root(leaves)
    if recomputed_root != info['sha256']:
        sys.exit(f"host's LEAVES don't Merkle-root to its own advertised sha256 "
                 f"({recomputed_root[:16]}... != {info['sha256'][:16]}...) — "
                 f"refusing to download from a host lying about its own archive")

    t0 = time.time()
    with open(out_path, 'wb') as out:
        for idx in range(info['n_chunks']):
            resp = _request(host, port, f'FETCH {idx}')
            if not resp.startswith('DATA '):
                sys.exit(f"chunk {idx}: bad response from host: {resp[:80]}")
            data = base64.b64decode(resp[5:])
            leaf = hashlib.sha256(data).hexdigest()
            if leaf != leaves[idx]:
                sys.exit(f"chunk {idx}: hash mismatch — host sent bytes that don't match "
                         f"its own committed chunk hash. aborting download, not writing a "
                         f"corrupted/tampered file.")
            out.write(data)
            if idx % 200 == 0 or idx == info['n_chunks'] - 1:
                print(f"  chunk {idx + 1}/{info['n_chunks']} verified", end='\r', flush=True)
    elapsed = time.time() - t0
    actual_size = os.path.getsize(out_path)
    print(f"\n{info['n_chunks']} chunks downloaded and verified in {elapsed:.1f}s")
    print(f"Merkle root over received chunks matches host's advertised sha256: True "
          f"(checked before downloading, not after)")
    if actual_size != info['size']:
        os.remove(out_path)
        sys.exit(f"size mismatch: wrote {actual_size:,} bytes, host advertised {info['size']:,} "
                 f"— deleted the output, do not trust this download")
    return out_path


# ── discovery + social signals ──────────────────────────────────────────

def post_event(relay_url, event):
    req = urllib.request.Request(
        f'{relay_url}/event', data=json.dumps(event).encode(),
        headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read())


def fetch_events(relay_url, event_type=None):
    url = f'{relay_url}/events' + (f'?type={event_type}' if event_type else '')
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, ConnectionRefusedError):
        return None


def publish(identity, relay_url, content_hash, title, host_addr):
    event = identity.sign_event('publish', content_hash=content_hash, title=title, host=host_addr)
    return post_event(relay_url, event)


def discover(relay_urls):
    seen = {}
    for relay_url in relay_urls:
        events = fetch_events(relay_url, 'publish')
        if events is None:
            print(f"  {relay_url}: unreachable, skipped")
            continue
        for e in events:
            ok, _ = verify_attestation(e)
            if ok:
                seen[attestation_id(e)] = e['payload']
    return list(seen.values())
