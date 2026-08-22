# Real Lightning HTLC settlement

Real bitcoind + 2 real LND nodes (Lightning Labs' production node software)
on regtest — not simulated, not testnet-with-a-mock. Regtest instead of
public testnet only to skip waiting on chain sync / faucet funds; the
Lightning protocol code path (BOLT11 invoices, onion-routed HTLCs, preimage
reveal on settlement) is identical either way. Images are
[Polar](https://lightningpolar.com)'s — the standard tool for exactly this
local-regtest-Lightning-network use case, reused rather than hand-rolled.

## One-time setup

```bash
docker compose up -d
sleep 5   # let bitcoind + both lnd nodes finish starting
```

Fund alice on-chain and mine to coinbase maturity:

```bash
ALICE_ADDR=$(docker exec lightning-lnd-alice-1 lncli --network=regtest --lnddir=/home/lnd/.lnd \
  newaddress p2wkh | grep -o '"address": *"[^"]*"' | cut -d'"' -f4)
docker exec lightning-bitcoind-1 bitcoin-cli -regtest -rpcuser=polaruser -rpcpassword=polarpass \
  generatetoaddress 101 "$ALICE_ADDR"
sleep 8   # let lnd catch up its wallet rescan
docker exec lightning-lnd-alice-1 lncli --network=regtest --lnddir=/home/lnd/.lnd walletbalance
```

Connect the two nodes and open a channel:

```bash
BOB_PUBKEY=$(docker exec lightning-lnd-bob-1 lncli --network=regtest --lnddir=/home/lnd/.lnd \
  getinfo | grep identity_pubkey | grep -o '"[0-9a-f]\{66\}"' | tr -d '"')
docker exec lightning-lnd-alice-1 lncli --network=regtest --lnddir=/home/lnd/.lnd \
  connect "${BOB_PUBKEY}@lnd-bob:9735"
docker exec lightning-lnd-alice-1 lncli --network=regtest --lnddir=/home/lnd/.lnd \
  openchannel --node_key="$BOB_PUBKEY" --local_amt=1000000 --min_confs=1
```

Mine confirmations so the channel goes active:

```bash
ALICE_ADDR2=$(docker exec lightning-lnd-alice-1 lncli --network=regtest --lnddir=/home/lnd/.lnd \
  newaddress p2wkh | grep -o '"address": *"[^"]*"' | cut -d'"' -f4)
docker exec lightning-bitcoind-1 bitcoin-cli -regtest -rpcuser=polaruser -rpcpassword=polarpass \
  generatetoaddress 6 "$ALICE_ADDR2"
sleep 5
docker exec lightning-lnd-alice-1 lncli --network=regtest --lnddir=/home/lnd/.lnd listchannels
# look for "active": true
```

## Use it

```bash
cd ..
python3 poc_challenge_auction.py --lightning
```

Every auction round's winner gets settled with a real HTLC: bob (payee,
stands in for "the winning peer") creates a real BOLT11 invoice, alice
(payer, stands in for "the requester") pays it over the real channel.
`lightning_settle.py` independently re-hashes the revealed preimage and
checks it against the invoice's payment_hash locally — doesn't just trust
LND's own "SUCCEEDED" status string.

Real run, 5 winning rounds:

```
WINNER: bob     9 sat  preimage 4ac71143706b... verified against payment_hash d1be1130c553...
WINNER: bob     5 sat  preimage eea88d802d1a... verified against payment_hash 8acabea4ddf7...
WINNER: bob     6 sat  preimage 76f1ade0f9be... verified against payment_hash f9c9bda28be0...
WINNER: bob    10 sat  preimage 96e696c1cde9... verified against payment_hash f8eeb66d1878...
WINNER: mallory 1 sat  preimage 508385841cb3... verified against payment_hash 1c2872b6018f...
```

Note mallory's round: she won that round of the challenge fairly (passed
the possession check that round — see the main README's Part 1 for why she
doesn't always cheat), and the settlement layer doesn't and shouldn't care
*why* someone won, only that they passed verification and had the lowest
bid. Bob's cumulative channel balance after this run (2765 sat) matches the
sum of every settled payment exactly — checked, not assumed.

## Standalone smoke test

```bash
python3 ../lightning_settle.py
```

Settles one 1234-sat test payment and re-verifies the preimage locally,
independent of the auction.

## Teardown

```bash
docker compose down -v
```

`-v` also drops the regtest chain state and both node wallets — next `up`
starts completely fresh, redo the one-time setup above.
