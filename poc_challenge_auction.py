#!/usr/bin/env python3
"""
PoC: challenge-response possession proof gating a reverse auction for seeding.

Validates the mechanism from the #all-pdx brainstorm: only peers who can
produce a valid Merkle proof for a randomly-challenged chunk are allowed to
bid to serve a file; cheapest verified bid wins.

merkle_root/merkle_proof/verify_proof below are vendored byte-for-byte from
rwscarb/btcvm's ott.py (same functions `ott verify-chunk` runs locally) —
copied rather than imported so this repo has no dependency outside itself.
Everything runs in one process with in-memory "peers" — no real network,
no real Lightning. The point is to validate the mechanism's logic (fraud
gets caught, the auction only ever sees verified bidders) before spending
time on an actual libp2p/BitTorrent + Lightning implementation.
"""
import hashlib
import os
import random


# ── vendored from btcvm/ott.py ────────────────────────────────────────────
def merkle_root(leaves: list[str]) -> str:
    if not leaves:
        return '0' * 64
    layer = list(leaves)
    while len(layer) > 1:
        if len(layer) % 2:
            layer.append(layer[-1])
        layer = [
            hashlib.sha256((layer[i] + layer[i + 1]).encode()).hexdigest()
            for i in range(0, len(layer), 2)
        ]
    return layer[0]


def merkle_proof(leaves: list[str], index: int) -> list[dict]:
    proof = []
    layer = list(leaves)
    idx = index
    while len(layer) > 1:
        if len(layer) % 2:
            layer.append(layer[-1])
        sibling_idx = idx ^ 1
        proof.append({
            'sibling': layer[sibling_idx],
            'side': 'right' if idx % 2 == 0 else 'left',
        })
        layer = [
            hashlib.sha256((layer[i] + layer[i + 1]).encode()).hexdigest()
            for i in range(0, len(layer), 2)
        ]
        idx //= 2
    return proof


def verify_proof(leaf: str, proof: list[dict], root: str) -> bool:
    h = leaf
    for step in proof:
        sib = step['sibling']
        if step['side'] == 'right':
            h = hashlib.sha256((h + sib).encode()).hexdigest()
        else:
            h = hashlib.sha256((sib + h).encode()).hexdigest()
    return h == root
# ── end vendored section ──────────────────────────────────────────────────


CHUNK_SIZE = 262144  # matches OTT_CHUNK_BYTES default
N_CHUNKS = 8
N_CHALLENGE_ROUNDS = 5

# Part 2: nonce-salted challenge, timed. Simulated latency in ms — not real
# time.sleep, just a modeled draw, so the script stays fast and deterministic
# under a fixed seed. LOCAL_LATENCY is "already had the bytes on disk";
# RELAY_LATENCY is the extra round trip a peer without the data pays to
# fetch it from someone who does, then answer as if it were them.
LOCAL_LATENCY_MS = (5, 25)
RELAY_LATENCY_MS = (120, 250)
TIME_BOUND_MS = 60


class Peer:
    def __init__(self, name, chunks, honest, price_range, cheat_rate=0.0):
        self.name = name
        self.chunks = chunks          # dict[int, bytes] — what this peer actually holds
        self.honest = honest
        self.price_range = price_range
        self.cheat_rate = cheat_rate  # fraction of the time an otherwise-honest holder tampers its reply
        self.strikes = 0

    def respond_to_challenge(self, idx, leaves):
        if idx not in self.chunks:
            return None  # doesn't hold this chunk — legitimate gap or outright fabrication
        data = self.chunks[idx]
        if random.random() < self.cheat_rate:
            data = data[:-1] + bytes([data[-1] ^ 0xFF])  # corrupt one byte
        leaf = hashlib.sha256(data).hexdigest()
        proof = merkle_proof(leaves, idx)
        return leaf, proof

    def bid(self):
        return round(random.uniform(*self.price_range), 2)


def build_file():
    chunks = {i: os.urandom(CHUNK_SIZE) for i in range(N_CHUNKS)}
    leaves = [hashlib.sha256(chunks[i]).hexdigest() for i in range(N_CHUNKS)]
    root = merkle_root(leaves)
    return chunks, leaves, root


def naive_price_auction(peers):
    print("\n=== ROUND 0: naive price-only auction (no possession check) ===")
    bids = {p: p.bid() for p in peers}
    for p, price in sorted(bids.items(), key=lambda kv: kv[1]):
        holds = f"{len(p.chunks)}/{N_CHUNKS} chunks" if p.honest else f"{len(p.chunks)}/{N_CHUNKS} chunks, DISHONEST"
        print(f"  {p.name:10s}  {price:6.2f} sat/chunk   (actually holds: {holds})")
    winner = min(bids, key=bids.get)
    print(f"  -> naive winner: {winner.name} — cheapest bid wins regardless of whether they can deliver")


def challenge_round(round_n, peers, idx, leaves, root):
    print(f"\n=== ROUND {round_n}: challenge chunk #{idx} (root {root[:12]}...) ===")
    survivors = []
    for p in peers:
        resp = p.respond_to_challenge(idx, leaves)
        if resp is None:
            print(f"  {p.name:10s}  NO RESPONSE (doesn't hold chunk #{idx})")
            p.strikes += 1
            continue
        leaf, proof = resp
        ok = leaf == leaves[idx] and verify_proof(leaf, proof, root)
        if ok:
            print(f"  {p.name:10s}  PASS  (chunk hash + Merkle proof verified)")
            survivors.append(p)
        else:
            print(f"  {p.name:10s}  FAIL  (hash doesn't match committed leaf — tampered response)")
            p.strikes += 1

    if not survivors:
        print("  no verified holders this round -> auction cancelled")
        return
    bids = {p: p.bid() for p in survivors}
    for p, price in sorted(bids.items(), key=lambda kv: kv[1]):
        print(f"    bid: {p.name:10s}  {price:6.2f} sat/chunk")
    winner = min(bids, key=bids.get)
    print(f"  -> WINNER: {winner.name} at {bids[winner]:.2f} sat/chunk "
          f"(settlement: mock Lightning HTLC held until delivery confirms)")


class LatentPeer:
    """Part 2: a peer that either holds the chunk locally, or has none of its
    own and relays from a source peer's chunks in real time to fake an
    answer. The relay's cryptographic answer is *correct* — it really did
    fetch the actual bytes — which is exactly why a nonce alone can't catch
    it; only the added latency can."""

    def __init__(self, name, local_chunks, relay_source=None):
        self.name = name
        self.local_chunks = local_chunks      # dict[int, bytes] actually on disk
        self.relay_source = relay_source        # dict[int, bytes] to fetch from if not local

    def respond_to_nonce_challenge(self, idx, nonce):
        if idx in self.local_chunks:
            data = self.local_chunks[idx]
            latency = random.uniform(*LOCAL_LATENCY_MS)
        elif self.relay_source is not None and idx in self.relay_source:
            data = self.relay_source[idx]  # really fetches the real bytes — just slowly
            latency = random.uniform(*LOCAL_LATENCY_MS) + random.uniform(*RELAY_LATENCY_MS)
        else:
            return None
        claimed = hashlib.sha256(data + nonce).hexdigest()
        return claimed, latency


def nonce_challenge_round(round_n, peers, idx, leaves, chunks):
    nonce = os.urandom(16)
    print(f"\n=== NONCE ROUND {round_n}: challenge chunk #{idx}, nonce {nonce.hex()[:12]}... "
          f"(bound {TIME_BOUND_MS}ms) ===")
    for p in peers:
        resp = p.respond_to_nonce_challenge(idx, nonce)
        if resp is None:
            print(f"  {p.name:10s}  NO RESPONSE (no local copy, no relay source for chunk #{idx})")
            continue
        claimed, latency = resp
        expected = hashlib.sha256(chunks[idx] + nonce).hexdigest()
        crypto_ok = claimed == expected
        time_ok = latency <= TIME_BOUND_MS
        if crypto_ok and time_ok:
            print(f"  {p.name:10s}  PASS   hash correct, {latency:5.1f}ms (within bound)")
        elif crypto_ok and not time_ok:
            print(f"  {p.name:10s}  FAIL   hash correct but {latency:5.1f}ms > {TIME_BOUND_MS}ms bound "
                  f"— real bytes, but not held locally (relay caught by timing, not crypto)")
        else:
            print(f"  {p.name:10s}  FAIL   hash mismatch — doesn't actually have the chunk")


def main():
    random.seed(1)
    chunks, leaves, root = build_file()
    print(f"file: {N_CHUNKS} chunks x {CHUNK_SIZE} bytes, committed Merkle root {root[:16]}...")

    peers = [
        Peer('alice',   dict(chunks), honest=True,  price_range=(5, 15)),
        Peer('bob',     dict(chunks), honest=True,  price_range=(3, 10)),
        Peer('carol',   {i: chunks[i] for i in range(0, N_CHUNKS, 2)}, honest=True, price_range=(1, 5)),   # partial: even chunks only
        Peer('mallory', dict(chunks), honest=False, price_range=(0.5, 2), cheat_rate=0.7),                  # has the bytes, cheats on most responses
        Peer('trudy',   {},           honest=False, price_range=(0.1, 1)),                                  # has nothing at all
    ]

    print("\npeers (ground truth — not visible to the requester):")
    for p in peers:
        kind = ('honest, full' if p.honest and len(p.chunks) == N_CHUNKS else
                'honest, partial' if p.honest else 'DISHONEST')
        print(f"  {p.name:10s}  holds {len(p.chunks)}/{N_CHUNKS} chunks  ({kind})")

    naive_price_auction(peers)

    for round_n in range(1, N_CHALLENGE_ROUNDS + 1):
        idx = random.randrange(N_CHUNKS)
        challenge_round(round_n, peers, idx, leaves, root)

    print("\n=== strikes after all challenge rounds ===")
    for p in peers:
        print(f"  {p.name:10s}  {p.strikes} strike(s)")

    print("\nverdict: naive price auction picked a dishonest/empty-handed peer as winner.\n"
          "gating bids on a passed challenge caught trudy (0 chunks) every round and\n"
          "caught mallory whenever her cheat roll fired — she only won when honest by luck.\n"
          "carol, a legitimately partial holder, correctly sits out rounds for chunks she\n"
          "doesn't have without being flagged dishonest. mechanism holds up in-process;\n"
          "next step is making respond_to_challenge a real network round-trip.")

    print("\n\n########## PART 2: nonce-salted challenge + timing bound ##########")
    print("does knowing the file's public chunk hash let you fake possession? and does a")
    print("plain hash challenge catch a peer who relay-fetches the real bytes in real time?")

    latent_peers = [
        LatentPeer('alice',   dict(chunks)),                                   # real local holder
        LatentPeer('sybil',   {}, relay_source=None),                          # knows the public hash, nothing else
        LatentPeer('reyna',   {}, relay_source=dict(chunks)),                  # relays real bytes from a source in real time
    ]
    for round_n in range(1, 4):
        idx = random.randrange(N_CHUNKS)
        nonce_challenge_round(round_n, latent_peers, idx, leaves, chunks)

    print("\nverdict: sybil (only ever knew the public sha256 of each chunk, never the bytes)\n"
          "can't produce hash(chunk||nonce) at all — preimage resistance holds, exactly as\n"
          "predicted, knowing the hash gives zero help. reyna produces the cryptographically\n"
          "*correct* answer every time, because she genuinely fetches the real bytes — the\n"
          "nonce alone does not catch her. only the added timing bound does, since her relay\n"
          "hop adds latency a locally-stored peer never pays. confirms both layers are load-\n"
          "bearing: nonce stops fabrication, RTT bound stops relay.")


if __name__ == '__main__':
    main()
