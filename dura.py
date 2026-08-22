#!/usr/bin/env python3
"""
dura — single CLI entry point for this repo's PoC mechanisms.

Two kinds of subcommand:

  - demo/network/stats/... — thin argparse wrapper over the Makefile, not a
    reimplementation of it. The Makefile stays the single source of truth
    for what each demo actually runs; this just gives it subcommands and
    `--help` text instead of needing to remember `make` targets.

  - host/discover/download/like/subscribe/whoami — real commands, not demo
    wrappers. These call directly into node.py and actually host a file,
    actually download one from a peer with per-chunk verification, actually
    post/read signed events from a relay. This is the integration piece —
    one tool, not six disconnected scripts.
"""
import argparse
import os
import subprocess
import sys

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO_DIR)

COMMANDS = {
    'demo':         ('demo',         'possession-gated auction, narrated (Parts 1 + 2)'),
    'network':      ('network',      'real sockets, single-shot challenge rounds, loopback'),
    'stats':        ('stats',        'real sockets, repeated-challenge separation analysis'),
    'discovery':    ('discovery',    '3 real relays, personalized ranking, sybil test'),
    'reputation':   ('reputation',   'signed attestations + revocation demo'),
    'chart':        ('chart',        'regenerate the README separation chart'),
    'containers':   ('containers',   'same challenge test, real docker containers'),
    'real-archive': ('real-archive', 'challenge mechanism against a real .ott video archive'),
    'clean':        ('clean',        'remove __pycache__, tmp reputation stores'),
}

LIGHTNING_COMMANDS = {
    'up':    ('lightning-up',    'start bitcoind + 2 LND nodes on regtest'),
    'down':  ('lightning-down',  'tear down the lightning stack (drops chain state + wallets)'),
    'demo':  ('lightning-demo',  'poc_challenge_auction.py --lightning — real HTLC settlement'),
    'smoke': ('lightning-smoke', 'one real test payment via lightning_settle.py'),
}


def run_make(target):
    result = subprocess.run(['make', target], cwd=REPO_DIR)
    sys.exit(result.returncode)


def build_parser():
    parser = argparse.ArgumentParser(
        prog='dura',
        description='Censorship-resistant video PoC — real mechanisms behind the #all-pdx brainstorm.')
    sub = parser.add_subparsers(dest='command', required=False)

    sub.add_parser('shell', help='interactive shell with tab completion (also the default with no command)')

    for name, (_target, help_text) in COMMANDS.items():
        sub.add_parser(name, help=help_text)

    lightning = sub.add_parser('lightning', help='real bitcoind + LND regtest stack (see lightning/README.md)')
    lightning_sub = lightning.add_subparsers(dest='lightning_command', required=True)
    for name, (_target, help_text) in LIGHTNING_COMMANDS.items():
        lightning_sub.add_parser(name, help=help_text)

    p_whoami = sub.add_parser('whoami', help='print your persistent node pubkey (~/.dura_identity.key)')

    p_host = sub.add_parser('host', help='actually serve a real archived file to the real network')
    p_host.add_argument('archive_dir', help='directory containing .ott/ (e.g. real_archive)')
    p_host.add_argument('--file', help='which archived file, if more than one (default: most recent)')
    p_host.add_argument('--port', type=int, default=9201)
    p_host.add_argument('--relay', action='append', default=[], help='relay URL to announce on (repeatable)')
    p_host.add_argument('--advertise-host', default='127.0.0.1',
                         help='address to tell the relay to advertise (no NAT traversal here — '
                              'set this to your real reachable IP if hosting off localhost)')

    p_discover = sub.add_parser('discover', help='list real content announced on one or more relays')
    p_discover.add_argument('--relay', action='append', default=['http://127.0.0.1:9101'],
                             help='relay URL to query (repeatable)')

    p_download = sub.add_parser('download', help='actually download a file from a real host, chunk-verified')
    p_download.add_argument('content_hash', nargs='?', help='content hash to resolve via --relay')
    p_download.add_argument('--from', dest='from_addr', help='host:port to connect to directly, skipping discovery')
    p_download.add_argument('--relay', action='append', default=['http://127.0.0.1:9101'],
                             help='relay URL to resolve content_hash against (repeatable)')
    p_download.add_argument('--out', help='output path (default: the advertised filename)')

    p_like = sub.add_parser('like', help='sign and post a real like event')
    p_like.add_argument('content_hash')
    p_like.add_argument('--relay', default='http://127.0.0.1:9101')

    p_subscribe = sub.add_parser('subscribe', help='sign and post a real subscribe event')
    p_subscribe.add_argument('target_pubkey')
    p_subscribe.add_argument('--relay', default='http://127.0.0.1:9101')

    return parser


def cmd_whoami(args):
    import node
    identity = node.load_or_create_identity()
    print(identity.pubkey_hex())


def cmd_host(args):
    import node
    identity = node.load_or_create_identity()
    entry = node.find_manifest_entry(args.archive_dir, args.file)
    for relay_url in args.relay:
        host_addr = f'{args.advertise_host}:{args.port}'
        result = node.publish(identity, relay_url, entry['sha256'], entry['name'], host_addr)
        print(f"announced on {relay_url}: {result}")
    node.run_host_server(args.archive_dir, args.file, args.port)


def cmd_discover(args):
    import node
    results = node.discover(args.relay)
    if not results:
        print("nothing found (relay(s) unreachable, or nothing published yet)")
        return
    for r in results:
        print(f"  {r['title']!r:40s}  hash={r['content_hash'][:16]}...  host={r['host']}  "
              f"by={r['signer_pubkey'][:12]}...")


def cmd_download(args):
    import node
    if args.from_addr:
        host, port = args.from_addr.rsplit(':', 1)
        title = None
    else:
        if not args.content_hash:
            sys.exit("need a content_hash (to resolve via --relay) or --from host:port")
        results = node.discover(args.relay)
        match = next((r for r in results if r['content_hash'].startswith(args.content_hash)), None)
        if not match:
            sys.exit(f"no published content matching {args.content_hash} found on {args.relay}")
        host, port = match['host'].rsplit(':', 1)
        title = match['title']
    out_path = args.out or title or f'download_{args.content_hash or "file"}'
    node.download(host, int(port), out_path)


def cmd_like(args):
    import node
    identity = node.load_or_create_identity()
    event = identity.sign_event('like', content_hash=args.content_hash)
    print(node.post_event(args.relay, event))


def cmd_subscribe(args):
    import node
    identity = node.load_or_create_identity()
    event = identity.sign_event('subscribe', target_pubkey=args.target_pubkey)
    print(node.post_event(args.relay, event))


NATIVE_COMMANDS = {
    'whoami': cmd_whoami, 'host': cmd_host, 'discover': cmd_discover,
    'download': cmd_download, 'like': cmd_like, 'subscribe': cmd_subscribe,
}


def main():
    args = build_parser().parse_args()
    if args.command is None or args.command == 'shell':
        import shell
        try:
            shell.DuraShell().cmdloop()
        except KeyboardInterrupt:
            print()
        return
    if args.command in NATIVE_COMMANDS:
        NATIVE_COMMANDS[args.command](args)
        return
    if args.command == 'lightning':
        target, _ = LIGHTNING_COMMANDS[args.lightning_command]
    else:
        target, _ = COMMANDS[args.command]
    run_make(target)


if __name__ == '__main__':
    main()
