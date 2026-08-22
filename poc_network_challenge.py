#!/usr/bin/env python3
"""
Next step from poc_challenge_auction.py Part 2: the same nonce-salted
possession challenge, but over a real TCP round trip between real OS
processes instead of an in-process function call. The point is to check
whether the timing-bound idea survives contact with a real network stack,
not just modeled latency numbers.

Roles:
  holder <port>                              — stores chunks, answers challenges directly
  relay <port> <holder_host> <holder_port>    — stores nothing; chains a SECOND real TCP
                                                 connection to the holder to fetch real bytes,
                                                 then answers. Two chained real network hops
                                                 vs one — the timing gap this produces is real.
  verify-remote <h_host> <h_port> <r_host> <r_port> [b_host] [b_port]
                                               — client mode: connects to already-running
                                                 holder/relay (e.g. other docker-compose
                                                 containers) and runs the same repeated-
                                                 challenge separation analysis against them.
                                                 Optional third target (b_host/b_port) is a
                                                 second honest holder, for a same-vs-same
                                                 baseline of normal jitter.
  stats                                       — local convenience: spawns holder+relay as
                                                 subprocesses on loopback, then runs the same
                                                 analysis. (no docker needed)
  (no arg)                                    — local convenience: single-shot narrated rounds
                                                 on loopback.

See docker-compose.yml for a container-network version of `verify-remote` —
loopback numbers understate real deployment overhead (or, per the last local
run, sometimes don't separate at all); real per-container network stack
overhead is closer to what an actual deployment looks like.
"""
import base64
import hashlib
import os
import random
import socket
import subprocess
import sys
import time

CHUNK_SIZE = 4096   # smaller than the in-process PoC's 256KB — this one pays real
                     # socket + base64 overhead per call, keep it light for a quick demo
N_CHUNKS = 8
SEED = 42            # shared across processes so holder/relay/verifier agree on file
N_ROUNDS = 8
N_STATS_SAMPLES = 80
K_GRID = (1, 2, 3, 5, 8, 12, 20, 30)


def build_chunks():
    rng = random.Random(SEED)
    return [rng.randbytes(CHUNK_SIZE) for _ in range(N_CHUNKS)]


def recv_line(sock):
    buf = b''
    while not buf.endswith(b'\n'):
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
    return buf.decode().strip()


def run_holder(port, bind_host='0.0.0.0'):
    chunks = build_chunks()
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((bind_host, port))
    srv.listen(8)
    print(f"[holder:{port}] listening on {bind_host}, holds all {N_CHUNKS} chunks", flush=True)
    while True:
        conn, addr = srv.accept()
        with conn:
            line = recv_line(conn)
            parts = line.split()
            if not parts:
                continue
            if parts[0] == 'CHALLENGE':
                idx, nonce_hex = int(parts[1]), parts[2]
                nonce = bytes.fromhex(nonce_hex)
                h = hashlib.sha256(chunks[idx] + nonce).hexdigest()
                conn.sendall(f'HASH {h}\n'.encode())
            elif parts[0] == 'FETCH':
                idx = int(parts[1])
                payload = base64.b64encode(chunks[idx]).decode()
                conn.sendall(f'DATA {payload}\n'.encode())


def run_relay(port, holder_host, holder_port, bind_host='0.0.0.0'):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((bind_host, port))
    srv.listen(8)
    print(f"[relay:{port}] listening on {bind_host}, holds NOTHING locally — "
          f"relays from {holder_host}:{holder_port}", flush=True)
    while True:
        conn, addr = srv.accept()
        with conn:
            line = recv_line(conn)
            parts = line.split()
            if not parts or parts[0] != 'CHALLENGE':
                continue
            idx, nonce_hex = int(parts[1]), parts[2]
            nonce = bytes.fromhex(nonce_hex)
            # second, real, chained TCP connection — genuinely fetches the real bytes,
            # over the real network (loopback locally, real container network in compose)
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as up:
                up.connect((holder_host, holder_port))
                up.sendall(f'FETCH {idx}\n'.encode())
                resp = recv_line(up)
            data = base64.b64decode(resp.split(' ', 1)[1])
            h = hashlib.sha256(data + nonce).hexdigest()
            conn.sendall(f'HASH {h}\n'.encode())


def wait_for_port(host, port, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def challenge(host, port, idx, expected_chunks, label, verbose=True):
    nonce = os.urandom(16)
    t0 = time.perf_counter()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.connect((host, port))
        s.sendall(f'CHALLENGE {idx} {nonce.hex()}\n'.encode())
        resp = recv_line(s)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    claimed = resp.split(' ', 1)[1] if resp.startswith('HASH ') else None
    expected = hashlib.sha256(expected_chunks[idx] + nonce).hexdigest()
    ok = claimed == expected
    if verbose:
        print(f"  [{label:6s}] chunk #{idx}  {elapsed_ms:6.2f}ms  "
              f"{'hash OK' if ok else 'HASH MISMATCH'}")
    return elapsed_ms, ok


def session_mean_bounds(samples, k, n_trials=3000):
    """(min, max) of the mean of k samples drawn with replacement from `samples`,
    over n_trials draws — an empirical bootstrap, not an assumed distribution."""
    means = []
    for _ in range(n_trials):
        draw = [random.choice(samples) for _ in range(k)]
        means.append(sum(draw) / k)
    return min(means), max(means)


def statistics_mean(xs):
    return sum(xs) / len(xs)


def collect_samples(holder_host, holder_port, relay_host, relay_port, n_samples=N_STATS_SAMPLES):
    expected_chunks = build_chunks()
    holder_times, relay_times = [], []
    for i in range(n_samples):
        idx = random.Random(2000 + i).randrange(N_CHUNKS)
        t, _ = challenge(holder_host, holder_port, idx, expected_chunks, 'holder', verbose=False)
        holder_times.append(t)
        t, _ = challenge(relay_host, relay_port, idx, expected_chunks, 'relay', verbose=False)
        relay_times.append(t)
    return holder_times, relay_times


def analyze_and_report(holder_times, relay_times, baseline_times=None, baseline_label='holder2'):
    print(f"\nholder: mean {statistics_mean(holder_times):.3f}ms  "
          f"min {min(holder_times):.3f}ms  max {max(holder_times):.3f}ms")
    print(f"relay:  mean {statistics_mean(relay_times):.3f}ms  "
          f"min {min(relay_times):.3f}ms  max {max(relay_times):.3f}ms")
    if baseline_times:
        print(f"{baseline_label} (2nd honest holder, same-vs-same jitter baseline): "
              f"mean {statistics_mean(baseline_times):.3f}ms  "
              f"min {min(baseline_times):.3f}ms  max {max(baseline_times):.3f}ms")

    print(f"\n{'session size k':>14}  {'worst honest mean':>18}  {'best cheater mean':>18}  separated?")
    first_k_separated = None
    for k in K_GRID:
        h_worst = session_mean_bounds(holder_times, k)[1]
        r_best = session_mean_bounds(relay_times, k)[0]
        sep = h_worst < r_best
        if sep and first_k_separated is None:
            first_k_separated = k
        print(f"{k:>14}  {h_worst:>16.3f}ms  {r_best:>16.3f}ms  {'YES' if sep else 'no'}")

    if first_k_separated:
        print(f"\nverdict: averaging {first_k_separated} repeated challenges per session "
              f"separates holder from relay reliably here.")
    else:
        print("\nverdict: even multi-round averaging didn't cleanly separate them in this "
              "run — needs more samples per session or more real network distance.")
    return first_k_separated


def run_remote_stats(h_host, h_port, r_host, r_port, b_host=None, b_port=None):
    print(f"verifier connecting to holder={h_host}:{h_port}  relay={r_host}:{r_port}"
          + (f"  baseline={b_host}:{b_port}" if b_host else ''))
    if not (wait_for_port(h_host, h_port) and wait_for_port(r_host, r_port)
            and (b_host is None or wait_for_port(b_host, b_port))):
        print("one or more targets never came up"); return
    time.sleep(0.2)

    print(f"collecting {N_STATS_SAMPLES} real samples from each over the real network...")
    holder_times, relay_times = collect_samples(h_host, h_port, r_host, r_port)
    baseline_times = None
    if b_host:
        expected_chunks = build_chunks()
        baseline_times = []
        for i in range(N_STATS_SAMPLES):
            idx = random.Random(5000 + i).randrange(N_CHUNKS)
            t, _ = challenge(b_host, b_port, idx, expected_chunks, 'holder2', verbose=False)
            baseline_times.append(t)
    analyze_and_report(holder_times, relay_times, baseline_times)


def run_stats():
    """Local convenience: spawn holder+relay as subprocesses on loopback,
    run the same analysis collect_samples/analyze_and_report do for real
    remote targets."""
    holder_port, relay_port = 8901, 8902
    print("spawning real subprocesses: one holder, one relay\n")
    holder_proc = subprocess.Popen([sys.executable, __file__, 'holder', str(holder_port)])
    relay_proc = subprocess.Popen(
        [sys.executable, __file__, 'relay', str(relay_port), '127.0.0.1', str(holder_port)])
    try:
        if not (wait_for_port('127.0.0.1', holder_port) and wait_for_port('127.0.0.1', relay_port)):
            print("processes didn't come up in time"); return
        time.sleep(0.2)
        print(f"collecting {N_STATS_SAMPLES} real samples from each (no per-round printing)...")
        holder_times, relay_times = collect_samples('127.0.0.1', holder_port, '127.0.0.1', relay_port)
        analyze_and_report(holder_times, relay_times)
    finally:
        holder_proc.terminate(); relay_proc.terminate()
        holder_proc.wait(); relay_proc.wait()


def run_single_shot_demo():
    holder_port, relay_port = 8901, 8902
    expected_chunks = build_chunks()

    print("spawning real subprocesses: one holder, one relay (chains a real 2nd TCP hop to the holder)\n")
    holder_proc = subprocess.Popen([sys.executable, __file__, 'holder', str(holder_port)])
    relay_proc = subprocess.Popen(
        [sys.executable, __file__, 'relay', str(relay_port), '127.0.0.1', str(holder_port)])
    try:
        if not (wait_for_port('127.0.0.1', holder_port) and wait_for_port('127.0.0.1', relay_port)):
            print("processes didn't come up in time"); return
        time.sleep(0.2)

        holder_times, relay_times = [], []
        print(f"\nrunning {N_ROUNDS} real challenge rounds against each over loopback TCP:\n")
        for i in range(N_ROUNDS):
            idx = random.Random(1000 + i).randrange(N_CHUNKS)
            print(f"round {i+1}, chunk #{idx}:")
            t, ok = challenge('127.0.0.1', holder_port, idx, expected_chunks, 'holder')
            holder_times.append(t)
            t, ok = challenge('127.0.0.1', relay_port, idx, expected_chunks, 'relay')
            relay_times.append(t)

        h_max, r_min = max(holder_times), min(relay_times)
        print(f"\nholder: avg {statistics_mean(holder_times):.2f}ms  min {min(holder_times):.2f}ms  max {h_max:.2f}ms")
        print(f"relay:  avg {statistics_mean(relay_times):.2f}ms  min {r_min:.2f}ms  max {max(relay_times):.2f}ms")
        gap = r_min - h_max
        print(f"\nworst-case holder ({h_max:.2f}ms) vs best-case relay ({r_min:.2f}ms): gap = {gap:.2f}ms")
        if gap > 0:
            bound = (h_max + r_min) / 2
            print(f"clean separation even on loopback — a time bound around {bound:.2f}ms "
                  f"would pass every real holder round and fail every relay round in this run.")
        else:
            print("NO clean separation on loopback this run — see `stats` mode for the "
                  "repeated-challenge fix.")
    finally:
        holder_proc.terminate(); relay_proc.terminate()
        holder_proc.wait(); relay_proc.wait()


def main():
    args = sys.argv[1:]
    if len(args) >= 2 and args[0] == 'holder':
        run_holder(int(args[1])); return
    if len(args) >= 4 and args[0] == 'relay':
        run_relay(int(args[1]), args[2], int(args[3])); return
    if len(args) >= 4 and args[0] == 'verify-remote':
        h_host, h_port, r_host, r_port = args[1], int(args[2]), args[3], int(args[4])
        b_host, b_port = (args[5], int(args[6])) if len(args) >= 7 else (None, None)
        run_remote_stats(h_host, h_port, r_host, r_port, b_host, b_port); return
    if args and args[0] == 'stats':
        run_stats(); return
    run_single_shot_demo()


if __name__ == '__main__':
    main()
