#!/usr/bin/env python3
"""
dht.py — real Kademlia DHT discovery, no relay involved.

Uses the real `kademlia` library (pip install kademlia) rather than
reimplementing Kademlia's node-ID/k-bucket/RPC machinery — that's exactly
the kind of already-solved distributed-systems problem worth reusing, not
rebuilding, especially for a PoC.

Scoped deliberately: this answers "how do two nodes find each other
without a shared relay" — announce(content_hash, host_addr) /
lookup(content_hash) — not the richer signed-event system (publish/like/
subscribe/attestation) discovery_relay.py already handles. Merging that
into DHT value storage is a separate, bigger design question (Kademlia's
plain key→value store isn't naturally an append-only event log); kept out
of this file on purpose. Every DHT node is a full peer — there's no
separate "relay" role here, only who happened to join first.
"""
import asyncio
import json
import threading

from kademlia.network import Server


async def _announce(server, content_hash, host_addr, title=None):
    """Merge this announcer into the existing list under content_hash
    instead of overwriting it — multiple peers can host the same content,
    and a blind set() would silently drop everyone else's announcement.
    Not race-free under concurrent announces to the same key (last write
    still wins on a true conflict) — a real CRDT merge is out of scope."""
    existing_raw = await server.get(content_hash)
    entries = json.loads(existing_raw) if existing_raw else []
    entries = [e for e in entries if e.get('host') != host_addr]  # replace any stale self-entry
    entries.append({'host': host_addr, 'title': title})
    await server.set(content_hash, json.dumps(entries))


async def _lookup(server, content_hash):
    raw = await server.get(content_hash)
    return json.loads(raw) if raw else []


class DHTNode:
    """Runs a real kademlia.network.Server in its own thread with its own
    asyncio event loop, and exposes plain synchronous announce()/lookup()
    methods any other thread (the shell, a CLI command) can call directly
    — kademlia's Server is asyncio-only, but nothing else in this repo's
    background-thread model (relay/host/web UI) is, so this is the
    adapter, not a rewrite of everything else to asyncio."""

    def __init__(self, port, bootstrap_nodes=None, quiet=False):
        self.port = port
        self.bootstrap_nodes = bootstrap_nodes or []
        self.quiet = quiet
        self.loop = None
        self.server = None
        self._ready = threading.Event()
        self._error = None

    def start(self):
        t = threading.Thread(target=self._run, daemon=True)
        t.start()
        if not self._ready.wait(timeout=15):
            raise RuntimeError('DHT node did not finish starting within 15s')
        if self._error:
            raise self._error
        return t

    def _run(self):
        try:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self.server = Server()
            self.loop.run_until_complete(self._setup())
        except Exception as e:
            self._error = e
            self._ready.set()
            return
        self._ready.set()
        self.loop.run_forever()

    async def _setup(self):
        await self.server.listen(self.port)
        if self.bootstrap_nodes:
            await self.server.bootstrap(self.bootstrap_nodes)
        if not self.quiet:
            joined = f', bootstrapped via {self.bootstrap_nodes}' if self.bootstrap_nodes else \
                     ' (first node in a new swarm)'
            print(f'[dht:{self.port}] node listening{joined}', flush=True)

    def announce(self, content_hash, host_addr, title=None, timeout=15):
        fut = asyncio.run_coroutine_threadsafe(
            _announce(self.server, content_hash, host_addr, title), self.loop)
        return fut.result(timeout=timeout)

    def lookup(self, content_hash, timeout=15):
        fut = asyncio.run_coroutine_threadsafe(_lookup(self.server, content_hash), self.loop)
        return fut.result(timeout=timeout)

    def stop(self):
        if self.loop and self.server:
            self.loop.call_soon_threadsafe(self.server.stop)
            self.loop.call_soon_threadsafe(self.loop.stop)


def run_dht_node(port, bootstrap_nodes=None, quiet=False):
    """Blocking entry point, same contract as run_relay_server/
    run_host_server — call this directly for a foreground CLI process, or
    hand it to threading.Thread(target=...) to run in the background."""
    node = DHTNode(port, bootstrap_nodes=bootstrap_nodes, quiet=quiet)
    t = threading.Thread(target=node._run, daemon=True)
    t.start()
    t.join()


def _parse_bootstrap(spec):
    """'host:port' or 'host:port,host:port' -> [(host, port), ...], or
    None if spec is falsy."""
    if not spec:
        return None
    out = []
    for part in spec.split(','):
        host, port_s = part.rsplit(':', 1)
        out.append((host, int(port_s)))
    return out


def main():
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8468
    bootstrap = _parse_bootstrap(sys.argv[2]) if len(sys.argv) > 2 else None
    run_dht_node(port, bootstrap_nodes=bootstrap)


if __name__ == '__main__':
    main()
