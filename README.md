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

`merkle_root`/`merkle_proof`/`verify_proof` are imported from the real,
[published](https://pypi.org/project/btcvm/) `btcvm` package (`pip install
btcvm`) — same functions `ott verify-chunk` runs locally. Used to be
vendored copies (kept this repo dependency-free before btcvm was on PyPI);
now that it's real and installable, this repo depends on it properly
instead. `ott` is the storage/archive layer — what you have and can prove
you have; this repo is the distribution/incentive layer built on top of
it. Deliberately kept as separate packages rather than merged: `ott` stays
the stable, already-published tool with real users, this stays free to be
a rougher-edged PoC without dragging Lightning/Docker into `ott`'s
dependency surface.

In-process simulation, five peers, one 8-chunk file.

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

RunPod was down the first time this was tried, so real WAN latency got
measured against public hosts as a substitute (1.1.1.1, 8.8.8.8,
api.github.com: 5-30ms real TCP-connect RTT) — suggestive, not conclusive.
**Update: ran it for real once a RunPod box came back up**, tunneled over a
real SSH connection (`ssh -L`, since the pod only exposes its SSH port, not
arbitrary TCP) — local honest holder vs. a local relay that secretly fetches
from that real remote box for every challenge:

```
holder: mean 0.254ms  min 0.186ms  max 1.250ms
relay:  mean 432.648ms  min 395.583ms  max 638.218ms

session size k   worst honest mean   best cheater mean  separated?
             1             1.250ms           395.583ms  YES
```

~1700x gap, separates cleanly at k=1 — single-shot is all you need once real
geographic distance is involved. Confirms the substitute-host hypothesis:
real WAN distance makes this *easy*; the hard case this PoC actually
stress-tests is two peers that are genuinely close together, which is
exactly when a nearby relay is hardest to catch on timing
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
3. **Revocation** — a signer can kill their own earlier vouch (`sign_revocation`,
   keyed to the attestation's content-hash `attestation_id`). Only accepted
   if the revocation's signer matches the original attestation's signer;
   the revoked attestation stays on record rather than being deleted, so
   "X vouched for Y, then revoked it" stays an honest, auditable fact
   instead of quietly disappearing.

Demonstrated for real in one run: a fresh client with zero direct history
bootstraps a trust score for an unknown peer purely from another client's
signed vouch; a vouch from a signer you don't trust at all is cryptographically
valid but contributes zero weight; mutating a signed payload after the fact
(`passes: 8 → 800`) is caught by signature verification; a 90-day-old
attestation is worth 0.125x a fresh one under a 30-day trust half-life;
alice revoking her own vouch drops bob's trust score for that peer without
bob ever re-verifying it himself; mallory forging a revocation of *alice's*
vouch (valid signature, wrong signer) is correctly rejected; a revocation
referencing an attestation nobody's ever seen is rejected too.

### `lightning_settle.py` + `lightning/` — real Lightning HTLC settlement

Replaces `poc_challenge_auction.py`'s mock "settlement" print with a real
one: two real LND nodes (Lightning Labs' production node software) on
regtest, real bitcoind backing them, a real funded channel between them.
`poc_challenge_auction.py --lightning` settles every auction round's winner
with a genuine BOLT11 invoice + HTLC — not simulated, and not just trusting
LND's own "SUCCEEDED" status: `lightning_settle.py` independently re-hashes
the revealed preimage and checks it against the invoice's payment_hash
locally before calling it settled.

Real run, 5 winning rounds, real preimages each verified against their own
payment hash:

```
WINNER: bob      9 sat   preimage 4ac71143706b...  payment_hash d1be1130c553...
WINNER: bob      5 sat   preimage eea88d802d1a...  payment_hash 8acabea4ddf7...
WINNER: bob      6 sat   preimage 76f1ade0f9be...  payment_hash f9c9bda28be0...
WINNER: bob     10 sat   preimage 96e696c1cde9...  payment_hash f8eeb66d1878...
WINNER: mallory  1 sat   preimage 508385841cb3...  payment_hash 1c2872b6018f...
```

Bob's cumulative channel balance after the run matched the sum of every
settled payment exactly, checked directly against LND rather than assumed.
Full setup steps in `lightning/README.md` — real bitcoind + LND takes a
one-time channel-funding setup regtest can't skip (mine to coinbase
maturity, open a channel, mine confirmations) before it's usable.

### `poc_real_archive_challenge.py` — real `.ott` archive, real video, real scale

Every other PoC file here used `os.urandom` fake chunks (8 of them).
This one points the same mechanism at a real 217MB video, archived with the
real `ott` CLI at a real 64KB chunk size:

```
real archive: real_video.mp4, 217,831,234 bytes, 3324 real chunks x 65536 bytes
recomputed Merkle root matches ott's own commit: True
```

The thing this was actually checking — proof size at real scale:

```
chunk     0: 12 steps, 396B raw, 1176B as JSON
chunk  1662: 12 steps, 396B raw, 1168B as JSON
chunk  3323: 12 steps, 396B raw, 1167B as JSON
```

12 proof steps at 3324 real chunks vs. 3 steps at the toy 8-chunk scale —
exactly log2(N), not linear, confirmed with real numbers instead of just
trusting the math. Even a 2-hour movie at these settings (~10GB, ~163,840
64KB chunks) would only need ~17 steps, still under 1KB. Then ran the same
nonce-salted-challenge logic from `poc_challenge_auction.py` Part 2 against
real bytes read straight off disk at real offsets — all 5 real rounds
checked out: hash matches ott's own committed leaf, Merkle proof verifies,
nonce response is internally consistent.

`real_archive/real_video.mp4` isn't committed to this repo (208MB, and it's
not this repo's to redistribute) — `real_archive/.ott/`'s metadata is
tracked, so the chunk list and commitment are there for inspection even
without the video itself. Reproduce with any file:

```bash
cd real_archive
python3 /path/to/btcvm/ott.py init
# edit .ott/config, set "chunk_size" to whatever you want (65536 used here)
python3 /path/to/btcvm/ott.py add your_video.mp4
python3 /path/to/btcvm/ott.py commit
cd ..
python3 poc_real_archive_challenge.py
```

### `discovery_relay.py` + `poc_discovery.py` — discovery, no canonical index

The last unsolved piece from the original brainstorm, actually built: no
single "trending" list, no server whose seizure kills discoverability.
Three independent relay processes (`discovery_relay.py` — real stdlib
`http.server`, no deps), each deliberately dumb: verifies a posted event's
signature (a relay won't store garbage) but has zero opinion on content
quality, zero ranking logic. A creator (carol) publishes a real event
pointing at the real video's real Merkle root from item 6. Viewers like it
and subscribe to each other, spread across the three relays — nobody posts
to all three, on purpose.

Two clients, `bob` (subscribes to dan + erin) and `mallory` (subscribes to
frank only), each query all three relays, verify every event's signature
themselves (never trust a relay's word for it), and compute their own
ranking from their own subscribe graph — subscriptions *are* the trust
graph, not a separate feature, same insight from the Slack thread now
actually running as code:

```
same 27 gossiped events, both clients saw all 23 likes (3 honest + 20 sybil),
but scored the content differently — 2.0 (bob) vs 1.0 (mallory) — because
ranking runs on each client's own trust graph, not vote count.
```

A 20-identity sybil swarm likes the same content — every signature is
real and individually valid, a relay has no basis to reject any of them —
and moves neither client's score, because neither bob nor mallory
subscribes to any of the sybils. Sybil resistance from the trust graph,
not from relay-side moderation.

Then relay:9101 — the one carol's publish event and dan's like both
happened to live on — gets killed outright. Real result, not a clean win:
the content stays discoverable and rankable (erin's like survived on a
different relay), but the human-readable title and dan's like are gone for
good, since neither was posted anywhere else. Redundancy has to be
deliberate — post to more than one relay — it isn't automatic just because
relays are plural. Same limitation a real Nostr relay dying would have.

```bash
python3 poc_discovery.py
```

### `node.py` — the integration piece: host, discover, download, for real

Everything above is a demo of one mechanism at a time. `node.py` (via
`dura.py host/discover/download/like/subscribe/whoami`) is the actual
integration: a real node that hosts a real archived file over the real
wire protocol from `poc_network_challenge.py` (extended with `INFO` and
`LEAVES` so a downloader can learn the archive's shape first), announces
itself on a real relay, and — new, not just wired from existing pieces —
actually downloads a file from a peer and reassembles it on disk, which
nothing before this verified chunk-by-chunk *and* wrote a real file.

A persistent identity now lives at `~/.dura_identity.key` — every other
script tonight generated a fresh Ed25519 keypair per run, which is fine for
a demo but means nobody could ever accumulate reputation or be subscribed
to across invocations. A real node needs a stable pubkey.

Real end-to-end run: hosted the real 217MB video, discovered it from a
separate process, downloaded it to a new path, and diffed the result
against the original with `cmp` (not just checking the tool's own claim of
success) — byte-for-byte identical, matching SHA256 on both sides, 3324
chunks downloaded and verified in 1.3s.

Caught a real bug doing this, not a clean pass on the first try: `ott`
records a video's `sha256` manifest field as the **Merkle root** over its
chunk hashes (`digest = merkle_root(chunks)` in `ott.py`'s `cmd_add`), not
a linear whole-file hash — my first version streamed a plain
`hashlib.sha256()` over the received bytes and compared that, which does
not and structurally cannot equal a Merkle root. Every individual chunk
was verifying correctly the whole time; only the final whole-file check
was comparing the wrong thing. Fixed by recomputing the Merkle root over
the received leaves and checking it against the host's advertised
`sha256` — done *before* downloading any chunk, not after, so a host lying
about its own archive gets caught immediately instead of after wasting
bandwidth on it.

```bash
# terminal 1
python3 discovery_relay.py 9101

# terminal 2 — host the video from item 6
python3 dura.py host real_archive --port 9201 --relay http://127.0.0.1:9101

# terminal 3
python3 dura.py whoami
python3 dura.py discover --relay http://127.0.0.1:9101
python3 dura.py download <content_hash_prefix> --relay http://127.0.0.1:9101 --out downloaded.mp4
python3 dura.py like <content_hash> --relay http://127.0.0.1:9101
python3 dura.py subscribe <target_pubkey> --relay http://127.0.0.1:9101
```

`--advertise-host` on `host` matters if you're not on localhost — no NAT
traversal here, it just tells the relay what address to hand out, real
reachability is on you. Same point-to-point-known-address limitation
named earlier in this README, now visible as an actual CLI flag instead of
just a caveat in prose.

### `download` now runs the actual stack, not just a direct fetch

Until this point, `download` trusted whichever host `discover` found
first, for free, with no possession check and no reputation. That was the
real gap flagged after the last round of shell bug-fixes: the auction
(`poc_challenge_auction.py`), the reputation/trust-graph layer
(`poc_reputation.py`), and Lightning settlement (`lightning_settle.py`)
were all built and validated standalone, but none of them were reachable
from a real download. Now they are:

1. **Resolve every host** claiming to have the content (`discover` already
   deduped by event, not by content — `download_with_auction` groups by
   content_hash so a second host publishing the same file actually gets
   considered, not silently dropped).
2. **Possession-challenge each one** — sample-FETCH `k` random chunks
   (default 3) and verify against the already Merkle-root-checked LEAVES.
   Scoped deliberately to poc_challenge_auction.py's **Part 1** mechanism
   (chunk-index challenge), not Part 2's nonce/timing relay-detection —
   that one needs ground-truth bytes the verifier already trusts, which a
   first-time downloader doesn't have until *after* this same sampling
   step. Noted here rather than silently narrowed.
3. **Auction survivors** by local reputation first, then price — same
   "challenge gates the auction" shape as the original PoC's naive-vs-
   gated comparison. A host with no history starts at 0.0, same as
   everyone; reputation only pulls ahead of price once there's real
   experience behind it (verified — see below).
4. **Pay the winner** over a real Lightning HTLC if `--lightning` is given
   and the price is nonzero — same `lightning_settle.py` regtest demo path
   as before. Honest limitation, not glossed over: this settles with the
   fixed alice/bob demo nodes, not a general "pay this specific host's own
   Lightning node" protocol — that would need hosts to serve their own
   real BOLT11 invoices, which isn't built.
5. **Download and record** — same chunk-verified `download()` as before,
   then the outcome (pass/fail, latency) gets written to
   `~/.dura_reputation.json` via `ReputationStore.record_direct`, so the
   next auction for this host starts from real history instead of 0.0.

Real test, two independent hosts (separate identities, separate `HOME`s so
podman's rootless state didn't collide) serving the same real video —
one free, one priced at 500 sat:

```
found 2 candidate host(s) for 7f2477c7ea675004...
  + 127.0.0.1:9202: possession verified (3/3 chunks), price=0 sat, reputation=0.00, avg_latency=3.4ms
  + 127.0.0.1:9203: possession verified (3/3 chunks), price=500 sat, reputation=0.00, avg_latency=0.7ms
selected 127.0.0.1:9202 — price 0 sat, reputation 0.00, 3.4ms avg
```

Ties on reputation, cheapest wins — correct. Then manually seeded
contrasting reputations (hostA bad, hostB good) to prove reputation
actually *overrides* price rather than the selection just always
defaulting to cheapest:

```
+ 127.0.0.1:9202: ... price=0 sat, reputation=0.10 ...
+ 127.0.0.1:9203: ... price=500 sat, reputation=1.00 ...
selected 127.0.0.1:9203 — price 500 sat, reputation 1.00, 0.4ms avg
```

Picked the pricier, more trusted host. Then killed the free host outright
and re-ran with `--lightning`: real HTLC settled (500 sat, preimage
independently re-verified against the invoice's own `r_hash` via
`lncli listinvoices`, not just trusting the printed claim), download
proceeded, byte-identical against the source via `cmp`.

```bash
python3 dura.py download <content_hash> --relay http://127.0.0.1:9101 \
    --rounds 5 --lightning --out downloaded.mp4
```

`host --price N` sets what a host charges (sats, default free — `PRICE` is
a new wire-protocol verb, backward compatible: a host that doesn't
implement it just gets treated as free by an older/newer client either way).

### Transitive trust — real attestations flow through the subscribe graph

The gap named right after the auction landed: `select_host` was only ever
passed *your own* direct history, never other people's signed vouches, so
a host you'd genuinely never dealt with always scored a flat 0.0 no matter
who else had already verified it. `poc_reputation.py`'s attestation/
revocation machinery could support exactly this — it just was never fed
anything, since attestations were never gossiped through a relay the way
publish/like/subscribe already were.

Considered adopting real PGP for this (Ryan asked) — decided against it:
PGP's actual trust-level/path-counting *idea* (marginal vs full trust,
computed transitively) is worth borrowing, but the OpenPGP *format* is
built for signing emails, not cheaply gossiping dozens of small JSON
events, and its classic path to real interoperability — public keyservers
— reintroduces exactly the single-point-of-failure problem discovery.py's
relay design exists to avoid. Built the trust-level idea on the Ed25519
signing already in place instead of adopting the standard.

`build_trust_graph()`: real BFS outward from your own pubkey through real
signed `subscribe` events pulled from a relay (a subscribe *is* a trust
edge — same insight from the original discovery-layer design, now actually
computed transitively instead of 1-hop-only). Trust decays per hop
(default 0.5×) — a friend counts fully, a friend-of-a-friend counts less.
Takes the shortest path to each reachable pubkey, not the sum across every
path — summing would let a sybil ring inflate a target's trust just by
adding more low-value paths to it.

`download_with_auction` now also pulls real `attestation` events from
relays (new event type — no relay code changes needed, `discovery_relay.py`
already verifies and filters by type generically), verifies each one, and
feeds them into `select_host` alongside the trust graph. And after every
successful download it publishes its own outcome as a real signed
attestation, not just recording it locally — so the next person who trusts
*you*, even transitively, benefits without ever dealing with that host
first.

Real end-to-end proof, not just the math: identity A downloaded from a
host directly (0.00 reputation, no history, same as before this change),
which auto-published a real attestation. A second identity, ROOT, who had
**never talked to that host**, subscribed to A (one real signed edge), then
ran `download`:

```
trust graph: 1 pubkey(s) reachable within 3 hop(s) of your own subscribes
pulled 1/1 real attestation(s) from relays (others' vouches, weighted by your trust in whoever signed them)
  + 127.0.0.1:9204: possession verified (3/3 chunks), price=0 sat, reputation=1.00 (1 attestation(s), weighted by signer trust + age), avg_latency=0.4ms
```

Would've been a flat `reputation=0.00` before this change — ROOT had zero
direct history with the host. Instead it inherited A's real, already-
verified experience through one real hop of trust. Byte-identical download
confirmed via `cmp`, same as every other download in this repo.

Separately verified the decay math itself against a real 3-hop chain
(root→A→B→C) plus a disconnected stranger: `A=0.5, B=0.25, C=0.125`,
stranger absent from the graph entirely, `max_hops` correctly bounding how
far it searches — exact, not approximate.

### `tunnel_relay.py` + persistent sessions in `node.py` — NAT traversal

`host --advertise-host`'s own help text used to say it outright: "no NAT
traversal here." Real gap — almost nobody has a directly reachable
inbound port. Fixed with a relay-mediated tunnel rather than real
STUN/ICE hole-punching: works behind *any* NAT including CGNAT (both
sides only ever make outbound connections, so there's nothing for a
firewall to block), at the honest cost of relay bandwidth/latency and
someone having to run `tunnel_relay.py` somewhere reachable — same
operational shape as already running a discovery relay, not a new kind of
problem.

Rendezvous protocol (hand-rolled to match this repo's own line-based wire
protocol rather than pulling in an external tunnel tool like `bore`):
a NAT'd host opens one persistent outbound `REGISTER <token>` control
connection; a downloader connects and sends `CONNECT <token>`; the relay
asks the host to dial back (`NEWSTREAM <stream_id>` on the control
channel, `DATA <stream_id>` as the reply) and, once paired, does nothing
but shovel raw bytes between the two sockets — same "dumb relay, no
opinion on the payload" design `discovery_relay.py` already uses, just
for bytes instead of signed JSON events. `token` is the archive's
`content_hash`; the relay has no idea what it means, same as everywhere
else in this repo that keys off it.

Had to fix a real prerequisite bug first, not just add the relay: every
wire-protocol command (`INFO`, `LEAVES`, one `FETCH` per chunk — 3324 of
them for the real archive) used to open a brand-new TCP connection.
Cheap directly, fatal through a relay — every single chunk would pay a
full rendezvous round-trip before any bytes moved. Fixed by giving both
sides a persistent session (`HostConnection` client-side, `serve_session`
host-side, looping over many commands per connection instead of one) —
a net win for direct connections too, not just a tunnel workaround.

Second real bug, caught while verifying this against real sockets, not
just in review: Python's `socketserver.ThreadingMixIn` closes a
connection's socket the instant its handler function *returns* — a
handler that spawns a pipe thread and returns immediately gets its own
socket killed out from under that thread mid-transfer. Fixed by having
each handler thread block for the tunneled session's full lifetime
(`_Pairing`'s `ready`/`done` events in `tunnel_relay.py`) instead of
firing off detached threads and returning early. First attempt failed
with `json.decoder.JSONDecodeError: Expecting value` — an empty response,
not a corrupted one, which is exactly what a socket closed mid-read looks
like.

Real end-to-end proof: hosted the real 217MB/3324-chunk archive,
advertised at a deliberately unreachable address (`10.255.255.1`, not
localhost) so there was no possibility of a direct connection carrying
the download, `--tunnel 127.0.0.1:9199`. Downloaded entirely through the
tunnel, `cmp`-verified byte-identical against the source, 3324 chunks in
1.4s — comparable to a direct download, not a meaningfully slower path.

```bash
# terminal 1
python3 discovery_relay.py 9101
# terminal 2
python3 tunnel_relay.py 9199
# terminal 3 — no reachable --advertise-host at all
python3 dura.py host real_archive --port 9201 --tunnel 127.0.0.1:9199 \
    --relay http://127.0.0.1:9101 --advertise-host 10.255.255.1
# terminal 4
python3 dura.py download <content_hash> --relay http://127.0.0.1:9101 --out downloaded.mp4
```

Additive, backward-compatible protocol change, same shape as the
optional `PRICE` wire verb: `publish()` gained an optional `tunnel`
field (`'relay_host:relay_port'` or absent); a candidate without it is
just connected to directly, exactly as before this existed.

**TLS**, for tunnel relays that terminate it at the edge instead of
speaking it themselves (deployed one on Fly with `handlers = ["tls"]` —
`fly.tunnel-relay.toml` + `Dockerfile.tunnel-relay` in this repo): prefix
the address with `tls://` — `--tunnel tls://tunnel.example.com:9199`.
`tunnel_relay.py` itself never changes; edge termination decrypts before
the bytes ever reach it, so only the two ends that actually cross the
public internet (the host's `REGISTER`/`DATA` connections, a
downloader's `CONNECT`) wrap the socket in
`ssl.create_default_context()` — real CA validation by default, not a
weakened check. Verified against a real self-signed-cert TLS-terminating
proxy built specifically to test this (mimicking exactly what Fly's edge
does): confirmed the client correctly *rejects* an untrusted cert before
trusting it, then a full host→tunnel→download round-trip over the
encrypted path, byte-identical result.

**Heartbeat**, for the same idle-connection problem real deployments
actually hit: the `REGISTER` control connection sends nothing between
registering and the first real download, sometimes for a long time.
Fly's own edge (confirmed from real production logs, not a guess) resets
TCP connections idle more than a few minutes, which silently
unregistered the host with no error until the next download failed.
Fixed with a small periodic `PING` on the control connection —
`tunnel_relay.py` needed zero changes, its `REGISTER` loop already
discards anything it receives that isn't relevant. Proved it against a
real idle-enforcing test server (3-second idle limit): without the
heartbeat, killed at exactly 3.0s; with it, survived 8 full seconds with
no kill event at all.

### `dht.py` — real Kademlia DHT discovery, no relay required

Every other discovery path in this repo needs a relay URL, told to you
out of band — real, and a real limitation, until now: `dht.py` answers
"how do two nodes find each other with no shared server" using an actual
Kademlia DHT, via the real `kademlia` PyPI library rather than
reimplementing node-IDs/k-buckets/RPC routing from scratch. Scoped
honestly: this covers `announce(content_hash, host_addr)` /
`lookup(content_hash)` — not the richer signed-event system
(publish/like/subscribe/attestation/trust-graph) `discovery_relay.py`
already handles, since Kademlia's plain key→value store isn't a natural
fit for an append-only event log. That richer system staying on relays
is a deliberate scope boundary, not an oversight.

Multiple announcers of the same content are merged, not overwritten —
`kademlia`'s `set()` is single-value-per-key, so a naive announce would
silently drop everyone else's listing; `_announce()` fetches, merges,
re-announcing from the same host again correctly doesn't duplicate.

Proved with three separate, escalating real tests: three chained nodes
(C only ever bootstrapped through B, never spoke to A directly) — content
announced on A was found from C, real Kademlia routing, not a shared-
memory illusion. Two different hosts announcing the same content — both
preserved. And the strongest one: a node announced content, then that
node's *entire process exited* — a completely independent fourth process,
knowing only the original bootstrap node, still found what was announced.
Real value replication surviving the announcer going offline, which is
the actual point of a DHT over a relay.

```bash
python3 dht.py 8468                          # first node, new swarm
python3 dht.py 8469 127.0.0.1:8468           # second node, joins the first
```

Or from the shell: `dht start [port] [bootstrap_host:port]`,
`dht announce <content_hash> <host:port> [title]`, `dht lookup <content_hash>`.

### `web_ui.py` — local web UI

Hosting/discovering/downloading/liking/subscribing all required
memorizing `dura.py`'s CLI flags — real friction for anyone who isn't
already comfortable with argparse. `web_ui.py` is a small stdlib
`http.server`/`ThreadingHTTPServer` JSON API — same tool
`discovery_relay.py` already uses, no new dependency, still just the
three packages in `requirements.txt` — wrapping the exact same
`node.py` functions the CLI calls, no reimplemented protocol logic. The
frontend (`web/`) is a single static page, vanilla JS, no build step —
matches the rest of this repo's no-toolchain style.

Binds `127.0.0.1` by default on purpose: a local control surface, not
something meant to face the internet, and there's no auth built — same
"reachability is on you" honesty `--advertise-host`'s docs already apply
elsewhere. `--bind` widens it at your own risk.

Endpoints, all thin wrappers: `GET /api/whoami`, `GET /api/discover`,
`POST /api/host` (backgrounds `run_host_server`/`run_host_tunnel` the
same way `shell.py`'s `do_host` already does) + `GET /api/hosts`,
`POST /api/download` + `GET /api/download/<job_id>` for polling progress
(`download()` gained an optional `on_progress(idx, n_chunks)` callback,
default no-op, so CLI output is unaffected), `POST /api/like`,
`POST /api/subscribe`, `GET /api/reputation/<pubkey>`,
`GET /api/stream/<job_id>`, `GET /api/qr?data=...`, `GET /api/lan-url`.

Real end-to-end proof, not just the API responding: hosted a file
through the **Host** form, confirmed a second terminal's `dura discover`
actually saw it (proves the API called the real `node.publish`, not a
mock); downloaded through the **Downloads** form with a live-polling
progress bar, `cmp`-verified byte-identical; liked and subscribed
through the UI, confirmed the real signed events landed on the relay by
querying it directly.

**Streaming** (`/api/stream/<job_id>`) — the actual gap behind "play
media on my phone": the UI could already trigger a download, but the
bytes only ever landed on this server's disk, never reached the browser.
Real HTTP range support (`Accept-Ranges`, `206 Partial Content`, `416`
for out-of-range), so a `<video>` tag can seek instead of downloading
blind. Verified with real range requests against a real completed
download — full fetch, a mid-file range, and an open-ended range, all
byte-exact against the source; error paths (out-of-range, unknown job)
checked too.

**QR codes** (`/api/qr`, terminal QR on startup) — same `qrcode` package
`ott`'s own `ott qr` already uses. `--bind 0.0.0.0` auto-detects the real
LAN IP (a UDP-route trick, no packet actually sent) instead of printing
the useless literal `0.0.0.0`, so the printed/scanned URL is one a phone
can actually reach — and it's computed server-side and exposed via
`/api/lan-url` specifically because the browser's own
`location.origin` lies the instant you load the page via `localhost`
instead of the LAN address; a real bug this caused (the header's "open
on phone" QR pointing at `127.0.0.1`) was reproduced and fixed by having
the client fetch the server's own answer instead of trusting
`location.origin`.

```bash
python3 dura.py serve                # alias for `web`, positional args: serve [bind] [port]
python3 dura.py serve 0.0.0.0 8080    # reachable from your phone; prints a scan-to-open QR
# or, from the shell: `serve [bind] [port]`
```

### `shell.py` — interactive, tab-completing, same pattern as `ott`'s shell

`python3 dura.py` with no arguments (or `dura.py shell`) drops into an
interactive shell — same `cmd.Cmd` + readline pattern as `ott`'s own shell,
same conventions: short aliases (`w`/`h`/`r`/`disc`/`dl`/`l`/`sub`), `help`
or `?` for commands, `Ctrl-D` or `q` to exit, tab completes.

`relay` runs a real discovery relay in the background too, same pattern as
`host` — the whole flow (relay, host, discover, download, like, subscribe)
runs from one shell session, no second terminal required. Discovered
running it that way for real (`host` with no relay running produces
"unreachable, skipped, nothing found," `discover` alone can't conjure a
relay that isn't there — real friction that surfaced from actually using
it, not a hypothetical). `relay` also sets itself as the session's default
relay, so `discover`/`download`/`like`/`subscribe` don't need `--relay`
repeated every time. `host` also takes `--tunnel [tls://]RELAY_HOST:PORT`
now, same as the CLI — see `tunnel_relay.py` above. `serve [bind] [port]`
runs the web UI in the background without a second terminal — see
`web_ui.py` above. `dht start/announce/lookup` runs a real Kademlia node
in the background — see `dht.py` above. Every background command
(`host`, `relay`, `serve`, `dht start`) goes through a shared `_bg()`
wrapper now: a real failure inside one of those threads used to dump a
raw Python traceback into the middle of the prompt (an uncaught
exception in a background thread was never covered by `onecmd()`'s own
try/except, which only wraps the synchronous part of a command) — caught
live from a real `--tunnel ~/share` typo, fixed, reproduced the same
crash again afterward to confirm it now prints a clean `✗ ...` line
instead and the shell stays fully usable.

Completion resolves against real state, not a fixed list — same idea as
`ott`'s completions (which complete against the real archive). `download`
and `like` tab-complete against content hashes actually seen in the last
`discover`; `subscribe` completes against pubkeys actually seen:

```
dura> relay
  relay running on port 9101 in the background — set as your default relay
dura> host real_archive --relay http://127.0.0.1:9101
  hosting real_video.mp4 on port 9201 in the background — shell still usable
dura> discover
  'real_video.mp4'   hash=7f2477c7ea675004...  host=127.0.0.1:9201  by=409a15dcfc59...
dura> download 7f24<TAB>
7f2477c7ea675004ad5dbab6dc7c44327c724b880cc389807df1965b77966acc
dura> download 7f2477c7ea675004ad5dbab6dc7c44327c724b880cc389807df1965b77966acc
3324 chunks downloaded and verified in 1.4s
```

`host` runs the server in a background thread instead of blocking the
shell — genuinely new, not copied from `ott`, since nothing in `ott`
blocks forever the way a hosting server does. Ran this exact sequence for
real (scripted, not just described): host → discover → download →
`cmp`-verified byte-identical → `like`, all in one shell session, download
still finishing in 1.4s with the server running in the background thread
the whole time.

## Running it

`./dura.py --help` (or `dura.py lightning --help` for the nested ones) is
the friendliest entry point — it's a thin argparse wrapper over the
Makefile, same targets, real subcommands and `--help` text instead of
needing to remember `make` target names. `make help` lists the same
targets directly. Or run any command below on its own:

```bash
python3 poc_challenge_auction.py          # in-process, Parts 1 + 2, narrated
python3 poc_network_challenge.py          # real sockets, single-shot rounds, loopback
python3 poc_network_challenge.py stats    # real sockets, repeated-challenge separation, loopback
python3 poc_reputation.py                 # real Ed25519 signing/verification demo
python3 viz_challenge_separation.py       # regenerates the chart above from fresh data
docker compose up --build --abort-on-container-exit verifier   # same test, real containers
cd lightning && docker compose up -d && cd ..   # real bitcoind + 2 LND nodes (see lightning/README.md for setup)
python3 poc_challenge_auction.py --lightning    # same auction, real HTLC settlement
python3 poc_real_archive_challenge.py           # same challenge mechanism, real 3324-chunk video
python3 poc_discovery.py                        # 3 real relays, personalized ranking, sybil test
python3 tunnel_relay.py 9199                    # NAT-traversal relay — see host --tunnel below
python3 dura.py serve --bind 0.0.0.0 --port 8080  # local web UI, reachable from your phone
python3 dht.py 8468                             # real Kademlia DHT node — see dht.py above
```

`pip install -r requirements.txt` gets everything (`btcvm`, `cryptography`,
`matplotlib`, `kademlia`). Broken down: `poc_challenge_auction.py` (and
`poc_real_archive_challenge.py`, which imports from it) needs `btcvm`;
`poc_reputation.py` and `poc_discovery.py` (which imports from it) need
`cryptography`; `viz_challenge_separation.py` needs `matplotlib`;
`dht.py` needs `kademlia`. `poc_network_challenge.py` and
`discovery_relay.py` are pure stdlib, no install needed. `qrcode` is
optional (`pip install qrcode`) — `web_ui.py`'s QR endpoints degrade to a
plain URL, printed instead of rendered, if it's missing. Docker/Compose
needed for the container-network test and for `--lightning` (real
bitcoind + LND, see `lightning/README.md`).

Or, packaged: `pip install -e .` (see `pyproject.toml`) installs a real
`dura` command on your `PATH` instead of `python3 dura.py`.

## Next steps

1. ~~Nonce-salted challenge + timing bound~~ — done, `poc_challenge_auction.py` Part 2
2. ~~Real network round-trip instead of in-process~~ — done, `poc_network_challenge.py`
3. ~~Local reputation + signed portable attestations~~ — done, `poc_reputation.py`
4. ~~Real WAN calibration against an actual second machine~~ — done, real
   RunPod box over an SSH tunnel: ~1700x gap, separates at k=1
5. ~~Real Lightning HTLC settlement~~ — done, `lightning_settle.py` +
   `lightning/` (real bitcoind + 2 LND nodes, real BOLT11 invoices, real
   preimage reveal independently re-verified). Regtest, not public testnet —
   same reasoning as #4: real protocol code, skip the wait on chain
   sync/faucets.
6. ~~Point the mechanism at a real `.ott` archive~~ — done,
   `poc_real_archive_challenge.py`: real 217MB video, 3324 real chunks,
   12-step proofs (~400B), confirmed O(log N) not linear.
7. ~~Attestation revocation~~ — done, `poc_reputation.py`: signer-only
   revocation keyed to `attestation_id`, forged revocation from a different
   signer correctly rejected, revoked attestation kept on record not deleted.
8. ~~Discovery layer~~ — done, `discovery_relay.py` + `poc_discovery.py`:
   3 independent dumb relays, personalized client-side ranking from each
   client's own subscribe graph, sybil-resistant (20 fake identities move
   neither client's score), real relay-death test (content survives,
   anything posted only to the dead relay doesn't — redundancy isn't free).
9. ~~NAT traversal~~ — done, `tunnel_relay.py`: relay-mediated rendezvous
   (not real STUN/ICE hole-punching), real 217MB/3324-chunk archive
   downloaded end-to-end through the tunnel with the host advertised at
   an unreachable address, byte-identical. Required a persistent-session
   refactor of `node.py`'s wire protocol first — see above.
10. ~~Local UI~~ — done, `web_ui.py` + `web/`: stdlib JSON API wrapping
    the same `node.py` functions the CLI calls, static vanilla-JS
    frontend, no new dependency. Host/discover/download/like/subscribe
    all verified working from the browser, not just the API responding.
11. ~~Real P2P/DHT discovery~~ — done, `dht.py`: real Kademlia via the
    `kademlia` library, not reimplemented. Content survived the
    announcing node's process exiting entirely, found by a fourth,
    independent process that only knew the original bootstrap node — real
    value replication, not a two-party memory trick. Scoped honestly:
    covers host-discovery only, not the richer event system.
12. ~~TLS for tunnel relays~~ — done, `tls://` prefix on `--tunnel`,
    verified against a real self-signed-cert TLS-terminating proxy built
    specifically to test it, full host→tunnel→download round-trip over
    the encrypted path.
13. ~~Play media from the web UI~~ — done, `/api/stream` with real HTTP
    range support, verified byte-exact against full/mid-file/open-ended
    range requests.
14. ~~Multi-file hosting~~ — done, `host <dir>` (no `--file`) serves every
    archived file over one port, downloader `SELECT`s by content hash.
    Fixes the original bug: a 45-file archive silently collapsed to
    whichever file happened to be last in the manifest.

Every item on the original roadmap is now built and verified against real
output, not just designed — and `node.py` (below) wires host/discover/
download/like/subscribe into one real tool instead of six disconnected
demos. What's left is scaling and hardening this, not proving the
mechanisms work — see each section above for the honest edges that are
still real constraints even though the core ideas held up: loopback
timing separation isn't airtight without averaging, relay death loses
non-redundant data, RunPod flakiness, regtest-only Lightning, the DHT
covers host-discovery but not the richer signed-event system, the tunnel
relay (even with TLS) is still a single point of failure/bandwidth cost
with no redundancy story the way discovery relays have, and the web UI
has no auth, local-only by design.
