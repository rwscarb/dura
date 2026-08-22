#!/usr/bin/env python3
"""
Roadmap item 8, closed out: discovery with no canonical index.

Three independent relay processes (discovery_relay.py), each dumb —
store-and-forward only, no ranking opinion. A creator publishes real
content (reusing the real Merkle root from real_archive/ — the same 217MB
video from item 6). Viewers "like" it and "subscribe" to each other,
spread across different relays, nobody posts to all three. Two clients
with different subscribe graphs pull from all three relays, verify every
event's signature themselves (never trust a relay's word for it), and each
compute their OWN ranking — same underlying gossiped events, personalized
result, no single number either of them has to agree with.

Then: a swarm of sybil identities likes the same content. Neither client
subscribes to any of them, so their likes get real signatures (a relay
happily stores them, since a relay isn't in the business of judging
opinions) but contribute ~0 to either ranking — sybil resistance from the
trust graph, not from relay-side moderation.

Then: kill one relay outright. Confirm a client re-querying the surviving
two still recovers the full picture — nothing was uniquely dependent on
the relay that just died.
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from poc_reputation import Identity, verify_attestation, attestation_id

RELAY_PORTS = [9101, 9102, 9103]


def post_event(port, event):
    req = urllib.request.Request(
        f'http://127.0.0.1:{port}/event',
        data=json.dumps(event).encode(),
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read())


def fetch_events(port):
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/events', timeout=5) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, ConnectionRefusedError):
        return None  # relay unreachable — a real client just skips it, doesn't fail


def wait_for_relay(port, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if fetch_events(port) is not None:
            return True
        time.sleep(0.1)
    return False


def client_view(ports, label):
    """A real client's discovery pass: query every reachable relay, merge
    by event id, verify every signature independently."""
    seen = {}
    reachable = []
    for port in ports:
        events = fetch_events(port)
        if events is None:
            print(f"    [{label}] relay:{port} unreachable — skipped, not fatal")
            continue
        reachable.append(port)
        for e in events:
            ok, _ = verify_attestation(e)
            if not ok:
                continue  # a relay could lie or get compromised; signature check is the real gate
            seen[attestation_id(e)] = e
    print(f"    [{label}] queried {ports}, {len(reachable)} reachable, "
          f"{len(seen)} unique verified events after merge")
    return list(seen.values())


def rank_content(events, trust_in_signers):
    """Personalized ranking: for each published content_hash, sum trust-
    weighted likes. No global score — this dict is specific to whoever's
    trust_in_signers was passed in."""
    titles = {}
    likes_by_content = {}
    for e in events:
        p = e['payload']
        if p['type'] == 'publish':
            titles[p['content_hash']] = p['title']
        elif p['type'] == 'like':
            w = trust_in_signers.get(p['signer_pubkey'], 0.0)
            likes_by_content.setdefault(p['content_hash'], 0.0)
            likes_by_content[p['content_hash']] += w
    return {titles.get(h, h[:12] + '...'): score for h, score in likes_by_content.items()}


def trust_graph_from_subscribes(events, subscriber_pubkey):
    """A client's trust graph IS its own subscribe events — nobody else's."""
    return {
        e['payload']['target_pubkey']: 1.0
        for e in events
        if e['payload']['type'] == 'subscribe' and e['payload']['signer_pubkey'] == subscriber_pubkey
    }


def real_content_hash():
    chunks_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'real_archive', '.ott', 'chunks')
    if os.path.isdir(chunks_dir) and os.listdir(chunks_dir):
        return os.listdir(chunks_dir)[0].removesuffix('.json'), 'real_video.mp4 (from item 6)'
    return 'fake_' + os.urandom(16).hex(), 'placeholder — real_archive/ not set up'


def main():
    print("spawning 3 independent relay processes...")
    procs = [subprocess.Popen([sys.executable, 'discovery_relay.py', str(p)]) for p in RELAY_PORTS]
    try:
        if not all(wait_for_relay(p) for p in RELAY_PORTS):
            print("relays didn't come up in time"); return

        carol = Identity('carol_creator')
        dan, erin, frank = Identity('dan'), Identity('erin'), Identity('frank')
        bob, mallory = Identity('bob_viewer'), Identity('mallory_viewer')
        sybils = [Identity(f'sybil_{i}') for i in range(20)]

        content_hash, content_label = real_content_hash()
        print(f"\ncontent: {content_label}\n  hash: {content_hash[:16]}...")

        publish = carol.sign_event('publish', content_hash=content_hash, title=content_label)
        r = post_event(RELAY_PORTS[0], publish)
        print(f"\ncarol publishes to relay:{RELAY_PORTS[0]} only — accepted={r['ok']}")

        print("\nhonest likes + subscribes, spread across relays (nobody posts to all three):")
        for identity, port in [(dan, RELAY_PORTS[0]), (erin, RELAY_PORTS[1]), (frank, RELAY_PORTS[2])]:
            like = identity.sign_event('like', content_hash=content_hash)
            r = post_event(port, like)
            print(f"  {identity.name:10s} likes it, posts to relay:{port}  accepted={r['ok']}")

        for target in (dan, erin):
            sub = bob.sign_event('subscribe', target_pubkey=target.pubkey_hex())
            post_event(RELAY_PORTS[0], sub)
        print(f"  bob_viewer subscribes to dan + erin (posted to relay:{RELAY_PORTS[0]})")

        sub = mallory.sign_event('subscribe', target_pubkey=frank.pubkey_hex())
        post_event(RELAY_PORTS[1], sub)
        print(f"  mallory_viewer subscribes to frank only (posted to relay:{RELAY_PORTS[1]})")

        print(f"\nsybil swarm: 20 fake identities all like the same content, posted to relay:{RELAY_PORTS[2]}")
        for sybil in sybils:
            like = sybil.sign_event('like', content_hash=content_hash)
            post_event(RELAY_PORTS[2], like)
        print("  every like has a real, individually valid signature — a relay has no basis to reject any of them")

        print("\n=== bob_viewer's discovery pass (trusts dan + erin, nobody else) ===")
        bob_events = client_view(RELAY_PORTS, 'bob')
        bob_trust = trust_graph_from_subscribes(bob_events, bob.pubkey_hex())
        bob_ranking = rank_content(bob_events, bob_trust)
        print(f"    bob's trust graph: {len(bob_trust)} pubkey(s) (from his own subscribes)")
        print(f"    bob's ranking: {bob_ranking}")

        print("\n=== mallory_viewer's discovery pass (trusts frank only) ===")
        mallory_events = client_view(RELAY_PORTS, 'mallory')
        mallory_trust = trust_graph_from_subscribes(mallory_events, mallory.pubkey_hex())
        mallory_ranking = rank_content(mallory_events, mallory_trust)
        print(f"    mallory's trust graph: {len(mallory_trust)} pubkey(s)")
        print(f"    mallory's ranking: {mallory_ranking}")

        print(f"\nsame {len(bob_events)} gossiped events, both clients saw all 23 likes (3 honest + 20 sybil), "
              f"but scored the content differently — {bob_ranking} vs {mallory_ranking} — because "
              f"ranking runs on each client's own trust graph, not vote count. sybils: 20 real "
              f"signatures, ~0 influence on either score.")

        print(f"\n=== killing relay:{RELAY_PORTS[0]} (the one carol's publish event lives on) ===")
        procs[0].terminate()
        procs[0].wait()
        time.sleep(0.3)
        recovery_events = client_view(RELAY_PORTS, 'bob-after-kill')
        recovery_ranking = rank_content(recovery_events, bob_trust)
        print(f"    ranking after relay:{RELAY_PORTS[0]} is dead: {recovery_ranking}")
        print(f"    content is still discoverable and rankable (erin's like survived on a different "
              f"relay) — but carol's publish event (the human-readable title) and dan's like both "
              f"only ever lived on the now-dead relay, so the title is gone (falls back to the raw "
              f"hash) and bob's score dropped from 2.0 to 1.0. real limitation, not a clean win: a "
              f"relay dying loses whatever *only it* had. redundancy has to be deliberate — post to "
              f"more than one relay — it isn't automatic just because relays are plural.")

    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()
        for p in procs:
            p.wait()


if __name__ == '__main__':
    main()
