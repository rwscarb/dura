#!/usr/bin/env python3
"""
Chart for poc_network_challenge.py's `stats` mode: at what session size k
does averaging repeated possession challenges reliably separate an honest
local holder from a relay faking possession over a second real network hop?

Re-runs the real measurement (same subprocess holder/relay, same protocol)
rather than plotting a hardcoded copy of an earlier run's numbers.
"""
import os
import sys

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import poc_network_challenge as pnc

BLUE = '#2a78d6'    # categorical slot 1 — honest holder
ORANGE = '#eb6834'  # categorical slot 2 — relay
SURFACE = '#fcfcfb'
TEXT_PRIMARY = '#0b0b0b'
TEXT_SECONDARY = '#52514e'
GRID = '#e5e4e0'

K_VALUES = (1, 2, 3, 5, 8, 12, 20, 30)


def measure():
    holder_port, relay_port = 8901, 8902
    expected_chunks = pnc.build_chunks()
    n_samples = 80

    import subprocess, time, random
    holder_proc = subprocess.Popen([sys.executable, pnc.__file__, 'holder', str(holder_port)])
    relay_proc = subprocess.Popen(
        [sys.executable, pnc.__file__, 'relay', str(relay_port), '127.0.0.1', str(holder_port)])
    try:
        pnc.wait_for_port('127.0.0.1', holder_port)
        pnc.wait_for_port('127.0.0.1', relay_port)
        time.sleep(0.2)
        holder_times, relay_times = [], []
        for i in range(n_samples):
            idx = random.Random(3000 + i).randrange(pnc.N_CHUNKS)
            t, _ = pnc.challenge('127.0.0.1', holder_port, idx, expected_chunks, 'holder', verbose=False)
            holder_times.append(t)
            t, _ = pnc.challenge('127.0.0.1', relay_port, idx, expected_chunks, 'relay', verbose=False)
            relay_times.append(t)
    finally:
        holder_proc.terminate(); relay_proc.terminate()
        holder_proc.wait(); relay_proc.wait()
    return holder_times, relay_times


def main():
    print("measuring fresh real samples for the chart...")
    holder_times, relay_times = measure()

    h_worst = [pnc.session_mean_bounds(holder_times, k)[1] for k in K_VALUES]
    r_best = [pnc.session_mean_bounds(relay_times, k)[0] for k in K_VALUES]
    crossover_k = next((k for k, hw, rb in zip(K_VALUES, h_worst, r_best) if hw < rb), None)

    fig, ax = plt.subplots(figsize=(9, 5.5), dpi=160)
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)

    ax.plot(K_VALUES, h_worst, color=BLUE, linewidth=2, marker='o', markersize=5,
            label='honest holder — worst-case session mean')
    ax.plot(K_VALUES, r_best, color=ORANGE, linewidth=2, marker='o', markersize=5,
            label='relay (fetches over real 2nd hop) — best-case session mean')

    if crossover_k:
        ax.axvline(crossover_k, color=TEXT_SECONDARY, linewidth=1, linestyle=(0, (3, 3)))
        ax.annotate(f'separates at k={crossover_k}',
                    xy=(crossover_k, 0.06), xycoords=ax.get_xaxis_transform(),
                    xytext=(8, 0), textcoords='offset points',
                    color=TEXT_SECONDARY, fontsize=9, va='bottom', ha='left')

    ax.set_xscale('log')
    ax.xaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: str(int(v)) if v in K_VALUES else ''))
    ax.set_xticks(K_VALUES)
    ax.set_xlabel('session size k (repeated challenges averaged)', color=TEXT_SECONDARY, fontsize=10)
    ax.set_ylabel('round-trip time, ms', color=TEXT_SECONDARY, fontsize=10)
    ax.set_title('Repeated challenges separate honest holder from relay — single-shot doesn\'t',
                 color=TEXT_PRIMARY, fontsize=13, pad=14, loc='left')

    ax.grid(True, color=GRID, linewidth=0.75)
    ax.set_axisbelow(True)
    for spine in ('top', 'right'):
        ax.spines[spine].set_visible(False)
    for spine in ('left', 'bottom'):
        ax.spines[spine].set_color(GRID)

    ax.tick_params(colors=TEXT_SECONDARY, labelsize=9)
    legend = ax.legend(loc='upper right', frameon=False, fontsize=9, labelcolor=TEXT_PRIMARY)

    fig.text(0.01, 0.01,
              f'real measured data, loopback TCP, n=80 samples per role — not modeled',
              fontsize=8, color=TEXT_SECONDARY)

    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'poc_challenge_separation.png')
    fig.tight_layout(rect=[0, 0.02, 1, 1])
    fig.savefig(out_path, facecolor=SURFACE)
    print(f"saved {out_path}")
    print(f"crossover at k={crossover_k}")


if __name__ == '__main__':
    main()
