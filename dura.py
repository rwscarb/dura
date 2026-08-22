#!/usr/bin/env python3
"""
dura — single CLI entry point for this repo's PoC mechanisms.

Thin argparse wrapper over the Makefile, not a reimplementation of it — the
Makefile stays the single source of truth for what each command actually
runs (already tested target by target); this just gives it subcommands,
`--help` text, and a name instead of needing to remember `make` targets.
"""
import argparse
import os
import subprocess
import sys

REPO_DIR = os.path.dirname(os.path.abspath(__file__))

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
    sub = parser.add_subparsers(dest='command', required=True)

    for name, (_target, help_text) in COMMANDS.items():
        sub.add_parser(name, help=help_text)

    lightning = sub.add_parser('lightning', help='real bitcoind + LND regtest stack (see lightning/README.md)')
    lightning_sub = lightning.add_subparsers(dest='lightning_command', required=True)
    for name, (_target, help_text) in LIGHTNING_COMMANDS.items():
        lightning_sub.add_parser(name, help=help_text)

    return parser


def main():
    args = build_parser().parse_args()
    if args.command == 'lightning':
        target, _ = LIGHTNING_COMMANDS[args.lightning_command]
    else:
        target, _ = COMMANDS[args.command]
    run_make(target)


if __name__ == '__main__':
    main()
