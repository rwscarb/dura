#!/usr/bin/env python3
"""
Real Lightning HTLC settlement, replacing poc_challenge_auction.py's mock
"settlement: mock Lightning HTLC held until delivery confirms" print.

Talks to two real LND nodes (lnd-alice, lnd-bob — see lightning/docker-compose.yml)
over a real channel on regtest via `docker exec ... lncli`. Not simulated:
real BOLT11 invoices, real onion-routed HTLCs, real preimage reveal on
settlement — same protocol code LND runs on mainnet, just against a private
regtest chain instead of waiting on public testnet sync/faucets (same
reasoning as using a real remote box over SSH for the WAN latency test
rather than a fabricated one).

Requires the compose stack in lightning/ to be up with a funded, active
channel between alice and bob (see lightning/README.md for the one-time
setup: fund alice on-chain, open channel, confirm).
"""
import hashlib
import json
import subprocess

ALICE_CONTAINER = 'lightning-lnd-alice-1'  # payer — stands in for the requester/viewer
BOB_CONTAINER = 'lightning-lnd-bob-1'      # payee — stands in for the auction winner
LNDDIR = '/home/lnd/.lnd'


class SettlementError(RuntimeError):
    pass


def _lncli(container, *args):
    cmd = ['docker', 'exec', container, 'lncli', '--network=regtest', f'--lnddir={LNDDIR}', *args]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise SettlementError(f'lncli {args[0]} failed: {result.stderr.strip()}')
    return json.loads(result.stdout)


def channel_active():
    try:
        chans = _lncli(ALICE_CONTAINER, 'listchannels')
    except (SettlementError, FileNotFoundError):
        return False
    return any(c.get('active') for c in chans.get('channels', []))


def settle(amount_sat, memo):
    """Real HTLC settlement for one auction round's winning bid.

    Bob (payee/winner) creates a real BOLT11 invoice; Alice (payer/
    requester) pays it over the real channel. Returns a dict with the real
    payment_hash, the real revealed preimage, and independently re-verifies
    sha256(preimage) == payment_hash locally rather than trusting LND's own
    claim of success — same "verify, don't just trust the tool's own report"
    standard the rest of this project has used throughout.
    """
    amount_sat = max(1, int(round(amount_sat)))
    invoice = _lncli(BOB_CONTAINER, 'addinvoice', f'--amt={amount_sat}', f'--memo={memo}')
    payment_request = invoice['payment_request']
    expected_hash = invoice['r_hash']

    payment = _lncli(ALICE_CONTAINER, 'payinvoice', '--force', '--json', payment_request)
    if payment.get('status') != 'SUCCEEDED':
        raise SettlementError(f'payment did not succeed: {payment.get("status")}')

    preimage = payment['payment_preimage']
    recomputed_hash = hashlib.sha256(bytes.fromhex(preimage)).hexdigest()
    if recomputed_hash != expected_hash:
        raise SettlementError(
            f'preimage does not match invoice payment_hash — '
            f'got {recomputed_hash}, expected {expected_hash}')

    return {
        'amount_sat': amount_sat,
        'payment_hash': expected_hash,
        'preimage': preimage,
        'fee_sat': int(payment.get('fee_sat', 0)),
        'verified_locally': True,
    }


if __name__ == '__main__':
    # smoke test — run directly to confirm the channel is up before wiring
    # it into the auction
    if not channel_active():
        print('no active channel found — bring up lightning/docker-compose.yml first')
        raise SystemExit(1)
    result = settle(1234, 'lightning_settle.py smoke test')
    print(json.dumps(result, indent=2))
    print('\npreimage independently re-hashed and matched the invoice payment_hash — real HTLC, verified.')
