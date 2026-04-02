#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["matplotlib"]
# ///
"""
Generate benchmark figures for BENCHMARK.md.

Usage (no setup required):
    cd canonicalizing-urls
    uv run scripts/gen_figures.py

Saves bench1.png … bench4.png + bench_overview.png to figures/.

Data comes from a representative run on Apple M-series / Python 3.13 / re2
installed. Re-run tests/perf_bench.py on your machine to get updated numbers,
then edit the constants below.
"""
from __future__ import annotations

import matplotlib
matplotlib.use("Agg")  # headless — no display needed

import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
from pathlib import Path

# ---------------------------------------------------------------------------
# Representative measurements
# Source: uv run --group dev --group perf python tests/perf_bench.py
# Machine: Apple M-series, Python 3.13, google-re2 installed
# ---------------------------------------------------------------------------

B1_R       = [10,    100,     1_000,    5_000]
B1_INDEXED = [0.317, 0.443,   0.453,    0.405]     # µs — flat O(1)
B1_NAIVE   = [1.223, 10.640,  108.146,  543.770]   # µs — linear O(R)

B2_G       = [5,     20,      50,       100]
B2_RE      = [0.264, 0.596,   2.138,    6.656]     # µs — NFA, grows
B2_RE2     = [2.682, 3.426,   4.023,    5.794]     # µs — DFA, near-flat
B2_FNMATCH = [2.680, 9.951,   26.072,   54.361]    # µs — linear O(G)

B3_R       = [50,    200,     1_000,    5_000]
B3_BUILD   = [35.2,  142.2,   747.0,    4_135.5]   # µs — linear O(R)
B3_LOOKUP  = [0.417, 0.403,   0.449,    0.409]     # µs — flat O(1)

B4_R       = [10,    50,      200,      500]
B4_NEW     = [302.7, 270.1,   301.0,    391.1]     # µs — flat O(candidates)
B4_OLD     = [1_330, 5_929,   23_397,   61_604]    # µs — linear O(R)

OUT_DIR = Path(__file__).parent.parent / "figures"

# ---------------------------------------------------------------------------
# Style constants
# ---------------------------------------------------------------------------

C_FLAT   = "#2563eb"  # blue  — optimized / flat path
C_LINEAR = "#dc2626"  # red   — naive / grows path
C_DFA    = "#16a34a"  # green — re2 DFA
C_EXTRA  = "#9333ea"  # purple — fnmatch
C_REF    = "#9ca3af"  # gray  — reference slope line

FLAT_KW   = dict(linewidth=2, marker="o", markersize=7, color=C_FLAT)
LINEAR_KW = dict(linewidth=2, marker="s", markersize=7, color=C_LINEAR)
DFA_KW    = dict(linewidth=2, marker="^", markersize=7, color=C_DFA)
EXTRA_KW  = dict(linewidth=2, marker="D", markersize=6, color=C_EXTRA)
REF_KW    = dict(linewidth=1, linestyle="--", color=C_REF)


def _slope(xs: list, y0: float, exponent: float) -> list:
    """Reference line: y = y0 * (x / xs[0]) ** exponent."""
    return [y0 * (x / xs[0]) ** exponent for x in xs]


def _apply(ax: plt.Axes, xs: list, title: str, xlabel: str, ylabel: str) -> None:
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xticks(xs)
    ax.xaxis.set_major_formatter(ticker.ScalarFormatter())
    ax.set_title(title, fontsize=10, fontweight="bold", pad=8)
    ax.set_xlabel(xlabel, fontsize=9)
    ax.set_ylabel(ylabel, fontsize=9)
    ax.grid(True, which="both", alpha=0.2, linestyle="--")
    ax.tick_params(labelsize=8)
    ax.legend(fontsize=8, framealpha=0.9)


# ---------------------------------------------------------------------------
# Individual figures
# ---------------------------------------------------------------------------

def _draw_bench1(ax: plt.Axes) -> None:
    ax.plot(B1_R, B1_INDEXED, label="indexed  O(1)",    **FLAT_KW)
    ax.plot(B1_R, B1_NAIVE,   label="naive scan  O(R)", **LINEAR_KW)
    ax.plot(B1_R, _slope(B1_R, B1_NAIVE[0], 1), label="O(R) slope", **REF_KW)
    _apply(ax, B1_R,
           "B1 — exact-host lookup: O(1) vs O(R) scan",
           "R  (non-matching rules)", "µs per call")


def _draw_bench2(ax: plt.Axes) -> None:
    ax.plot(B2_G, B2_RE,      label="stdlib re  (NFA)",     **LINEAR_KW)
    ax.plot(B2_G, B2_RE2,     label="google-re2  (DFA)",    **DFA_KW)
    ax.plot(B2_G, B2_FNMATCH, label="naive fnmatch  O(G·L)", **EXTRA_KW)
    ax.plot(B2_G, _slope(B2_G, B2_FNMATCH[0], 1), label="O(G) slope", **REF_KW)
    _apply(ax, B2_G,
           "B2 — HostGlob: merged regex vs fnmatch  O(G·L)",
           "G  (glob patterns)", "µs per call")


def _draw_bench3(ax: plt.Axes) -> None:
    ax.plot(B3_R, B3_BUILD,  label="index build  O(R)",      **LINEAR_KW)
    ax.plot(B3_R, B3_LOOKUP, label="candidate lookup  O(1)", **FLAT_KW)
    ax.plot(B3_R, _slope(B3_R, B3_BUILD[0], 1), label="O(R) slope", **REF_KW)
    _apply(ax, B3_R,
           "B3 — index build O(R) vs lookup O(1)",
           "R  (rules)", "µs per call")


def _draw_bench4(ax: plt.Axes) -> None:
    ax.plot(B4_R, B4_NEW, label="new  O(candidates)",  **FLAT_KW)
    ax.plot(B4_R, B4_OLD, label="old  O(R) Furl loop", **LINEAR_KW)
    ax.plot(B4_R, _slope(B4_R, B4_OLD[0], 1), label="O(R) slope", **REF_KW)
    _apply(ax, B4_R,
           "B4 — pipeline Furl cost: before/after fix",
           "R  (non-matching rules)", "µs per call")


BENCHMARKS = [
    ("bench1.png", _draw_bench1),
    ("bench2.png", _draw_bench2),
    ("bench3.png", _draw_bench3),
    ("bench4.png", _draw_bench4),
]


def main() -> None:
    OUT_DIR.mkdir(exist_ok=True)

    # Individual figures
    for filename, draw_fn in BENCHMARKS:
        fig, ax = plt.subplots(figsize=(6, 4))
        draw_fn(ax)
        fig.tight_layout()
        path = OUT_DIR / filename
        fig.savefig(path, dpi=150, bbox_inches="tight")
        plt.close(fig)
        print(f"  wrote {path}")

    # Overview: 2×2 grid
    fig, axes = plt.subplots(2, 2, figsize=(12, 8))
    fig.suptitle("_RuleIndex complexity benchmarks  (log–log scale)",
                 fontsize=12, fontweight="bold", y=1.01)
    for ax, (_, draw_fn) in zip(axes.flat, BENCHMARKS):
        draw_fn(ax)
    fig.tight_layout()
    path = OUT_DIR / "bench_overview.png"
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  wrote {path}")

    print("Done.")


if __name__ == "__main__":
    main()
