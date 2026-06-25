# Benchmark: Rule Index Performance (Rust)

Verifies the complexity claims in [DESIGN.md § Rule Indexing](DESIGN.md#rule-indexing)
using two complementary mechanisms:

- **`tests/perf_ratios.rs`** — machine-independent *ratio* assertions that run
  under `cargo test` and **fail the build on regression** (e.g. if an O(1)
  lookup silently becomes O(R)).
- **`benches/rule_index.rs`** — `criterion` statistical benchmarks producing
  human-readable reports under `target/criterion/` and the numbers below.

---

## Goals

1. **O(1) exact-host lookup** — `candidate_indices(host)` time is flat as the
   number of non-matching `Host(...)` rules grows.
2. **O(L) HostGlob matching via `RegexSet`** — all `HostGlob` patterns merged
   into one multi-pattern DFA; lookup grows sub-linearly in the number of glob
   rules G (a single automaton pass, not G separate matches).
3. **Index build is O(R), separate from lookup O(1)** — one-time `RuleIndex::new`
   grows with R while `candidate_indices()` stays flat.
4. **Pipeline parses only for candidate rules** — `canonicalize()` constructs
   the working `Url` only when a rule is a candidate, not once per rule.

---

## The DFA merge: `RegexSet`

The Python version merged all `HostGlob` patterns into one regex *alternation*
with named capture groups, then read back `m.groupdict()` to learn which glob
matched — a workaround because stdlib `re` has no multi-pattern primitive.

Rust's `regex` crate provides [`RegexSet`](https://docs.rs/regex/latest/regex/struct.RegexSet.html)
natively: all patterns compile into **one combined automaton**, and
`set.matches(host)` runs the host through it once, returning the indices of
every matching pattern. This is the honest expression of "merge all globs into
one DFA":

- linear time in hostname length L, independent of the number of globs G;
- no backtracking, no catastrophic blowup (guaranteed by the engine, not a flag);
- no optional `google-re2` backend to install — the standard crate already is a
  finite-automaton engine.

`RuleIndex` keeps a parallel `glob_rule_ids: Vec<usize>` mapping each set-match
index back to its rule index — replacing Python's `group_to_rule` string map.

---

## Results

Representative `cargo bench` run (Apple M-series, Rust 1.96, `--release`).
criterion medians.

![Overview of all four benchmarks](figures/bench_overview.png)

> Figures: `cargo run --example gen_figures --features figures`. Axes are log–log.

### Benchmark 1 — exact-host lookup (O(1) vs O(R))

![Benchmark 1](figures/bench1.png)

```
    R   index lookup   naive .matches()   speedup
─────  ─────────────  ─────────────────  ────────
   10        45.4 ns           26.3 ns      0.6×
  100        44.7 ns          237.8 ns      5.3×
 1000        45.7 ns          2.321 µs     50.8×
 5000        45.4 ns         11.682 µs    257.3×
```

Index lookup is flat (~45 ns) regardless of R; the naive per-rule scan grows
linearly to 11.7 µs at R=5000. (At R=10 the naive scan is faster — the index
has a small fixed cost that only pays off once R exceeds ~20.)

### Benchmark 2 — HostGlob `RegexSet` (O(L), ~flat in G)

![Benchmark 2](figures/bench2.png)

```
    G   RegexSet candidate lookup
─────  ─────────────────────────
    5        60.4 ns
   20        69.4 ns
   50        81.1 ns
  100       103.4 ns
```

Merging 5 → 100 glob patterns into one `RegexSet` grows the lookup only ~1.7×
(60 → 103 ns) — sub-linear in G, because the host runs through a single
combined automaton once rather than being tested against each pattern.

### Benchmark 3 — index build O(R) vs lookup O(1)

![Benchmark 3](figures/bench3.png)

```
    R     build      lookup
─────  ─────────  ─────────
   50    6.25 µs    45.0 ns
  200   25.67 µs    46.0 ns
 1000  145.83 µs    44.7 ns
 5000  687.49 µs    45.0 ns
```

Build grows ~110× as R grows 100× (linear); lookup stays flat at ~45 ns.
Because the index is built once per top-level `canonicalize()` call (and reused
across `FollowRedirect` recursion), this O(R) cost is paid once, not per hop.
For the real rule list (~16 rules), build is a few µs — negligible.

### Benchmark 4 — pipeline `canonicalize()` over R rules

![Benchmark 4](figures/bench4.png)

```
    R   canonicalize
─────  ─────────────
   10       2.35 µs
   50       7.47 µs
  200      27.50 µs
  500      76.12 µs
```

This grows with R because each rule still costs a `candidate_indices` set build
plus a membership check. The optimization (parse the working URL only for
*candidate* rules) still holds; for the real ~16-rule list a full canonicalize
is ~10 µs (see below).

---

## Head-to-head: Python vs Rust

Same algorithm, same rules, measured **in-process** (the engine function, not CLI
startup) on identical inputs, warm. Python via `time.perf_counter` loops; Rust
via criterion. Apple M-series.

| Operation | Python | Rust | Speedup |
|-----------|-------:|-----:|--------:|
| `candidate_indices` (R=5000 exact rules) | 0.18 µs | 0.045 µs | **4×** |
| naive match-all scan (R=5000) | 266 µs | 11.7 µs | 23× |
| index build (R=5000) | 1678 µs | 687 µs | 2.4× |
| **`canonicalize()` real RULES, 1 URL** | **294 µs** | **10.5 µs** | **~28×** |

The real-world figure is the one that matters: a full canonicalize over the
built-in rule list is **~28× faster** in Rust. Both implementations preserve the
same O(1)-lookup / O(R)-build complexity; Rust wins on constant factors
(no interpreter, no `furl` per-parse, compiled regexes shared warm).

> **A note on how this number was earned.** The first Rust cut was accidentally
> *slower* than Python (431 µs) — it recompiled regexes per call and, worse,
> returned **cloned** `Regex` handles whose internal DFA cache is cold on every
> clone, so each `.find()` rebuilt it (~tens of µs). The fix: memoize compiled
> regexes and share them by `Arc` (warm cache preserved). `tests/perf_ratios.rs`
> now guards this permanently — `regex_compiled_once_not_per_call` fails the build
> if regex-using rules drift more than 8× slower than params-only rules. The
> head-to-head benchmark is what surfaced the regression; without it, the port
> would have shipped slower than the Python it replaced.

---

## Reproduce

```bash
cargo bench                              # criterion reports → target/criterion/
cargo test --test perf_ratios -- --nocapture   # ratio assertions (CI gate)
cargo run --example gen_figures --features figures # regenerate figures/
```
