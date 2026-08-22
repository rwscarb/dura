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
import threading
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from poc_reputation import Identity, verify_attestation, attestation_id
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

IDENTITY_PATH = os.path.expanduser('~/.dura_identity.key')
IDENTITY_ARMOR_HEADER = '-----BEGIN DURA IDENTITY KEY-----'
IDENTITY_ARMOR_FOOTER = '-----END DURA IDENTITY KEY-----'


def _armor_identity(raw_bytes):
    """Base64 text with a header/footer — NOT real OpenPGP armor (no
    CRC24, no packet framing), and not encryption: this only changes how
    the same private key bytes are encoded on disk, from opaque binary
    (`file` calls it "data") to something readable/diffable/copy-
    pasteable. A dura-specific label on purpose, not a PGP one — this
    repo already decided against adopting the real OpenPGP format (see
    README's "Transitive trust" section), so nothing here should look
    like it's actually PGP-compatible."""
    b64 = base64.b64encode(raw_bytes).decode()
    lines = [b64[i:i + 64] for i in range(0, len(b64), 64)]
    return IDENTITY_ARMOR_HEADER + '\n' + '\n'.join(lines) + '\n' + IDENTITY_ARMOR_FOOTER + '\n'


def _dearmor_identity(text):
    lines = text.strip().splitlines()
    if len(lines) < 2 or lines[0].strip() != IDENTITY_ARMOR_HEADER \
            or lines[-1].strip() != IDENTITY_ARMOR_FOOTER:
        raise ValueError('not a dura-armored identity key')
    return base64.b64decode(''.join(lines[1:-1]))


def load_or_create_identity():
    """A real node needs a stable pubkey across runs — regenerating fresh
    every invocation (like every other script tonight) would mean nobody
    could ever build reputation or subscribe to a host's key for real."""
    identity = Identity('local')
    if os.path.exists(IDENTITY_PATH):
        with open(IDENTITY_PATH, 'rb') as f:
            file_bytes = f.read()
        try:
            key_bytes = _dearmor_identity(file_bytes.decode())
        except (UnicodeDecodeError, ValueError):
            # pre-armor identity file (raw 32 bytes, no wrapper) — same key,
            # just not encoded yet. Re-armor it in place so this only ever
            # has to happen once; the underlying private key bytes (and
            # therefore the pubkey) are untouched.
            key_bytes = file_bytes
            with open(IDENTITY_PATH, 'w') as f:
                f.write(_armor_identity(key_bytes))
            os.chmod(IDENTITY_PATH, 0o600)
        identity._priv = Ed25519PrivateKey.from_private_bytes(key_bytes)
        identity.pub = identity._priv.public_key()
    else:
        key_bytes = identity._priv.private_bytes(
            serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
            serialization.NoEncryption())
        with open(IDENTITY_PATH, 'w') as f:
            f.write(_armor_identity(key_bytes))
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


class LineReader:
    """Buffered line reader for a connection that can receive more than
    one newline-terminated message per recv() — recv_line above assumes
    exactly one line arrives per call, true everywhere else in this
    protocol (strict request/response, never pipelined) but not true for
    the tunnel control channel: if two downloaders CONNECT to
    tunnel_relay.py around the same time, both NEWSTREAM messages can
    legitimately land in a single recv(), and recv_line would silently
    fold both into one malformed line and drop the second one."""

    def __init__(self, sock):
        self.sock = sock
        self.buf = b''

    def readline(self):
        while b'\n' not in self.buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                line, self.buf = self.buf, b''
                return line.decode().strip()
            self.buf += chunk
        line, self.buf = self.buf.split(b'\n', 1)
        return line.decode().strip()


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


def load_manifest_entries(archive_dir, file_name=None):
    """Every distinct file in the archive, not just one — find_manifest_entry
    collapses to a single entries[-1], which is exactly why `host <dir>` with
    no --file only ever served the single most-recently-added file out of a
    45-video archive. Dedupes by name (last-write-wins, same convention)."""
    archive_dir = os.path.expanduser(archive_dir)
    manifest_path = os.path.join(archive_dir, '.ott', 'manifest.jsonl')
    if not os.path.exists(manifest_path):
        sys.exit(f"no .ott/manifest.jsonl in {archive_dir} — archive a file with ott first")
    with open(manifest_path) as f:
        raw = [json.loads(line) for line in f if line.strip()]
    if file_name:
        raw = [e for e in raw if e['name'] == file_name]
    by_name = {}
    for e in raw:
        by_name[e['name']] = e
    entries = list(by_name.values())
    if not entries:
        sys.exit(f"no archived file found in {archive_dir}" + (f" matching {file_name}" if file_name else ""))
    return entries


def load_leaves(archive_dir, root_hash):
    archive_dir = os.path.expanduser(archive_dir)
    chunks_path = os.path.join(archive_dir, '.ott', 'chunks', f'{root_hash}.json')
    if not os.path.exists(chunks_path):
        sys.exit(f"no chunks file at {chunks_path} — the manifest entry for this hash has no "
                 f"matching chunk data in this archive_dir (stale/mismatched .ott/ state, or "
                 f"this isn't the archive_dir that file was actually added from)")
    with open(chunks_path) as f:
        return json.load(f)


def serve_session(conn, entries_by_hash, default_hash, price):
    """Handle every command on one connection, not just one — a download
    needs INFO + LEAVES + one FETCH per chunk (thousands, for a real
    archive), and reconnecting per command is what makes a tunneled
    connection (see tunnel_relay.py) pay a full relay rendezvous per
    chunk instead of once per session. Shared by the direct accept() loop
    below and by run_host_tunnel's per-stream data connections.

    entries_by_hash lets one server (one port) hold more than one file —
    a downloader picks which via SELECT <content_hash_or_prefix> before
    anything else. default_hash (set only when the host has exactly one
    file) means a single-file host never needs SELECT at all, so the
    `download --from host:port` discovery-skipping escape hatch and the
    tunnel path (already scoped to one file before this function runs)
    keep working unchanged."""
    selected = default_hash
    with conn:
        while True:
            line = recv_line(conn)
            if not line:
                break
            parts = line.split()
            if not parts:
                continue
            if parts[0] == 'SELECT':
                match = next((h for h in entries_by_hash if len(parts) > 1 and h.startswith(parts[1])), None)
                if match:
                    selected = match
                    conn.sendall(b'OK\n')
                else:
                    conn.sendall(b'ERR unknown content hash\n')
                continue
            if selected is None:
                conn.sendall(b'ERR this host serves more than one file '
                              b'- send SELECT <content_hash> first\n')
                continue
            entry, leaves, file_path = entries_by_hash[selected]
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


def run_host_server(archive_dir, file_name, port, bind_host='0.0.0.0', quiet=False, price=0):
    archive_dir = os.path.expanduser(archive_dir)
    entries = load_manifest_entries(archive_dir, file_name)
    entries_by_hash = {}
    for entry in entries:
        leaves = load_leaves(archive_dir, entry['sha256'])
        file_path = entry.get('last_path') or os.path.join(archive_dir, entry['name'])
        if not os.path.exists(file_path):
            sys.exit(f"archived file not found on disk at {file_path}")
        entries_by_hash[entry['sha256']] = (entry, leaves, file_path)
    default_hash = next(iter(entries_by_hash)) if len(entries_by_hash) == 1 else None

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((bind_host, port))
    srv.listen(8)
    if not quiet:
        # a background thread's print() races with cmd.Cmd's input()-driven
        # prompt on the same stdout — see run_relay_server's docstring for
        # why the shell passes quiet=True instead of patching this visually
        if default_hash:
            entry = entries[0]
            print(f"[host:{port}] serving {entry['name']} ({entry['size']:,} bytes, "
                  f"{entry['n_chunks']} chunks x {entry['chunk_size']} bytes)")
            print(f"[host:{port}] sha256/merkle root: {entry['sha256']}")
        else:
            total_size = sum(e['size'] for e in entries)
            print(f"[host:{port}] serving {len(entries)} files ({total_size:,} bytes total) "
                  f"— clients SELECT which one by content hash")

    while True:
        conn, _ = srv.accept()
        # a whole download now lives on one connection (see serve_session) —
        # accept() must hand off to a thread per connection, or one session
        # would block every other downloader until it finished
        threading.Thread(target=serve_session, args=(conn, entries_by_hash, default_hash, price),
                          daemon=True).start()


def run_host_tunnel(relay_host, relay_port, token, entry, leaves, file_path, price, quiet=False):
    """NAT-traversal path: instead of (or alongside) binding a locally
    reachable port, register with a tunnel_relay.py relay and serve every
    downloader it pairs us with. One persistent CONTROL connection stays
    open for the lifetime of hosting; each real downloader gets its own
    DATA connection, dialed back to the relay on demand (NEWSTREAM), so
    concurrent tunneled downloads don't block each other."""
    entries_by_hash = {entry['sha256']: (entry, leaves, file_path)}
    ctrl = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    ctrl.connect((relay_host, relay_port))
    ctrl.sendall(f'REGISTER {token}\n'.encode())
    if not quiet:
        print(f"[tunnel] registered {token[:16]}... with relay {relay_host}:{relay_port}")

    reader = LineReader(ctrl)
    while True:
        line = reader.readline()
        if not line:
            if not quiet:
                print(f"[tunnel] control connection to {relay_host}:{relay_port} closed")
            return
        parts = line.split()
        if parts and parts[0] == 'NEWSTREAM':
            stream_id = parts[1]
            data_conn = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            data_conn.connect((relay_host, relay_port))
            data_conn.sendall(f'DATA {stream_id}\n'.encode())
            threading.Thread(target=serve_session,
                              args=(data_conn, entries_by_hash, entry['sha256'], price),
                              daemon=True).start()


# ── client side ──────────────────────────────────────────────────────────

class HostConnection:
    """One persistent socket carrying every command for a session, direct
    or tunneled — see serve_session's docstring for why reconnecting per
    command (the old behavior) is untenable once a tunnel relay is in the
    path."""

    def __init__(self, sock):
        self.sock = sock

    @classmethod
    def connect_direct(cls, host, port, timeout=10):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect((host, port))
        return cls(sock)

    @classmethod
    def connect_via_tunnel(cls, relay_host, relay_port, token, timeout=10):
        """token is the content_hash the host registered under (see
        run_host_tunnel) — the tunnel relay has zero opinion on content,
        it just pairs this CONNECT with that host's next NEWSTREAM."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect((relay_host, relay_port))
        sock.sendall(f'CONNECT {token}\n'.encode())
        return cls(sock)

    def request(self, line):
        self.sock.sendall((line + '\n').encode())
        return recv_line(self.sock)

    def close(self):
        self.sock.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()


def _parse_tunnel(tunnel_addr):
    """'relay_host:relay_port' -> (relay_host, relay_port), or None."""
    if not tunnel_addr:
        return None
    relay_host, relay_port = tunnel_addr.rsplit(':', 1)
    return relay_host, int(relay_port)


def open_connection(host_addr, tunnel=None, content_hash=None, timeout=10):
    """host_addr is 'host:port' — used directly unless tunnel is given, in
    which case it's ignored and content_hash is used as the tunnel
    rendezvous token instead (the host isn't reachable at host_addr at
    all in that case)."""
    if tunnel:
        if not content_hash:
            raise ValueError("tunnel connection requires content_hash as the rendezvous token")
        relay_host, relay_port = tunnel
        return HostConnection.connect_via_tunnel(relay_host, relay_port, content_hash, timeout=timeout)
    host, port_s = host_addr.rsplit(':', 1)
    return HostConnection.connect_direct(host, int(port_s), timeout=timeout)


def download(host_addr, out_path, tunnel=None, content_hash=None, on_progress=None):
    from ott import merkle_root  # pip install btcvm

    out_path = os.path.expanduser(out_path)
    via = f"tunnel {tunnel[0]}:{tunnel[1]}" if tunnel else host_addr
    with open_connection(host_addr, tunnel=tunnel, content_hash=content_hash) as conn:
        if content_hash and not tunnel:
            # tunnel connections are already scoped to one file by the relay's
            # rendezvous token (see run_host_tunnel) — SELECT is only needed
            # against a direct multi-file host, which may be serving more than
            # just this content_hash on the same port
            sel = conn.request(f'SELECT {content_hash}')
            if sel != 'OK':
                sys.exit(f"host at {host_addr} rejected SELECT {content_hash[:16]}...: {sel}")
        info = json.loads(conn.request('INFO'))
        print(f"downloading {info['name']} ({info['size']:,} bytes, {info['n_chunks']} chunks) "
              f"from {via}")
        leaves = json.loads(conn.request('LEAVES'))
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
                resp = conn.request(f'FETCH {idx}')
                if not resp.startswith('DATA '):
                    sys.exit(f"chunk {idx}: bad response from host: {resp[:80]}")
                data = base64.b64decode(resp[5:])
                leaf = hashlib.sha256(data).hexdigest()
                if leaf != leaves[idx]:
                    sys.exit(f"chunk {idx}: hash mismatch — host sent bytes that don't match "
                             f"its own committed chunk hash. aborting download, not writing a "
                             f"corrupted/tampered file.")
                out.write(data)
                if on_progress:
                    on_progress(idx, info['n_chunks'])
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

def sample_challenge(conn, leaves, k=3):
    """Fetch k random chunks over the given (already-open) connection and
    verify each against the (already Merkle-verified) leaves list —
    proves *this specific host* truly holds real chunks, not just that
    someone somewhere does."""
    n = len(leaves)
    indices = random.sample(range(n), min(k, n))
    latencies = []
    for idx in indices:
        t0 = time.perf_counter()
        try:
            resp = conn.request(f'FETCH {idx}')
        except OSError:
            return False, latencies
        latencies.append((time.perf_counter() - t0) * 1000)
        if not resp.startswith('DATA '):
            return False, latencies
        data = base64.b64decode(resp[5:])
        if hashlib.sha256(data).hexdigest() != leaves[idx]:
            return False, latencies
    return True, latencies


def get_price(conn):
    try:
        resp = conn.request('PRICE')
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


def build_trust_graph(subscribe_events, root_pubkey, max_hops=3, decay=0.5):
    """Transitive trust, computed here instead of adopting PGP's standard
    to get it: BFS outward from root_pubkey through real signed subscribe
    events (each one a real trust edge — 'I subscribe to X' = 'I trust
    X'). Trust decays per hop, same shape as PGP's marginal-vs-full
    distinction (a direct subscribe counts more than a friend-of-a-
    friend), computed on the Ed25519 events already in place rather than
    needing the OpenPGP format at all.

    Takes the *shortest* path to each reachable pubkey (first time BFS
    reaches it), not the sum across every path that reaches it — summing
    would let a sybil ring inflate a target's trust just by adding more
    low-value paths to it, which defeats the point of a hop-decayed graph
    in the first place."""
    edges = {}
    for e in subscribe_events:
        p = e['payload']
        edges.setdefault(p['signer_pubkey'], set()).add(p['target_pubkey'])

    trust = {}
    frontier = [root_pubkey]
    seen = {root_pubkey}
    hop = 0
    while frontier and hop < max_hops:
        hop += 1
        weight = decay ** hop
        next_frontier = []
        for node in frontier:
            for target in edges.get(node, ()):
                if target not in seen:
                    seen.add(target)
                    trust[target] = weight
                    next_frontier.append(target)
        frontier = next_frontier
    return trust


def select_host(candidates, k=3, reputation=None, trust_in_signers=None):
    """Gate on real possession, then rank survivors by reputation then
    price — same 'challenge gates the auction' shape as
    poc_challenge_auction.py's naive-vs-gated comparison. trust_in_signers
    (from build_trust_graph) lets a candidate with zero *direct* history
    still score above 0 if someone in the caller's transitive trust graph
    has attested to it."""
    from ott import merkle_root

    scored = []
    for c in candidates:
        tunnel = _parse_tunnel(c.get('tunnel'))
        try:
            with open_connection(c['host'], tunnel=tunnel, content_hash=c['content_hash']) as conn:
                if not tunnel:
                    sel = conn.request(f"SELECT {c['content_hash']}")
                    if sel != 'OK':
                        print(f"  x {c['host']}: SELECT rejected ({sel}) — skipping")
                        continue
                leaves = json.loads(conn.request('LEAVES'))
                info = json.loads(conn.request('INFO'))
                if merkle_root(leaves) != info['sha256'] or info['sha256'] != c['content_hash']:
                    print(f"  x {c['host']}: advertised content doesn't match its own LEAVES/INFO — skipping")
                    continue
                passed, latencies = sample_challenge(conn, leaves, k=k)
                if not passed:
                    print(f"  x {c['host']}: failed possession challenge ({k} chunks sampled) — skipping")
                    continue
                price = get_price(conn)
        except OSError as e:
            print(f"  x {c['host']}: unreachable ({e})")
            continue
        rep_score, rep_why = (reputation.trust_score(c['signer_pubkey'], trust_in_signers)
                               if reputation else (0.5, 'no reputation store'))
        avg_latency = sum(latencies) / len(latencies) if latencies else 0.0
        print(f"  + {c['host']}: possession verified ({k}/{k} chunks), price={price} sat, "
              f"reputation={rep_score:.2f} ({rep_why}), avg_latency={avg_latency:.1f}ms")
        scored.append({'candidate': c, 'info': info, 'price': price,
                        'reputation': rep_score, 'avg_latency_ms': avg_latency})

    if not scored:
        return None
    scored.sort(key=lambda s: (-s['reputation'], s['price']))  # highest trust first, then cheapest
    return scored[0]


def fetch_verified(relay_urls, event_type):
    """Fetch+verify every event of a type across relays, deduped by event
    id — same merge pattern discover() uses, generalized so subscribe/
    attestation gossip go through the same real signature-checking path."""
    seen = {}
    for relay_url in relay_urls:
        events = fetch_events(relay_url, event_type)
        if events is None:
            continue
        for e in events:
            ok, _ = verify_attestation(e)
            if ok:
                seen[attestation_id(e)] = e
    return list(seen.values())


def download_with_auction(content_hash, relay_urls, out_path=None, k=3, use_lightning=False,
                           trust_hops=3, trust_decay=0.5, on_progress=None):
    """The real end-to-end path: resolve every host claiming to have this
    content, challenge-gate them, auction among survivors — weighted by
    reputation built from real transitive trust (your own subscribes, plus
    your subscribes' subscribes, decayed per hop) and real attestations
    gossiped from relays, not just your own direct history — optionally
    pay the winner over a real Lightning HTLC, download, record the
    outcome locally AND publish it so others can benefit transitively too."""
    from poc_reputation import ReputationStore

    identity = load_or_create_identity()
    candidates = discover_hosts_for(relay_urls, content_hash)
    if not candidates:
        sys.exit(f"no hosts found publishing content matching {content_hash}")
    print(f"found {len(candidates)} candidate host(s) for {content_hash[:16]}...")

    subscribe_events = fetch_verified(relay_urls, 'subscribe')
    trust_graph = build_trust_graph(subscribe_events, identity.pubkey_hex(),
                                     max_hops=trust_hops, decay=trust_decay)
    print(f"trust graph: {len(trust_graph)} pubkey(s) reachable within {trust_hops} hop(s) "
          f"of your own subscribes")

    reputation = ReputationStore(os.path.expanduser('~/.dura_reputation.json'))
    attestation_events = fetch_verified(relay_urls, 'attestation')
    added = 0
    for e in attestation_events:
        ok, _ = reputation.add_attestation(e)
        added += ok
    if attestation_events:
        print(f"pulled {added}/{len(attestation_events)} real attestation(s) from relays "
              f"(others' vouches, weighted by your trust in whoever signed them)")

    winner = select_host(candidates, k=k, reputation=reputation, trust_in_signers=trust_graph)
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

    path = download(c['host'], out_path or c['title'], tunnel=_parse_tunnel(c.get('tunnel')),
                     content_hash=c['content_hash'], on_progress=on_progress)

    reputation.record_direct(c['signer_pubkey'], passes=1, fails=0,
                              avg_latency_ms=winner['avg_latency_ms'])
    reputation.save()
    print(f"recorded this download in local reputation store "
          f"(~/.dura_reputation.json) for {c['signer_pubkey'][:12]}...")

    attestation = identity.sign_event('attestation', peer_pubkey=c['signer_pubkey'],
                                       passes=1, fails=0, avg_latency_ms=winner['avg_latency_ms'], k=k)
    for relay_url in relay_urls:
        post_event(relay_url, attestation)
    print(f"published this outcome as a real attestation — anyone who trusts you "
          f"(directly or transitively) now benefits from it too, without dealing with "
          f"{c['signer_pubkey'][:12]}... themselves first")
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
    except (urllib.error.URLError, ConnectionRefusedError) as e:
        # download_with_auction posts the post-download attestation to every
        # relay in relay_urls, which by default includes 127.0.0.1:9101
        # whether or not anything is actually listening there (same default
        # fetch_events already tolerates) — an unreachable relay here used to
        # take down the whole download with an uncaught traceback *after* the
        # file was already correctly written and reputation already recorded
        return {'ok': False, 'error': str(e)}


def fetch_events(relay_url, event_type=None):
    url = f'{relay_url}/events' + (f'?type={event_type}' if event_type else '')
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, ConnectionRefusedError):
        return None


def publish(identity, relay_url, content_hash, title, host_addr, tunnel=None):
    """tunnel, if given, is 'relay_host:relay_port' for a tunnel_relay.py
    instance this host registered with — additive and backward compatible,
    same as the optional PRICE wire verb: an event without it just means
    'connect directly to host_addr', same as before this field existed."""
    event = identity.sign_event('publish', content_hash=content_hash, title=title,
                                 host=host_addr, tunnel=tunnel)
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
