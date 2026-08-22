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
import shlex
import threading

import discovery_relay
import node


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

    def preloop(self):
        try:
            import readline
            readline.set_completer_delims(' \t\n')
        except ImportError:
            pass

    def emptyline(self):
        pass  # don't repeat the last command on a bare Enter, like ott

    # ── commands ─────────────────────────────────────────────────────────

    def do_whoami(self, arg):
        """whoami  — print your persistent node pubkey (~/.dura_identity.key)."""
        print(f'  {self.identity.pubkey_hex()}')

    def do_host(self, arg):
        """host <archive_dir> [--file NAME] [--port N] [--relay URL] [--no-announce] [--advertise-host HOST]
        — serve a real archived file in a background thread (shell stays usable),
        announcing it on --relay (default: your session's default relay — see `relay`)
        unless --no-announce is given."""
        parts = shlex.split(arg)
        if not parts:
            print('  usage: host <archive_dir> [--file NAME] [--port N] [--relay URL] [--no-announce] [--advertise-host HOST]')
            return
        archive_dir = parts[0]
        file_name = None
        port = 9201
        relay = self.default_relay
        no_announce = '--no-announce' in parts
        advertise_host = '127.0.0.1'
        i = 1
        while i < len(parts):
            if parts[i] == '--file' and i + 1 < len(parts):
                i += 1; file_name = parts[i]
            elif parts[i] == '--port' and i + 1 < len(parts):
                i += 1; port = int(parts[i])
            elif parts[i] == '--relay' and i + 1 < len(parts):
                i += 1; relay = parts[i]
            elif parts[i] == '--advertise-host' and i + 1 < len(parts):
                i += 1; advertise_host = parts[i]
            i += 1

        entry = node.find_manifest_entry(archive_dir, file_name)
        if relay and not no_announce:
            result = node.publish(self.identity, relay, entry['sha256'], entry['name'],
                                   f'{advertise_host}:{port}')
            print(f'  announced on {relay}: {result}')
        elif not relay:
            print('  no relay set (run `relay` first, or pass --relay) — hosting without announcing')
        t = threading.Thread(target=node.run_host_server, args=(archive_dir, file_name, port), daemon=True)
        t.start()
        self._host_threads.append(t)
        print(f'  hosting {entry["name"]} on port {port} in the background — shell still usable')

    def do_relay(self, arg):
        """relay [port]  — run a real discovery relay in the background (default port 9101),
        so you don't need a separate terminal for one. Sets it as the default relay for
        discover/download/like/subscribe in this session."""
        port = int(arg.strip()) if arg.strip() else 9101
        t = threading.Thread(target=discovery_relay.run_relay_server, args=(port,), daemon=True)
        t.start()
        self._host_threads.append(t)
        self.default_relay = f'http://127.0.0.1:{port}'
        print(f'  relay running on port {port} in the background — set as your default relay')

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
        """download <content_hash_or_prefix> [--out FILE] [--relay URL]
        — actually download and chunk-verify a file. Tab-completes against
        the last `discover`."""
        parts = shlex.split(arg)
        if not parts:
            print('  usage: download <content_hash_or_prefix> [--out FILE] [--relay URL]')
            return
        prefix = parts[0]
        out = None
        relay = self.default_relay
        i = 1
        while i < len(parts):
            if parts[i] == '--out' and i + 1 < len(parts):
                i += 1; out = parts[i]
            elif parts[i] == '--relay' and i + 1 < len(parts):
                i += 1; relay = parts[i]
            i += 1

        match = next((r for r in self._last_discovery if r['content_hash'].startswith(prefix)), None)
        if not match:
            results = node.discover([relay])
            self._last_discovery = results
            match = next((r for r in results if r['content_hash'].startswith(prefix)), None)
        if not match:
            print(f'  no content matching {prefix} — run discover first')
            return
        host, port = match['host'].rsplit(':', 1)
        node.download(host, int(port), out or match['title'])

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
    def do_disc(self, arg): return self.do_discover(arg)
    def do_dl(self, arg): return self.do_download(arg)
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

    def complete_like(self, text, line, begidx, endidx):
        return self._known_hashes(text)

    def complete_l(self, *a):
        return self.complete_like(*a)

    def complete_subscribe(self, text, line, begidx, endidx):
        return self._known_pubkeys(text)

    def complete_sub(self, *a):
        return self.complete_subscribe(*a)
