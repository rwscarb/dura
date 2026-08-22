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
import random
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
    archive_dir = os.path.expanduser(archive_dir)  # os.path.join never expands ~, it stays literal
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
    archive_dir = os.path.expanduser(archive_dir)
    chunks_path = os.path.join(archive_dir, '.ott', 'chunks', f'{root_hash}.json')
    with open(chunks_path) as f:
        return json.load(f)


def run_host_server(archive_dir, file_name, port, bind_host='0.0.0.0', quiet=False, price=0):
    archive_dir = os.path.expanduser(archive_dir)
    entry = find_manifest_entry(archive_dir, file_name)
    leaves = load_leaves(archive_dir, entry['sha256'])
    file_path = entry.get('last_path') or os.path.join(archive_dir, entry['name'])
    if not os.path.exists(file_path):
        sys.exit(f"archived file not found on disk at {file_path}")

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((bind_host, port))
    srv.listen(8)
    if not quiet:
        # a background thread's print() races with cmd.Cmd's input()-driven
        # prompt on the same stdout — see run_relay_server's docstring for
        # why the shell passes quiet=True instead of patching this visually
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
            elif parts[0] == 'PRICE':
                conn.sendall(f'PRICE {price}\n'.encode())


# ── client side ──────────────────────────────────────────────────────────

def _request(host, port, line, timeout=10):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect((host, port))
        s.sendall((line + '\n').encode())
        return recv_line(s)


def download(host, port, out_path):
    from ott import merkle_root  # pip install btcvm

    out_path = os.path.expanduser(out_path)
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


# ── possession challenge + price auction, wired to real discovery ───────
#
# Everything below stitches poc_challenge_auction.py (challenge-gate a
# reverse auction), poc_reputation.py (local trust score), and
# lightning_settle.py (real HTLC settlement) into the real download path —
# none of them were reachable from `download()` before this, which meant
# `discover` -> `download` would silently trust the first host found, for
# free, with zero possession check.
#
# Scoped honestly, not silently: this uses sample-FETCH + Merkle-proof
# verification (poc_challenge_auction.py Part 1's mechanism) to prove a
# host holds real chunks, not the nonce-salted timing challenge from Part
# 2 — that one specifically detects a RELAY masquerading as a holder, but
# needs ground-truth bytes the verifier already trusts, which a first-time
# downloader doesn't have yet (that's the whole point of downloading).
# Sample-verifying a few chunks via FETCH is what a fresh client can
# actually do independently, since LEAVES is already Merkle-root-checked
# against the host's own advertised sha256.

def sample_challenge(host, port, leaves, k=3, timeout=10):
    """Fetch k random chunks and verify each against the (already Merkle-
    verified) leaves list — proves *this specific host* truly holds real
    chunks, not just that someone somewhere does."""
    n = len(leaves)
    indices = random.sample(range(n), min(k, n))
    latencies = []
    for idx in indices:
        t0 = time.perf_counter()
        try:
            resp = _request(host, port, f'FETCH {idx}', timeout=timeout)
        except OSError:
            return False, latencies
        latencies.append((time.perf_counter() - t0) * 1000)
        if not resp.startswith('DATA '):
            return False, latencies
        data = base64.b64decode(resp[5:])
        if hashlib.sha256(data).hexdigest() != leaves[idx]:
            return False, latencies
    return True, latencies


def get_price(host, port, timeout=5):
    try:
        resp = _request(host, port, 'PRICE', timeout=timeout)
    except OSError:
        return 0
    if resp.startswith('PRICE '):
        try:
            return int(resp[6:])
        except ValueError:
            return 0
    return 0  # host doesn't implement PRICE — treat as free rather than failing


def discover_hosts_for(relay_urls, content_hash):
    """Every host that published a matching content_hash — real multi-host
    resolution (discover() already dedupes by event, not by content, so
    two publishers of the same file both show up here)."""
    return [p for p in discover(relay_urls) if p['content_hash'].startswith(content_hash)]


def select_host(candidates, k=3, reputation=None):
    """Gate on real possession, then rank survivors by reputation then
    price — same 'challenge gates the auction' shape as
    poc_challenge_auction.py's naive-vs-gated comparison."""
    from ott import merkle_root

    scored = []
    for c in candidates:
        host, port_s = c['host'].rsplit(':', 1)
        port = int(port_s)
        try:
            leaves = json.loads(_request(host, port, 'LEAVES'))
            info = json.loads(_request(host, port, 'INFO'))
        except OSError as e:
            print(f"  x {c['host']}: unreachable ({e})")
            continue
        if merkle_root(leaves) != info['sha256'] or info['sha256'] != c['content_hash']:
            print(f"  x {c['host']}: advertised content doesn't match its own LEAVES/INFO — skipping")
            continue
        passed, latencies = sample_challenge(host, port, leaves, k=k)
        if not passed:
            print(f"  x {c['host']}: failed possession challenge ({k} chunks sampled) — skipping")
            continue
        price = get_price(host, port)
        rep_score, rep_why = reputation.trust_score(c['signer_pubkey']) if reputation else (0.5, 'no reputation store')
        avg_latency = sum(latencies) / len(latencies) if latencies else 0.0
        print(f"  + {c['host']}: possession verified ({k}/{k} chunks), price={price} sat, "
              f"reputation={rep_score:.2f} ({rep_why}), avg_latency={avg_latency:.1f}ms")
        scored.append({'candidate': c, 'host': host, 'port': port, 'info': info,
                        'price': price, 'reputation': rep_score, 'avg_latency_ms': avg_latency})

    if not scored:
        return None
    scored.sort(key=lambda s: (-s['reputation'], s['price']))  # highest trust first, then cheapest
    return scored[0]


def download_with_auction(content_hash, relay_urls, out_path=None, k=3, use_lightning=False):
    """The real end-to-end path: resolve every host claiming to have this
    content, challenge-gate them, auction among survivors, optionally pay
    the winner over a real Lightning HTLC, download, and record the
    outcome to local reputation for next time."""
    from poc_reputation import ReputationStore

    candidates = discover_hosts_for(relay_urls, content_hash)
    if not candidates:
        sys.exit(f"no hosts found publishing content matching {content_hash}")
    print(f"found {len(candidates)} candidate host(s) for {content_hash[:16]}...")

    reputation = ReputationStore(os.path.expanduser('~/.dura_reputation.json'))
    winner = select_host(candidates, k=k, reputation=reputation)
    if winner is None:
        sys.exit("no candidate host passed the possession challenge — "
                 "refusing to download from an unverified source")

    c = winner['candidate']
    print(f"selected {c['host']} — price {winner['price']} sat, "
          f"reputation {winner['reputation']:.2f}, {winner['avg_latency_ms']:.1f}ms avg")

    if winner['price'] > 0:
        if use_lightning:
            import lightning_settle
            result = lightning_settle.settle(
                winner['price'], f"download {content_hash[:16]}... from {c['host']}")
            print(f"paid via real Lightning HTLC: {result['amount_sat']} sat, "
                  f"preimage {result['preimage'][:12]}... verified against "
                  f"payment_hash {result['payment_hash'][:12]}...")
        else:
            print(f"  price is {winner['price']} sat but --lightning not given "
                  f"— downloading anyway, unpaid (no enforcement in this demo)")

    path = download(winner['host'], winner['port'], out_path or c['title'])

    reputation.record_direct(c['signer_pubkey'], passes=1, fails=0,
                              avg_latency_ms=winner['avg_latency_ms'])
    reputation.save()
    print(f"recorded this download in local reputation store "
          f"(~/.dura_reputation.json) for {c['signer_pubkey'][:12]}...")
    return path


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
