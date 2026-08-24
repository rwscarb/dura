#!/usr/bin/env python3
"""
Interactive shell for dura.py, same pattern as btcvm/ott.py's OttShell:
cmd.Cmd, readline tab completion, short aliases, Ctrl-D/q to exit. `host`
runs the server in a background thread instead of blocking the shell, so
you can host and discover/download/like in the same session — ott doesn't
have a precedent for a long-running command since nothing in ott blocks
forever, this is a genuinely new case, not copied.

Tab completion resolves against real state, same idea as ott's
completions (which complete against the real archive, not a fixed list):
`download`/`like` complete against content hashes actually seen in the
last `discover`; `subscribe` completes against pubkeys actually seen.
"""
import cmd
import os
import shlex
import threading

import dht
import discovery_relay
import node
import web_ui


def _bg(fn, *args, **kwargs):
    """Wrap a background-thread target so a real failure (bad bind address,
    port already in use, whatever) prints the shell's own '  ✗ ...' one-
    liner instead of Python's default thread traceback landing raw in the
    middle of the prompt. onecmd()'s try/except below only ever covers the
    synchronous part of a do_* call -- host/relay/serve all hand the actual
    work to a background thread, and nothing catches what *that* raises
    once do_* has already returned and printed its 'running in the
    background' line. Every threading.Thread(target=...) in this file
    should go through this instead of calling the real function directly."""
    try:
        fn(*args, **kwargs)
    except SystemExit as e:
        print(f'\n  ✗ {e}')
    except Exception as e:
        print(f'\n  ✗ {type(e).__name__}: {e}')


class DuraShell(cmd.Cmd):
    intro = (
        '\n  dura — censorship-resistant video PoC node\n'
        '  Type help or ? for commands. Tab completes. Ctrl-D or q to exit.\n'
    )
    prompt = 'dura> '

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.identity = node.load_or_create_identity()
        self._last_discovery = []   # cache of the last `discover` results, for tab completion
        self._host_threads = []     # background host() threads started this session
        self.default_relay = 'http://127.0.0.1:9101'
        self.dht_node = None        # active dht.DHTNode, once `dht start` has run

    def preloop(self):
        try:
            import readline
            readline.set_completer_delims(' \t\n')
        except ImportError:
            pass

    def emptyline(self):
        pass  # don't repeat the last command on a bare Enter, like ott

    def onecmd(self, line):
        """ott's do_* methods each catch their own expected errors (OttError,
        OttNotFoundError) locally rather than needing a shell-wide net —
        that works there because ott's operations are all local/filesystem.
        dura's commands do real network I/O, depend on packages that might
        not be installed, and several node.py functions call sys.exit() on
        expected failures (missing archive, hash mismatch, unreachable
        host) — correct for the one-shot CLI, where sys.exit() ending the
        process IS the right behavior, but fatal here: the shell's own
        `quit`/`q`/Ctrl-D exit via a do_* method returning True, never via
        SystemExit, so there's no legitimate case where letting SystemExit
        (or anything else) propagate out of a command is correct — it would
        just kill the session, including any background host/relay threads
        still running. Catch broadly, print, stay alive."""
        try:
            return super().onecmd(line)
        except ModuleNotFoundError as e:
            # e.name is the *module* name (e.g. 'ott'), not necessarily the pip package
            # name (btcvm) — don't suggest `pip install {e.name}`, it's wrong here and
            # would be wrong again for any other module/package name mismatch.
            print(f'  ✗ {e} — pip install -r requirements.txt')
        except SystemExit as e:
            print(f'  ✗ {e}')
        except Exception as e:
            print(f'  ✗ {type(e).__name__}: {e}')

    # ── commands ─────────────────────────────────────────────────────────

    def do_whoami(self, arg):
        """whoami  — print your persistent node pubkey (~/.dura_identity.key)."""
        print(f'  {self.identity.pubkey_hex()}')

    def do_host(self, arg):
        """host <archive_dir> [--file NAME] [--port N] [--price SAT] [--relay URL]
        [--no-announce] [--advertise-host HOST] [--tunnel [tls://]RELAY_HOST:PORT]
        — serve every archived file in archive_dir in a background thread
        (shell stays usable), one port, downloaders SELECT which by content
        hash. Pass --file to restrict to a single file. Announces each file
        on --relay (default: your session's default relay — see `relay`)
        unless --no-announce is given. --price sets what download charges
        (default free). --tunnel registers with a tunnel_relay.py instead of
        relying on a reachable inbound port — for hosting behind NAT/CGNAT,
        and only supports one file at a time, so requires --file if
        archive_dir has more than one. Prefix tls:// if the relay
        terminates TLS at the edge (e.g. a Fly service with handlers =
        ["tls"]) — the relay process itself never needs to know."""
        parts = shlex.split(arg)
        if not parts:
            print('  usage: host <archive_dir> [--file NAME] [--port N] [--price SAT] '
                  '[--relay URL] [--no-announce] [--advertise-host HOST] '
                  '[--tunnel RELAY_HOST:PORT]')
            return
        archive_dir = parts[0]
        file_name = None
        port = 9201
        price = 0
        relay = self.default_relay
        no_announce = '--no-announce' in parts
        advertise_host = '127.0.0.1'
        tunnel = None
        i = 1
        while i < len(parts):
            if parts[i] == '--file' and i + 1 < len(parts):
                i += 1
                file_name = parts[i]
            elif parts[i] == '--port' and i + 1 < len(parts):
                i += 1
                port = int(parts[i])
            elif parts[i] == '--price' and i + 1 < len(parts):
                i += 1
                price = int(parts[i])
            elif parts[i] == '--relay' and i + 1 < len(parts):
                i += 1
                relay = parts[i]
            elif parts[i] == '--advertise-host' and i + 1 < len(parts):
                i += 1
                advertise_host = parts[i]
            elif parts[i] == '--tunnel' and i + 1 < len(parts):
                i += 1
                tunnel = parts[i]
            i += 1

        entries = node.load_manifest_entries(archive_dir, file_name)
        if tunnel and len(entries) > 1:
            print(f'  {len(entries)} files in {archive_dir} but --tunnel only serves one at a time '
                  f'— pass --file NAME to pick one')
            return
        # fail fast, before announcing anything — a manifest entry with no
        # matching chunk data would otherwise get announced to the relay
        # and only fail later, in the background server thread (onecmd's
        # exception net doesn't reach into background threads)
        all_leaves = {e['sha256']: node.load_leaves(archive_dir, e['sha256']) for e in entries}
        if relay and not no_announce:
            for entry in entries:
                result = node.publish(self.identity, relay, entry['sha256'], entry['name'],
                                       f'{advertise_host}:{port}', tunnel=tunnel)
                print(f'  announced {entry["name"]} on {relay}: {result}')
        elif not relay:
            print('  no relay set (run `relay` first, or pass --relay) — hosting without announcing')
        if tunnel:
            entry = entries[0]
            relay_host, relay_port, use_tls = node._parse_tunnel(tunnel)
            expanded_dir = os.path.expanduser(archive_dir)
            file_path = entry.get('last_path') or os.path.join(expanded_dir, entry['name'])
            tt = threading.Thread(target=_bg,
                                   args=(node.run_host_tunnel, relay_host, relay_port,
                                         entry['sha256'], entry, all_leaves[entry['sha256']],
                                         file_path, price),
                                   kwargs={'use_tls': use_tls, 'quiet': True}, daemon=True)
            tt.start()
            self._host_threads.append(tt)
        t = threading.Thread(target=_bg,
                              args=(node.run_host_server, archive_dir, file_name, port),
                              kwargs={'quiet': True, 'price': price}, daemon=True)
        t.start()
        self._host_threads.append(t)
        price_note = f', {price} sat/download' if price else ', free'
        tunnel_note = f', tunneled via {tunnel}' if tunnel else ''
        if len(entries) == 1:
            print(f'  hosting {entries[0]["name"]} on port {port} in the background{price_note}'
                  f'{tunnel_note} — shell still usable')
        else:
            print(f'  hosting {len(entries)} files on port {port} in the background{price_note} '
                  f'— shell still usable')

    def do_relay(self, arg):
        """relay [port]  — run a real discovery relay in the background (default port 9101),
        so you don't need a separate terminal for one. Sets it as the default relay for
        discover/download/like/subscribe in this session."""
        port = int(arg.strip()) if arg.strip() else 9101
        t = threading.Thread(target=_bg, args=(discovery_relay.run_relay_server, port),
                              kwargs={'quiet': True}, daemon=True)
        t.start()
        self._host_threads.append(t)
        self.default_relay = f'http://127.0.0.1:{port}'
        print(f'  relay running on port {port} in the background — set as your default relay')

    def do_serve(self, arg):
        """serve [bind] [port]  — run the local web control UI in the background
        (default 127.0.0.1:8080; pass 0.0.0.0 to reach it from your phone).
        Same server web_ui.py runs standalone, just launched inline so you
        don't need a second terminal. Its own page has a 📱 button for a
        scan-to-open QR code once it's up."""
        parts = shlex.split(arg)
        bind_host = parts[0] if len(parts) > 0 else '127.0.0.1'
        port = int(parts[1]) if len(parts) > 1 else 8080
        t = threading.Thread(target=_bg, args=(web_ui.run_web_ui, port),
                              kwargs={'bind_host': bind_host, 'quiet': True}, daemon=True)
        t.start()
        self._host_threads.append(t)
        if bind_host == '0.0.0.0':
            print(f'  web control UI running on all interfaces, port {port}, in the background '
                  f'— open http://127.0.0.1:{port}/ here, or use its 📱 button to reach it '
                  f'from your phone')
        else:
            print(f'  web control UI running at http://{bind_host}:{port}/ in the background')

    def do_dht(self, arg):
        """dht start [port] [bootstrap_host:port]  — start (or join) a real
        Kademlia DHT node in the background (default port 8468). No relay
        needed — once bootstrapped into an existing swarm, peers find each
        other through the DHT itself. Omit the bootstrap address to start
        a brand new swarm as its first node.
        dht announce <content_hash> <host:port> [title]  — announce you're
        hosting content, on the active node (from the last `dht start`).
        dht lookup <content_hash>  — look up who's hosting content, via
        the active node."""
        parts = shlex.split(arg)
        if not parts:
            print('  Usage: dht <start|announce|lookup> ...')
            return
        subcmd, rest = parts[0], parts[1:]
        if subcmd == 'start':
            port = int(rest[0]) if len(rest) > 0 else 8468
            bootstrap_spec = rest[1] if len(rest) > 1 else None
            try:
                node_obj = dht.DHTNode(port, bootstrap_nodes=dht._parse_bootstrap(bootstrap_spec),
                                        quiet=True)
                node_obj.start()
            except Exception as e:
                print(f'  ✗ {type(e).__name__}: {e}')
                return
            self.dht_node = node_obj
            joined = f', joined via {bootstrap_spec}' if bootstrap_spec else ' (new swarm)'
            print(f'  dht node listening on port {port}{joined} — set as the active dht node')
        elif subcmd == 'announce':
            if not self.dht_node:
                print('  no active dht node — run `dht start` first')
                return
            if len(rest) < 2:
                print('  Usage: dht announce <content_hash> <host:port> [title]')
                return
            content_hash, host_addr = rest[0], rest[1]
            title = rest[2] if len(rest) > 2 else None
            try:
                self.dht_node.announce(content_hash, host_addr, title=title)
            except Exception as e:
                print(f'  ✗ {type(e).__name__}: {e}')
                return
            print(f'  announced {content_hash[:16]}... at {host_addr} via dht')
        elif subcmd == 'lookup':
            if not self.dht_node:
                print('  no active dht node — run `dht start` first')
                return
            if not rest:
                print('  Usage: dht lookup <content_hash>')
                return
            try:
                results = self.dht_node.lookup(rest[0])
            except Exception as e:
                print(f'  ✗ {type(e).__name__}: {e}')
                return
            if not results:
                print('  nothing found on the dht for that hash')
                return
            for r in results:
                print(f'  host={r["host"]}  title={r.get("title") or "(untitled)"}')
        else:
            print(f'  unknown dht subcommand: {subcmd!r}  (start, announce, lookup)')

    def complete_dht(self, text, line, begidx, endidx):
        parts = shlex.split(line[:begidx])
        if len(parts) == 1:
            return [s for s in ('start', 'announce', 'lookup') if s.startswith(text)]
        return []

    def do_discover(self, arg):
        """discover [relay_url ...]  — list content announced on one or more relays
        (default: the last relay used, or http://127.0.0.1:9101)."""
        relays = shlex.split(arg) or [self.default_relay]
        self.default_relay = relays[0]
        results = node.discover(relays)
        self._last_discovery = results
        if not results:
            print('  nothing found')
            return
        for r in results:
            print(f'  {r["title"]!r:40s}  hash={r["content_hash"][:16]}...  '
                  f'host={r["host"]}  by={r["signer_pubkey"][:12]}...')

    def do_download(self, arg):
        """download <content_hash_or_prefix> [--out FILE] [--relay URL] [--rounds N] [--lightning]
        — resolve every host publishing this content, possession-challenge
        each one (N chunks sampled, default 3), auction survivors by
        reputation then price, optionally pay the winner over a real
        Lightning HTLC, download, and record the outcome to local
        reputation. Tab-completes against the last `discover`."""
        parts = shlex.split(arg)
        if not parts:
            print('  usage: download <content_hash_or_prefix> [--out FILE] [--relay URL] [--rounds N] [--lightning]')
            return
        prefix = parts[0]
        out = None
        relay = self.default_relay
        rounds = 3
        use_lightning = '--lightning' in parts
        i = 1
        while i < len(parts):
            if parts[i] == '--out' and i + 1 < len(parts):
                i += 1
                out = parts[i]
            elif parts[i] == '--relay' and i + 1 < len(parts):
                i += 1
                relay = parts[i]
            elif parts[i] == '--rounds' and i + 1 < len(parts):
                i += 1
                rounds = int(parts[i])
            i += 1

        node.download_with_auction(prefix, [relay], out_path=out, k=rounds, use_lightning=use_lightning)

    def do_like(self, arg):
        """like <content_hash> [--relay URL]  — sign and post a real like event."""
        parts = shlex.split(arg)
        if not parts:
            print('  usage: like <content_hash> [--relay URL]')
            return
        relay = self.default_relay
        if '--relay' in parts:
            i = parts.index('--relay')
            relay = parts[i + 1]
        event = self.identity.sign_event('like', content_hash=parts[0])
        print(f'  {node.post_event(relay, event)}')

    def do_subscribe(self, arg):
        """subscribe <target_pubkey> [--relay URL]  — sign and post a real subscribe event."""
        parts = shlex.split(arg)
        if not parts:
            print('  usage: subscribe <target_pubkey> [--relay URL]')
            return
        relay = self.default_relay
        if '--relay' in parts:
            i = parts.index('--relay')
            relay = parts[i + 1]
        event = self.identity.sign_event('subscribe', target_pubkey=parts[0])
        print(f'  {node.post_event(relay, event)}')

    def do_quit(self, arg):
        """quit  — exit the shell."""
        return True

    def do_EOF(self, arg):
        print()
        return True

    def do_q(self, arg):
        return self.do_quit(arg)

    # ── short aliases, same convention as ott ───────────────────────────

    def do_w(self, arg): return self.do_whoami(arg)
    def do_h(self, arg): return self.do_host(arg)
    def do_r(self, arg): return self.do_relay(arg)
    def do_s(self, arg): return self.do_serve(arg)
    def do_disc(self, arg): return self.do_discover(arg)
    def do_dl(self, arg): return self.do_download(arg)
    def do_get(self, arg): return self.do_download(arg)
    def do_l(self, arg): return self.do_like(arg)
    def do_sub(self, arg): return self.do_subscribe(arg)

    # ── tab completion — resolves against real discovered state ─────────

    def _known_hashes(self, text):
        return [r['content_hash'] for r in self._last_discovery if r['content_hash'].startswith(text)]

    def _known_pubkeys(self, text):
        return [r['signer_pubkey'] for r in self._last_discovery if r['signer_pubkey'].startswith(text)]

    def complete_download(self, text, line, begidx, endidx):
        return self._known_hashes(text)

    def complete_dl(self, *a):
        return self.complete_download(*a)

    def complete_get(self, *a):
        return self.complete_download(*a)

    def complete_like(self, text, line, begidx, endidx):
        return self._known_hashes(text)

    def complete_l(self, *a):
        return self.complete_like(*a)

    def complete_subscribe(self, text, line, begidx, endidx):
        return self._known_pubkeys(text)

    def complete_sub(self, *a):
        return self.complete_subscribe(*a)
