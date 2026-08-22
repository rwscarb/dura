#!/usr/bin/env python3
"""
Persistent local reputation + signed portable attestations — the two
mechanisms from the last two Slack messages, actually built.

1. Local reputation list: a client's own record of "how has peer X
   behaved with me," built from real challenge-response outcomes
   (poc_network_challenge.py) and persisted across runs.

2. Signed attestations: a client can sign its own verification outcome
   for a peer with its own Ed25519 key, and hand that signed blob to
   another client — who didn't do the verification themselves, but can
   check the signature and decide how much to trust it, exactly PGP's
   Web of Trust model, applied to possession-verification results
   instead of identity.

3. Revocation: a signer can kill their own earlier vouch — for a peer that
   went bad after being vouched for, or a vouch that was simply wrong.
   Only the original signer's key can revoke it (verified by real signature,
   same as the attestation itself); the revoked attestation stays on record
   rather than being deleted, so "X vouched for Y, then revoked it" remains
   an honest, auditable fact instead of quietly disappearing.

Demo below runs all real (Ed25519 sign/verify via `cryptography`, not
faked) and shows: (a) a fresh client with zero direct history can bootstrap
trust in an unknown peer purely from another client's signed vouching, (b)
tampering with a signed attestation after the fact is caught, (c) an
attestation's trust weight decays with age instead of being trusted
forever, (d) a signer can revoke their own vouch and it actually moves the
score, (e) nobody else can forge a revocation of someone else's vouch.
"""
import hashlib
import json
import os
import time

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives import serialization
from cryptography.exceptions import InvalidSignature

ATTESTATION_HALF_LIFE_DAYS = 30.0  # trust weight halves every 30 days without a fresh re-verification


class Identity:
    """An Ed25519 keypair for one client. Real signing keys, generated fresh
    per run here — in a real client this would persist to disk once."""

    def __init__(self, name):
        self.name = name
        self._priv = Ed25519PrivateKey.generate()
        self.pub = self._priv.public_key()

    def pubkey_hex(self):
        return self.pub.public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()

    def sign_attestation(self, peer_pubkey_hex, passes, fails, avg_latency_ms, k, ts=None):
        payload = {
            'signer_pubkey': self.pubkey_hex(),
            'peer_pubkey': peer_pubkey_hex,
            'passes': passes,
            'fails': fails,
            'avg_latency_ms': round(avg_latency_ms, 3),
            'k': k,
            'ts': ts if ts is not None else time.time(),
        }
        payload_bytes = json.dumps(payload, sort_keys=True).encode()
        sig = self._priv.sign(payload_bytes)
        return {'payload': payload, 'signature': sig.hex()}

    def sign_event(self, event_type, ts=None, **fields):
        """General-purpose signed event — same shape as attestations/
        revocations, used by discovery_relay.py/poc_discovery.py for
        publish/like/subscribe events. Kept generic rather than one method
        per event type since a relay never needs to know the field schema,
        only that the signature is real."""
        payload = {
            'type': event_type,
            'signer_pubkey': self.pubkey_hex(),
            'ts': ts if ts is not None else time.time(),
            **fields,
        }
        payload_bytes = json.dumps(payload, sort_keys=True).encode()
        sig = self._priv.sign(payload_bytes)
        return {'payload': payload, 'signature': sig.hex()}

    def sign_revocation(self, target_id, reason='', ts=None):
        payload = {
            'type': 'revocation',
            'signer_pubkey': self.pubkey_hex(),
            'revokes': target_id,
            'reason': reason,
            'ts': ts if ts is not None else time.time(),
        }
        payload_bytes = json.dumps(payload, sort_keys=True).encode()
        sig = self._priv.sign(payload_bytes)
        return {'payload': payload, 'signature': sig.hex()}


def attestation_id(attestation):
    """Stable content-hash identifier for an attestation, so a revocation can
    reference exactly which vouch it kills without a central sequence number."""
    payload_bytes = json.dumps(attestation['payload'], sort_keys=True).encode()
    return hashlib.sha256(payload_bytes).hexdigest()


def verify_attestation(attestation):
    """Real Ed25519 verification against the embedded signer pubkey. Returns
    (valid: bool, reason: str)."""
    payload = attestation['payload']
    payload_bytes = json.dumps(payload, sort_keys=True).encode()
    try:
        signer_pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(payload['signer_pubkey']))
        signer_pub.verify(bytes.fromhex(attestation['signature']), payload_bytes)
        return True, 'signature valid'
    except InvalidSignature:
        return False, 'signature invalid — payload was tampered with after signing'
    except Exception as e:
        return False, f'malformed attestation: {e}'


def attestation_weight(attestation):
    """Decayed confidence in an attestation based on its age — a six-month-old
    'this peer was good' claim shouldn't count as much as one from today."""
    age_days = (time.time() - attestation['payload']['ts']) / 86400
    return 0.5 ** (age_days / ATTESTATION_HALF_LIFE_DAYS)


class ReputationStore:
    """One client's local view: direct experience (highest confidence,
    never shared) plus signed attestations received from other clients
    (portable, weighted by how much *I* trust the signer)."""

    def __init__(self, path):
        self.path = path
        self.direct = {}        # peer_pubkey -> {passes, fails, avg_latency_ms, last_ts}
        self.attestations = []  # list of received signed attestations
        self.revocations = []   # list of received signed revocations
        self._load()

    def _load(self):
        if os.path.exists(self.path):
            with open(self.path) as f:
                data = json.load(f)
            self.direct = data.get('direct', {})
            self.attestations = data.get('attestations', [])
            self.revocations = data.get('revocations', [])

    def save(self):
        with open(self.path, 'w') as f:
            json.dump({'direct': self.direct, 'attestations': self.attestations,
                       'revocations': self.revocations}, f, indent=2)

    def record_direct(self, peer_pubkey, passes, fails, avg_latency_ms):
        self.direct[peer_pubkey] = {
            'passes': passes, 'fails': fails,
            'avg_latency_ms': avg_latency_ms, 'last_ts': time.time(),
        }

    def add_attestation(self, attestation):
        ok, reason = verify_attestation(attestation)
        if not ok:
            return False, reason
        self.attestations.append(attestation)
        return True, 'accepted'

    def add_revocation(self, revocation):
        """Only accepted if it's a real signature AND the revoker is the
        same key that signed the attestation being revoked — otherwise
        anyone could forge a revocation to silently kill someone else's
        legitimate vouch. The original attestation is kept (not deleted):
        'X vouched for Y, then revoked it' stays an honest, auditable fact."""
        ok, reason = verify_attestation(revocation)
        if not ok:
            return False, reason
        target_id = revocation['payload']['revokes']
        target = next((a for a in self.attestations if attestation_id(a) == target_id), None)
        if target is None:
            return False, 'revokes an attestation this store has never seen'
        if target['payload']['signer_pubkey'] != revocation['payload']['signer_pubkey']:
            return False, 'revocation signer does not match the original attestation signer — rejected'
        self.revocations.append(revocation)
        return True, 'accepted'

    def _revoked_ids(self):
        return {r['payload']['revokes'] for r in self.revocations}

    def trust_score(self, peer_pubkey, trust_in_signers=None):
        """Direct experience always dominates when present. Otherwise,
        blend signed, non-revoked attestations weighted by (a) how much I
        trust that signer and (b) how stale the attestation is."""
        trust_in_signers = trust_in_signers or {}
        if peer_pubkey in self.direct:
            rec = self.direct[peer_pubkey]
            total = rec['passes'] + rec['fails']
            return rec['passes'] / total if total else 0.0, 'direct experience'

        revoked = self._revoked_ids()
        relevant = [a for a in self.attestations
                    if a['payload']['peer_pubkey'] == peer_pubkey and attestation_id(a) not in revoked]
        n_revoked = sum(1 for a in self.attestations
                         if a['payload']['peer_pubkey'] == peer_pubkey and attestation_id(a) in revoked)
        if not relevant:
            if n_revoked:
                return 0.0, f'all {n_revoked} attestation(s) for this peer were revoked by their own signers'
            return 0.0, 'no direct history, no attestations — unknown peer'

        num, den = 0.0, 0.0
        for a in relevant:
            signer = a['payload']['signer_pubkey']
            my_trust = trust_in_signers.get(signer, 0.0)
            if my_trust <= 0:
                continue
            w = my_trust * attestation_weight(a)
            p = a['payload']['passes']
            f = a['payload']['fails']
            score = p / (p + f) if (p + f) else 0.0
            num += w * score
            den += w
        if den == 0:
            return 0.0, 'attestations exist but from signers I have zero trust in'
        revoked_note = f', {n_revoked} revoked and excluded' if n_revoked else ''
        return num / den, f'{len(relevant)} attestation(s){revoked_note}, weighted by signer trust + age'


def main():
    for p in ('/tmp/poc_rep_alice.json', '/tmp/poc_rep_bob.json'):
        if os.path.exists(p):
            os.remove(p)

    print("=== identities (real Ed25519 keypairs) ===")
    alice = Identity('alice')   # verifies peers directly, will vouch for them
    bob = Identity('bob')       # fresh client, never met "peer_x" directly
    print(f"  alice pubkey: {alice.pubkey_hex()[:16]}...")
    print(f"  bob   pubkey: {bob.pubkey_hex()[:16]}...")

    peer_x_pubkey = 'peerX_' + os.urandom(8).hex()  # stand-in for a real peer identity

    print("\n=== alice directly verifies peer_x (8/8 real challenge passes, from earlier work) ===")
    alice_store = ReputationStore('/tmp/poc_rep_alice.json')
    alice_store.record_direct(peer_x_pubkey, passes=8, fails=0, avg_latency_ms=0.18)
    score, why = alice_store.trust_score(peer_x_pubkey)
    print(f"  alice's own trust score for peer_x: {score:.2f}  ({why})")

    print("\n=== alice signs an attestation and hands it to bob ===")
    attestation = alice.sign_attestation(peer_x_pubkey, passes=8, fails=0, avg_latency_ms=0.18, k=8)
    print(f"  signed payload: {json.dumps(attestation['payload'])}")
    print(f"  signature: {attestation['signature'][:24]}...")

    print("\n=== bob (never talked to peer_x) receives it, verifies, has SOME trust in alice ===")
    bob_store = ReputationStore('/tmp/poc_rep_bob.json')
    ok, reason = bob_store.add_attestation(attestation)
    print(f"  signature check: {ok}  ({reason})")
    bob_trust_in_signers = {alice.pubkey_hex(): 0.8}  # bob trusts alice's vouches at 80%
    score, why = bob_store.trust_score(peer_x_pubkey, bob_trust_in_signers)
    print(f"  bob's bootstrapped trust score for peer_x: {score:.2f}  ({why})")
    print("  -> bob has zero direct history with peer_x, but isn't starting from zero either.")

    print("\n=== bob receives the SAME attestation from a signer he doesn't trust at all ===")
    mallory = Identity('mallory')
    fake_vouch = mallory.sign_attestation(peer_x_pubkey, passes=8, fails=0, avg_latency_ms=0.18, k=8)
    bob_store.add_attestation(fake_vouch)
    score, why = bob_store.trust_score(peer_x_pubkey, bob_trust_in_signers)  # mallory absent from bob's trust map
    print(f"  bob's trust score, mallory's vouch included but ignored (0 weight): {score:.2f}  ({why})")
    print("  -> an untrusted signer's signature is still cryptographically valid, it just doesn't move the score.")

    print("\n=== tamper test: mutate the payload after signing ===")
    tampered = json.loads(json.dumps(attestation))  # deep copy
    tampered['payload']['passes'] = 800  # attacker tries to inflate the record
    ok, reason = verify_attestation(tampered)
    print(f"  tampered attestation (passes: 8 -> 800): valid={ok}  ({reason})")

    print("\n=== staleness test: same attestation, backdated ===")
    old_attestation = alice.sign_attestation(
        peer_x_pubkey, passes=8, fails=0, avg_latency_ms=0.18, k=8,
        ts=time.time() - 90 * 86400)  # 90 days old
    fresh_weight = attestation_weight(attestation)
    old_weight = attestation_weight(old_attestation)
    print(f"  fresh attestation weight: {fresh_weight:.3f}")
    print(f"  90-day-old attestation weight: {old_weight:.3f}  "
          f"(half-life {ATTESTATION_HALF_LIFE_DAYS:.0f}d, so ~3 half-lives in)")

    print("\n=== revocation: alice later learns peer_x went bad, revokes her own vouch ===")
    target_id = attestation_id(attestation)
    revocation = alice.sign_revocation(target_id, reason='peer_x failed possession challenges after this vouch')
    ok, reason = bob_store.add_revocation(revocation)
    print(f"  bob receives alice's revocation: accepted={ok}  ({reason})")
    score, why = bob_store.trust_score(peer_x_pubkey, bob_trust_in_signers)
    print(f"  bob's trust score for peer_x now: {score:.2f}  ({why})")
    print("  -> dropped without bob ever re-verifying peer_x himself — the original attestation")
    print("     is still on record (attestation_id present in the JSON below), just excluded.")

    print("\n=== attack test: mallory tries to forge a revocation of ALICE's vouch ===")
    forged = mallory.sign_revocation(target_id, reason='pretending to be alice')
    ok, reason = bob_store.add_revocation(forged)
    print(f"  bob receives mallory's forged revocation: accepted={ok}  ({reason})")
    print("  -> valid signature (it really is signed by mallory), but signer != original signer, rejected.")

    print("\n=== integrity test: revocation referencing an attestation nobody's seen ===")
    bogus = alice.sign_revocation('0' * 64, reason='nonexistent target')
    ok, reason = bob_store.add_revocation(bogus)
    print(f"  bob receives revocation for an unknown attestation id: accepted={ok}  ({reason})")

    alice_store.save()
    bob_store.save()
    print(f"\npersisted to {alice_store.path} / {bob_store.path} — "
          f"a real client would keep these across restarts, not just this run")


if __name__ == '__main__':
    main()
