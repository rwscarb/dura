# Censorship-resistant video platform — PoC notes

Brainstormed in #all-pdx 2026-08-22: a YouTube replacement indexed on Bitcoin,
distributed over BitTorrent-style magnet links. This tracks what got built and
what was actually learned, not just the idea.

## The core design problem

Storage and delivery over BitTorrent-plus-a-chain-timestamp is the easy part —
solved plumbing. The two things that actually kill projects like this:

- **Incentives.** LBRY minted a token and got sued as an unregistered security.
  BitTube minted a token and died to the standard watch-to-earn Ponzi spiral.
  PeerTube minted nothing and stayed permanently niche — no incentive layer at
  all, purely volunteer-hosted. Anchoring to *existing* BTC (no new token) and
  paying for service directly over Lightning avoids both failure modes at once.
- **Discovery.** A single global index or "most popular" list is exactly as
  seizable as YouTube's own trending page. The answer that doesn't reintroduce
  a chokepoint: no canonical list at all — gossiped signals (payments, in this
  case), any number of independent, replaceable indexer apps computing their
  own view, Nostr-style. Not built yet, just designed.

## What's built

### `poc_challenge_auction.py` — possession-gated reverse auction

`merkle_root`/`merkle_proof`/`verify_proof` are vendored byte-for-byte from
[rwscarb/btcvm](https://github.com/rwscarb/btcvm)'s `ott.py` — the same
functions `ott verify-chunk` runs locally, copied in rather than imported so
this repo has no dependency outside itself. In-process simulation, five
peers, one 8-chunk file.

**Part 1 — chunk-index challenge + auction.** A naive price-only auction picks
the cheapest bidder regardless of whether they can deliver — and does, in the
run captured here (a peer holding zero chunks wins on price alone). Gating bid
eligibility on passing a random chunk-index + Merkle-proof challenge fixes it:
a peer with nothing gets caught every round; a peer that tampers its response
~70% of the time gets caught most rounds and wins once by pure luck — real
evidence that a single spot-check isn't airtight, only a statistical one is.
A legitimately partial holder (only half the chunks) correctly sits out
rounds for chunks it doesn't have, without being flagged dishonest.

**Part 2 — nonce-salted challenge + timing bound.** Knowing a file's public
SHA256 gives an attacker nothing — SHA256 preimage resistance means you can't
derive `hash(chunk||nonce)` from `hash(chunk)` alone, so a peer with only the
published hash can't even attempt a response. A peer that *does* have the
real bytes but fetches them from someone else in real time (relaying) answers
with a cryptographically **correct** hash every time — the nonce alone can't
catch that. Only an added timing bound can, because the relay hop costs real
latency a local holder never pays.

### `poc_network_challenge.py` — the same mechanism over real sockets

Takes the timing-bound claim off paper: real TCP, real OS subprocesses, real
`time.perf_counter()`, loopback (127.0.0.1). Three modes:

- `holder <port>` — actually stores chunks, answers directly
- `relay <port> <holder_host> <holder_port>` — stores nothing, chains a
  second real TCP connection to the holder to fetch real bytes before
  answering
- `verify-remote <h_host> <h_port> <r_host> <r_port> [b_host] [b_port]` —
  client mode: connects to already-running holder/relay (e.g. other
  containers) and runs the separation analysis against them, with an
  optional second honest holder as a same-vs-same jitter baseline
- (no args) — local convenience: single-shot challenge rounds on loopback, narrated
- `stats` — local convenience: spawns holder+relay as subprocesses on
  loopback, bulk collection (80 real samples per role) + bootstrap analysis
  of how many repeated challenges it takes for the *session mean* to
  reliably separate holder from relay

**Real finding, not the expected one:** on loopback, single-shot timing does
*not* reliably separate a holder from a relay — the honest holder's worst
recorded round was slower than the relay's best. Averaging repeated
challenges does separate them (typically somewhere in the k=3–20 range across
several runs — see below); trust a session average, not any one round.

Separately: RunPod (meant to be the real-WAN test target) was down when this
was run, so real WAN latency was measured against public hosts instead
(1.1.1.1, 8.8.8.8, api.github.com: 5–30ms real TCP-connect RTT). Even the
fastest of those numbers dwarfs the loopback holder's worst case — meaning
real geographic distance make single-shot separation *easy*; the hard case
this PoC actually stress-tests is two peers that are genuinely close
together, which is exactly when a nearby relay is hardest to catch on timing
alone.

![session-size separation chart](poc_challenge_separation.png)

Chart from `viz_challenge_separation.py` — regenerates real measurements each
run rather than plotting a fixed snapshot. **The exact crossover k is not a
fixed constant** — it moved between k=3, k=8, and k=20 across different runs
of this same script on the same machine, purely from real system jitter. That
instability is itself the finding: don't hardcode a specific k, measure it
live and adapt, and prefer statistical separation over any fixed threshold.

### `docker-compose.yml` — the same test over real container networking

Four services: `holder1` and `holder2` (two independent honest peers),
`relay` (holds nothing, relays from `holder1` over the compose network),
`verifier` (runs the same repeated-challenge analysis against all three,
using DNS service names instead of loopback).

```bash
docker compose up --build --abort-on-container-exit verifier
```

Real run, over podman's docker-compose shim, actual separate containers on
the compose bridge network:

```
holder: mean 0.246ms  min 0.188ms  max 0.953ms
relay:  mean 0.483ms  min 0.414ms  max 1.134ms
holder2 (2nd honest holder, same-vs-same jitter baseline): mean 0.236ms  min 0.209ms  max 0.311ms

session size k   worst honest mean   best cheater mean  separated?
             1             0.953ms             0.414ms  no
             2             0.626ms             0.414ms  no
             3             0.712ms             0.421ms  no
             5             0.541ms             0.432ms  no
             8             0.430ms             0.437ms  YES
            12             0.425ms             0.445ms  YES
            20             0.346ms             0.448ms  YES
            30             0.314ms             0.451ms  YES
```

Same shape as loopback (single-shot doesn't separate, k≈8 does), plus one
useful sanity check the loopback version can't give: holder2's baseline mean
(0.236ms) sits right next to holder1's (0.246ms) — two equally honest,
unrelated containers naturally land close together, while the relay
(0.483ms, roughly double) is a real structural gap, not just inter-container
noise.

### `poc_reputation.py` — persistent local reputation + signed portable attestations

Two mechanisms, both real (Ed25519 via the `cryptography` package, actual
signing and verification, not simulated):

1. **Local reputation store** (`ReputationStore`, persisted to JSON) — a
   client's own record of direct experience with a peer (passes/fails/avg
   latency), so a known-good peer doesn't need to re-earn trust from zero on
   every interaction.
2. **Signed attestations** — a client signs its own verification outcome for
   a peer and hands the signed blob to another client, who didn't do the
   verification but can check the signature and decide how much to trust it.
   Same shape as PGP's Web of Trust, applied to possession-verification
   outcomes instead of key identity — including PGP's actual historical
   weak point: the crypto is the easy part, "how much do I trust this
   signer" is the unsolved UX problem, not a technical one.

Demonstrated for real in one run: a fresh client with zero direct history
bootstraps a trust score for an unknown peer purely from another client's
signed vouch; a vouch from a signer you don't trust at all is cryptographically
valid but contributes zero weight; mutating a signed payload after the fact
(`passes: 8 → 800`) is caught by signature verification; a 90-day-old
attestation is worth 0.125x a fresh one under a 30-day trust half-life.

## Running it

```bash
python3 poc_challenge_auction.py          # in-process, Parts 1 + 2, narrated
python3 poc_network_challenge.py          # real sockets, single-shot rounds, loopback
python3 poc_network_challenge.py stats    # real sockets, repeated-challenge separation, loopback
python3 poc_reputation.py                 # real Ed25519 signing/verification demo
python3 viz_challenge_separation.py       # regenerates the chart above from fresh data
docker compose up --build --abort-on-container-exit verifier   # same test, real containers
```

Self-contained — no dependency outside this repo. Needs `cryptography` and
`matplotlib` (`pip install cryptography matplotlib`) for the reputation demo
and the chart; `poc_network_challenge.py` and `poc_challenge_auction.py` are
pure stdlib. Docker/Compose needed only for the container-network test.

## Next steps

1. ~~Nonce-salted challenge + timing bound~~ — done, `poc_challenge_auction.py` Part 2
2. ~~Real network round-trip instead of in-process~~ — done, `poc_network_challenge.py`
3. ~~Local reputation + signed portable attestations~~ — done, `poc_reputation.py`
4. **Real WAN calibration against an actual second machine.** The public-host
   substitute measurement is suggestive, not conclusive — needs the actual
   protocol run against a real remote holder once RunPod (or any reachable
   second box) is up.
5. **Real testnet Lightning HTLC settlement**, replacing the mock
   "settlement" print statement in the auction.
6. **Point the mechanism at a real `.ott` archive** instead of `os.urandom`
   fake chunks — confirm Merkle proof size stays cheap at real video scale
   (thousands of chunks, not 8).
7. **Attestation revocation** — signed vouches currently only decay by age;
   no way to explicitly revoke one for a peer that went bad after being
   vouched for.
8. **Discovery layer** — the actual unsolved, and probably hardest, piece.
   Designed in conversation (gossiped payment attestations, no canonical
   index, Nostr-style pluralized indexers) but nothing built yet.
