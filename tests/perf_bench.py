#!/usr/bin/env python3
"""
Performance benchmarks for the _RuleIndex optimization.

Verifies the three complexity claims from DESIGN.md § Rule Indexing:

  Benchmark 1 — O(1) exact-host candidate lookup
    candidate_indices(host) does one dict lookup; calling rule.match.matches(f)
    for every rule is O(R). Shows that indexed lookup time is flat with R
    while naive match-all grows linearly.

  Benchmark 2 — O(L) HostGlob via merged regex
    One compiled regex alternation over G glob patterns is O(L) in hostname
    length. Naive: G separate fnmatch.fnmatch() calls are O(G·L).
    Compares stdlib re (NFA) vs google-re2 (true DFA) vs naive fnmatch.

  Benchmark 3 — Index build O(R) vs candidate lookup O(1)
    Index construction grows with R; candidate_indices() stays flat.
    Shows the one-time O(R) build cost is separate from per-URL O(1) lookup.

  Benchmark 4 — Pipeline Furl cost: O(candidates) not O(R)
    Moving Furl(url) inside the candidate guard reduces instantiation cost
    from O(R) to O(candidates) per canonicalize() call.

Assertions are ratio-based (machine-independent):
  "flat" means max/min < 5×  across all data points
  "grows" means last/first > 10% of the R or G growth ratio

Run:
    cd canonicalizing-urls
    uv run --group dev python tests/perf_bench.py
    # or via pytest:
    uv run --group dev pytest tests/perf_bench.py -v -s
"""
from __future__ import annotations

import fnmatch
import os
import sys
import time

# Allow direct execution (pytest uses pyproject.toml pythonpath config)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import re

try:
    import re2
    HAS_RE2 = True
except ImportError:
    HAS_RE2 = False

from engine import AnyHost, Host, HostGlob, Rule, StripParams, _RuleIndex, _glob_to_inner_regex, canonicalize
from furl import furl as Furl

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

UNIVERSAL_RULE = Rule(match=AnyHost(), actions=[StripParams(params=["utm_source"])])
TEST_HOST = "www.youtube.com"
TEST_URL = f"https://{TEST_HOST}/watch?v=abc&utm_source=share"
TEST_FURL = Furl(TEST_URL)


def exact_rules(n: int) -> list[Rule]:
    """N Host(...) rules with hosts that never equal TEST_HOST."""
    return [
        Rule(match=Host(f"host{i}.example.com"), actions=[StripParams(params=[f"p{i}"])])
        for i in range(n)
    ]


def glob_rules(n: int) -> list[Rule]:
    """N HostGlob(...) rules that never match TEST_HOST."""
    return [
        Rule(match=HostGlob(f"x{i}.*.net"), actions=[StripParams(params=[f"g{i}"])])
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# Timing helper
# ---------------------------------------------------------------------------

def measure_us(fn, *, warmup: int = 20, n: int = 2000) -> float:
    """Return mean call time in microseconds."""
    for _ in range(warmup):
        fn()
    t0 = time.perf_counter()
    for _ in range(n):
        fn()
    return (time.perf_counter() - t0) * 1e6 / n


def _header(title: str) -> None:
    print(f"\n{'═' * 68}")
    print(f"  {title}")
    print(f"{'═' * 68}")


def _assert_ratio(label: str, actual: float, op: str, threshold: float) -> None:
    """Print and assert a ratio check."""
    ops = {"<": actual < threshold, ">": actual > threshold}
    passed = ops[op]
    mark = "✓" if passed else "✗"
    print(f"    {mark}  {label}: {actual:.1f}× {op} {threshold:.0f}×")
    assert passed, f"{label}: {actual:.1f}× not {op} {threshold:.0f}×"


# ---------------------------------------------------------------------------
# Benchmark 1: candidate_indices() is O(1) w.r.t. R (exact-host rules)
# ---------------------------------------------------------------------------

def test_bench1_exact_host_lookup(capsys=None) -> None:
    """O(1) dict lookup — indexed time flat as non-matching exact rules grow."""
    _header("Benchmark 1: exact-host lookup  (claim: O(1) vs O(R) scan)")
    print(f"  Task: find which of R rules can match host='{TEST_HOST}'")
    print(f"  All R rules target 'hostN.example.com' (non-matching).\n")
    print(f"  {'R':>5}  {'index lookup µs':>16}  {'naive .matches() µs':>20}  {'speedup':>8}")
    print(f"  {'─'*5}  {'─'*16}  {'─'*20}  {'─'*8}")

    r_values = [10, 100, 1000, 5000]
    t_indices, t_naives = [], []

    for r in r_values:
        rules = [UNIVERSAL_RULE] + exact_rules(r)
        index = _RuleIndex(rules)
        f = TEST_FURL

        def run_indexed(index=index, host=TEST_HOST):
            index.candidate_indices(host)

        def run_naive(rules=rules, f=f):
            for rule in rules:
                rule.match.matches(f)

        t_idx = measure_us(run_indexed, n=5000)
        t_naive = measure_us(run_naive, n=2000)
        t_indices.append(t_idx)
        t_naives.append(t_naive)

        print(f"  {r:>5}  {t_idx:>16.3f}  {t_naive:>20.3f}  {t_naive/t_idx:>7.1f}×")

    r_growth = r_values[-1] / r_values[0]  # 500×
    print(f"\n  Assertions (machine-independent ratios):")
    _assert_ratio("index lookup flat (max/min)", max(t_indices) / min(t_indices), "<", 5)
    _assert_ratio(f"naive grows with R (last/first, expect ~{r_growth:.0f}×)", t_naives[-1] / t_naives[0], ">", r_growth * 0.1)


# ---------------------------------------------------------------------------
# Benchmark 2: HostGlob merged regex — stdlib re (NFA) vs re2 (DFA) vs fnmatch
# ---------------------------------------------------------------------------

def _build_merged_regex(patterns: list[str], module) -> re.Pattern:
    """Compile a merged alternation of glob patterns using the given re module."""
    src = "(?i)" + "|".join(
        f"(?P<g{j}>{_glob_to_inner_regex(p)})"
        for j, p in enumerate(patterns)
    )
    return module.compile(src)


def test_bench2_hostglob_merged_regex(capsys=None) -> None:
    """stdlib re (NFA) and google-re2 (DFA) vs naive fnmatch — all three vs G."""
    _header("Benchmark 2: HostGlob merged regex  (claim: O(L) vs O(G·L))")
    host = "m.example.com"  # matches none — full scan, worst case
    print(f"  hostname: '{host}'  (no match — worst-case full scan)")
    print(f"  re   = stdlib re, NFA backend  (always available)")
    print(f"  re2  = google-re2, true DFA    ({'installed' if HAS_RE2 else 'NOT installed — run: uv sync --group perf'})")
    print(f"  fnmatch = G separate fnmatch.fnmatch() calls  O(G·L)\n")

    re2_col = f"{'re2 µs':>10}" if HAS_RE2 else f"{'re2 µs':>10} (N/A)"
    print(f"  {'G':>5}  {'re µs':>8}  {re2_col}  {'fnmatch µs':>11}  {'re2/re':>7}  {'fnmatch/re':>11}")
    print(f"  {'─'*5}  {'─'*8}  {'─'*10}  {'─'*11}  {'─'*7}  {'─'*11}")

    g_values = [5, 20, 50, 100]
    t_res, t_re2s, t_naives = [], [], []

    for g in g_values:
        patterns = [f"x{i}.*.net" for i in range(g)]

        re_pat = _build_merged_regex(patterns, re)
        re2_pat = _build_merged_regex(patterns, re2) if HAS_RE2 else None

        def run_re(pat=re_pat, host=host):
            pat.fullmatch(host)

        def run_re2(pat=re2_pat, host=host):
            pat.fullmatch(host)

        def run_naive(patterns=patterns, host=host):
            for p in patterns:
                fnmatch.fnmatch(host, p)

        t_re = measure_us(run_re, n=10000)
        t_re2 = measure_us(run_re2, n=10000) if HAS_RE2 else None
        t_naive = measure_us(run_naive, n=10000)
        t_res.append(t_re)
        if t_re2 is not None:
            t_re2s.append(t_re2)
        t_naives.append(t_naive)

        re2_str = f"{t_re2:>10.3f}" if t_re2 is not None else f"{'N/A':>10}"
        re2_ratio = f"{t_re2/t_re:>7.2f}×" if t_re2 is not None else f"{'N/A':>8}"
        print(
            f"  {g:>5}  {t_re:>8.3f}  {re2_str}  {t_naive:>11.3f}  {re2_ratio}  {t_naive/t_re:>10.1f}×"
        )

    g_growth = g_values[-1] / g_values[0]  # 20×
    print(f"\n  Assertions (machine-independent ratios):")
    _assert_ratio(f"fnmatch grows with G (last/first, expect ~{g_growth:.0f}×)", t_naives[-1] / t_naives[0], ">", g_growth * 0.25)
    if t_re2s:
        re2_growth = t_re2s[-1] / t_re2s[0]
        fnmatch_growth = t_naives[-1] / t_naives[0]
        _assert_ratio("re2 grows less than fnmatch/3 (DFA vs O(G·L))", re2_growth, "<", fnmatch_growth / 3)


# ---------------------------------------------------------------------------
# Benchmark 3: Index build O(R) separate from candidate lookup O(1)
# ---------------------------------------------------------------------------

def test_bench3_build_vs_lookup(capsys=None) -> None:
    """Index build grows with R; candidate lookup stays flat."""
    _header("Benchmark 3: index build vs lookup  (claim: build O(R), lookup O(1))")
    print(f"  Index build: _RuleIndex(rules) — one-time cost, O(R)")
    print(f"  Lookup: index.candidate_indices(host) — per-URL cost, O(1)\n")
    print(f"  {'R':>5}  {'build µs':>10}  {'lookup µs':>10}  {'build/lookup ratio':>19}")
    print(f"  {'─'*5}  {'─'*10}  {'─'*10}  {'─'*19}")

    r_values = [50, 200, 1000, 5000]
    t_builds, t_lookups = [], []

    for r in r_values:
        rules = [UNIVERSAL_RULE] + exact_rules(r)

        def run_build(rules=rules):
            _RuleIndex(rules)

        index = _RuleIndex(rules)

        def run_lookup(index=index, host=TEST_HOST):
            index.candidate_indices(host)

        t_build = measure_us(run_build, warmup=5, n=200)
        t_lookup = measure_us(run_lookup, warmup=50, n=10000)
        t_builds.append(t_build)
        t_lookups.append(t_lookup)

        print(f"  {r:>5}  {t_build:>10.1f}  {t_lookup:>10.3f}  {t_build/t_lookup:>19.0f}×")

    r_growth = r_values[-1] / r_values[0]  # 100×
    print(f"\n  Assertions (machine-independent ratios):")
    _assert_ratio(f"build grows with R (last/first, expect ~{r_growth:.0f}×)", t_builds[-1] / t_builds[0], ">", r_growth * 0.1)
    _assert_ratio("lookup flat (max/min)", max(t_lookups) / min(t_lookups), "<", 5)


# ---------------------------------------------------------------------------
# Benchmark 4: Furl-per-candidate vs Furl-per-rule (pipeline fix)
# ---------------------------------------------------------------------------

def test_bench4_furl_instantiation(capsys=None) -> None:
    """Furl moved inside candidate guard: O(candidates) instead of O(R)."""
    _header("Benchmark 4: pipeline Furl cost  (fix: O(candidates) not O(R))")
    print(f"  Simulates the canonicalize() inner loop with R non-matching rules.")
    print(f"  old: Furl(url) on every iteration  →  O(R) instantiations (~130µs each)")
    print(f"  new: Furl(url) only for candidates  →  1 Furl for the universal rule\n")
    print(f"  {'R':>5}  {'new µs':>10}  {'old µs':>10}  {'speedup':>8}")
    print(f"  {'─'*5}  {'─'*10}  {'─'*10}  {'─'*8}")

    # Furl() costs ~130µs each. Target ~0.5s per old measurement.
    FURL_COST_US = 130

    r_values = [10, 50, 200, 500]
    t_news, t_olds = [], []

    for r in r_values:
        rules = [UNIVERSAL_RULE] + exact_rules(r)
        index = _RuleIndex(rules)
        n_old = max(10, 500_000 // (r * FURL_COST_US))

        def run_new(rules=rules, index=index):
            canonicalize(TEST_URL, rules, _index=index)

        def run_old(rules=rules, index=index):
            prev_host = None
            candidates: frozenset[int] = frozenset()
            url = TEST_URL
            for i, rule in enumerate(rules):
                f = Furl(url)           # O(R) — the old behaviour
                if f.host != prev_host:
                    candidates = index.candidate_indices(f.host)
                    prev_host = f.host
                if i not in candidates:
                    continue
                if not rule.match.matches(f):
                    continue
                for action in rule.actions:
                    new_url = action.apply(f)
                    url = new_url if new_url is not None else f.url

        t_new = measure_us(run_new, warmup=20, n=200)
        t_old = measure_us(run_old, warmup=max(2, n_old // 5), n=n_old)
        t_news.append(t_new)
        t_olds.append(t_old)

        print(f"  {r:>5}  {t_new:>10.1f}  {t_old:>10.1f}  {t_old/t_new:>7.1f}×")

    r_growth = r_values[-1] / r_values[0]  # 50×
    print(f"\n  Assertions (machine-independent ratios):")
    _assert_ratio("new pipeline flat (max/min)", max(t_news) / min(t_news), "<", 5)
    _assert_ratio(f"old grows with R (last/first, expect ~{r_growth:.0f}×)", t_olds[-1] / t_olds[0], ">", r_growth * 0.1)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("_RuleIndex complexity benchmarks")
    print("(DESIGN.md § Rule Indexing — three O(·) claims + pipeline fix)")
    test_bench1_exact_host_lookup()
    test_bench2_hostglob_merged_regex()
    test_bench3_build_vs_lookup()
    test_bench4_furl_instantiation()
    print(f"\n{'═' * 68}")
    print("  Done.")
    print(f"{'═' * 68}\n")
