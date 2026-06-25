# Design: canonicalizing-urls

## Overview

An agent skill that canonicalizes URLs — stripping tracking params,
unwrapping redirects, normalizing hosts, extracting canonical paths, and
resolving opaque short-links via HTTP.

Originally written in Python, now implemented in **Rust** (see migration below).

---

## Migration: Python → Rust

The Rust port preserves the algorithm, rule DSL, and complexity design; it
changes only the implementation substrate. Each Python construct maps as:

| Concern | Python (original) | Rust (current) | Why |
|---------|-------------------|----------------|-----|
| URL model | `furl` (mutable, order-preserving) | `url` crate + ordered-query wrapper (`url_model`) | `url::Url` doesn't preserve query order on mutation; the wrapper restores furl's behavior |
| Match/action types | `@dataclass` + duck typing | `enum` ADTs + `match` | compiler-checked exhaustiveness |
| Rule DSL | `Rule(match=Host("x") & Path("/y"), actions=[…])` | `rule(Host("x") & Path("/y"), vec![…])` | constructor fns + `impl BitAnd` keep the look |
| HostGlob merge | `fnmatch.translate` → alternation + named groups; optional `google-re2` | one `regex::RegexSet` (native multi-pattern DFA) | linear-time, no optional backend, drops the group→rule map |
| Path regex syntax | `\1`, `\2` (Python `re.sub`) | `$1`, `$2` (Rust `regex`) | the one unavoidable DSL difference |
| HTTP | `httpx` | `reqwest` (blocking) | — |
| HTML (probe) | `beautifulsoup4` | `tl` | lighter; probe only reads 3 meta tags |
| CLI | `click` + PEP 723 / `uv run` | `clap` + prebuilt binary | no runtime toolchain for users |
| Network test seam | `unittest.mock.patch("engine._http_resolve")` | resolver injected as a parameter (DI) | explicit seam; offline tests panic if the net is touched |

**Performance:** same O(1)-lookup / O(R)-build complexity; ~28× faster on the
real rule list in absolute terms. See [BENCHMARK.md](BENCHMARK.md) for the
head-to-head, including the per-call-regex-compilation regression the port
hit and fixed.

---

## File Structure

```
skill/
  SKILL.md           ← triggering conditions + Claude workflows; step-0 install guard
  scripts/
    install.sh       ← download+verify per-platform release archive → bin/ (fallback: cargo build)
  bin/               ← git-ignored; canonicalize binary fetched/built on first use
src/
  lib.rs             ← crate root (functional core)
  url_model.rs       ← order-preserving URL wrapper (replaces furl)
  engine.rs          ← primitives, pipeline, RuleIndex, validate_rules
  rules.rs           ← rules() list (the one file that grows)
  main.rs            ← clap CLI (imperative shell)
  probe.rs           ← differential HTTP probe (reqwest + tl)
examples/
  gen_figures.rs     ← plotters; dev-only, regenerates figures/ (not a [[bin]], so dist skips it)
tests/
  uat.rs             ← pipeline e2e: BEFORE→AFTER acceptance table
  builtin_rules.rs   ← built-in RULES integration tests
  cli.rs             ← process e2e: spawns the binary, asserts stdout/exit contract
  perf_ratios.rs     ← complexity ratio assertions (CI regression gate)
benches/
  rule_index.rs      ← criterion statistical benchmarks
dist-workspace.toml  ← `dist` release config (4 targets, archives + checksums)
figures/
  bench1.png … bench4.png   ← individual benchmark charts
  bench_overview.png         ← 2×2 overview of all four benchmarks
BENCHMARK.md       ← benchmark goals, design, results, and insights
```

**Separation of concerns (Functional Core / Imperative Shell):**

| File | Role | Changes when |
|------|------|-------------|
| `engine.rs` | Rule language + execution (pure) | Adding new primitive types |
| `url_model.rs` | URL parsing/mutation (pure) | URL representation changes |
| `rules.rs` | Domain-specific rules | Adding support for a new site |
| `main.rs` | CLI + network shell | Changing CLI flags |
| `probe.rs` | Rule-discovery shell | Probe heuristics change |
| `benches/`, `examples/gen_figures.rs` | Complexity verification & charts | Benchmarking changes |

The engine and URL model are a **pure functional core** (no I/O); `main.rs` and
`probe.rs` are the **imperative shell** (network). The pipeline takes the HTTP
resolver as a parameter (dependency injection), so the core is testable without
mocks and the network seam is explicit.

### Action ADTs (type-safe control flow)

Match conditions and actions are Rust `enum`s (`Matcher`, `Action`). The
`apply()` function `match`es over `Action`, so the compiler enforces
exhaustiveness — adding a variant without handling it fails to build. The DSL is
preserved via constructor functions (`Host("x")`, `strip_params(&[…])`) plus an
`impl BitAnd for Matcher`, so `Host("x") & Path("/y")` reads like the original
Python. `RewritePath` replacements use Rust regex syntax (`$1`, `$2`).

---

## Rule Language

Rules are declarative Rust values built via constructor functions. Each `Rule`
has a `matcher` condition and an `actions` list. Rules run top-to-bottom;
**all matching rules apply** (not first-match-only).

```rust
rule(
    Host("www.linkedin.com") & Path("/learning-login/share"),
    vec![
        unwrap_redirect_param("redirect"),
        strip_params(&["account", "forceAccount", "trk", "shareId"]),
    ],
)
```

### Match primitives

| Primitive | Matches |
|-----------|---------|
| `AnyHost()` | Every URL |
| `Host("x.com")` | Exact host |
| `HostGlob("m.*.com")` | Glob host (`fnmatch`) |
| `Path("/foo/*")` | Glob path (`fnmatch`) |
| `A & B` | Both conditions (`_And`) |

### Action primitives

| Action | Network? | Effect |
|--------|----------|--------|
| `StripParams(params=[…])` | no | Remove query params matching patterns (denylist) |
| `KeepParams(params=[…])` | no | Remove all query params EXCEPT those matching patterns (allowlist) |
| `UnwrapRedirectParam("key")` | no | URL-decode redirect param → new URL |
| `RewriteHost("x.com")` | no | Replace domain with a fixed value |
| `RewriteHostPrefix("m.", "www.")` | no | Replace host prefix (e.g. mobile → desktop) |
| `TrimPathSuffix(n=N)` | no | Remove N trailing path segments |
| `ExtractPath(pattern=r"…")` | no | Regex-extract path sub-segment |
| `RewritePath(pattern=r"…", replacement=r"…")` | no | Regex substitution on path (supports capture groups); clears query+fragment if path changed |
| `StripFragment()` | no | Remove `#fragment` |
| `FollowRedirect()` | **yes** | HTTP GET → restart pipeline with final URL |

`StripParams` param syntax:
- Exact: `"forceAccount"` — literal match
- Glob: `"utm_*"` — fnmatch wildcard
- Wildcard all: `"*"` — strip every param
- Regex: `"/^custom_.+/"` — full regex (delimited by `/`)

---

## Pipeline Algorithm

```
canonicalize(url, rules, online, resolve) → String
    index = RuleIndex::new(rules)          # built once; reused across recursion

for each rule i in rules:
    if host changed since last rule:
        candidates = index.candidate_indices(host)   # O(1) pre-filter
    if i not in candidates: continue       # skip without parsing the URL

    f = Url::parse(url)                     # parse only for candidate rules
    if not rule.matcher.matches(f): continue

    for each action in rule.actions:
        if action is FollowRedirect:
            if online:
                resolved = resolve(url)     # injected resolver (DI, not a global)
                return canonicalize(resolved, rules, online, resolve)  # recursive restart
            else:
                break                       # skip rule remainder when offline

        new_url = apply(action, &mut f)
        if new_url is Some:                 # UnwrapRedirectParam returned a new URL
            f = Url::parse(new_url)         # switch context to new URL
            url = new_url                   # don't break — remaining actions continue
        else:
            url = f.to_string()             # action mutated f in place

return url
```

### Key design decisions

**All matching rules run.** This allows a universal tracking-param rule
(`AnyHost`) and a domain-specific rule (`Host("www.linkedin.com")`) to both
fire on the same URL. The result of one rule feeds into the next.

**`UnwrapRedirectParam` does not break the action loop.** After unwrapping,
the remaining actions in the same `Rule.actions` list continue on the
unwrapped URL. This is intentional: a rule like LinkedIn's has
`[UnwrapRedirectParam("redirect"), StripParams([…])]` where the
`StripParams` cleans up params that appear in the redirect *target*.

**`FollowRedirect` restarts the whole pipeline.** It calls `canonicalize()`
recursively with the resolved URL, so all rules (including universal tracking
strippers) apply to the final URL.

**Host-rewriting rules must precede `HostGlob` rules.** After a `RewriteHost`
action fires, `current_host` changes and `candidates` is recomputed for the
next rule. This means a `Host("m.x.com") → RewriteHost("x.com")` rule
placed before the generic `HostGlob("m.*.com")` rule correctly prevents the
glob from also firing — `x.com` is no longer a candidate for `m.*.com`.
`validate_rules()` enforces this automatically: if a specific `Host("x")`
rule with a host-rewriting action appears *after* a `HostGlob` rule that also
rewrites the host and matches `"x"`, a `ValueError` is raised at import time.

---

## Probe Algorithm (`--probe`)

Runs differential HTTP tests against a URL to discover which parts are safe
to remove. Output is a suggested `rule(...)` block ready to paste into
`src/rules.rs`.

```
probe(url):

  base = _fetch_signals(url)   # fetch and extract canonical signals

  1. Params
     strip ALL params → same content? → suggest StripParams(["*"])
     else: test each param individually → collect strippable ones

  2. Host
     starts with "m."? → test rewrite to "www." → same? → suggest RewriteHost

  3. Path
     canonical != original?
       orig_path.endswith(canon_path) → suggest ExtractPath(pattern)
       orig_path.startswith(canon_path) → suggest TrimPathSuffix(n)
```

### "Same content" signals (checked in priority order)

1. `Location:` redirect — definitive
2. `<link rel="canonical">` — strong
3. `<og:url>` meta tag — strong
4. `<title>` tag — moderate

Implemented in `fetch_signals()` (reqwest + tl) and `same_content()`.

---

## StripParams vs KeepParams

Both actions share the same pattern syntax (exact, glob, regex). They are
complementary — use whichever produces a shorter, clearer rule.

| | `StripParams` | `KeepParams` |
|---|---|---|
| Model | Denylist — name the bad params | Allowlist — name the good params |
| Use when | You know the tracking params to remove | You know the content params to keep |
| Scope | Universal or broad rules | Domain-specific rules only |
| Future-proof | No — new tracking params slip through | Yes — new tracking params stripped automatically |

**Convention:** Use `StripParams` in `AnyHost()` rules. Use `KeepParams` only
in `Host("x.com")` rules where you can enumerate all meaningful params.

### Interaction between the two

When both fire on the same URL (across different rules), `KeepParams` always
dominates — it strips everything not in its allowlist, making any earlier
`StripParams` redundant but harmless. This means universal `StripParams` rules
and domain-specific `KeepParams` rules compose cleanly with no ordering concern.

The only destructive case is **two `KeepParams` rules matching the same URL**:
each strips what the other kept, leaving no params. The bootstrap lint check
prevents this.

## Bootstrap Lint Check

`validate_rules(rules)` returns `Result<(), String>` and is exercised by the
`validate_rules_passes_builtin_rules` test (so a bad rule list fails the build).
It reports `Err` on two classes of mistakes:

**1. Conflicting `KeepParams` rules** — two rules that can match the same URL
both contain `KeepParams`. Each strips what the other kept, leaving no params.

*Algorithm:* For each pair of rules that both contain a `KeepParams` action,
extract the host constraint from each matcher (`_hosts_from_matcher`), then
check if those host sets can overlap:

- `AnyHost()` → `{"*"}` (unconstrained — overlaps with everything)
- `Host("x.com")` → `{"x.com"}`
- `HostGlob("m.*.com")` → `{"*"}` (conservative — treated as unconstrained)
- `_And(Host("x.com"), Path("/foo"))` → `{"x.com"}` (Path doesn't constrain host)

This check is **conservative at the host level**: two `KeepParams` rules with
the same host but non-overlapping paths (e.g. `/a/*` vs `/b/*`) are still
flagged. This is intentional — the path-level overlap analysis would be
complex and error-prone, and having two `KeepParams` for the same host almost
always indicates a design mistake anyway.

**2. Eclipsed host-rewriting rules** — a `Host("x")` rule with a
`RewriteHost`/`RewriteHostPrefix` action appears *after* a `HostGlob` rule
that also rewrites the host and whose pattern matches `"x"`. The glob fires
first, changes `current_host`, and the specific rule is silently skipped.

*Algorithm:* For each rule with a specific host (`Host(...)` or `_And(Host(...), ...)`)
that rewrites the host, scan all earlier rules for a `HostGlob` that also
rewrites the host and whose `fnmatch` pattern matches the specific host. If
found, raise `ValueError` naming both rule indices and suggesting the fix.

## Rule Indexing

> Performance verification: [BENCHMARK.md](BENCHMARK.md)

`canonicalize()` builds a `_RuleIndex` on the first call and passes it through
recursive `FollowRedirect` calls to avoid rebuilding.

### Buckets

| Bucket | Matcher type | Per-URL lookup |
|--------|-------------|----------------|
| `universal` | `AnyHost()` | Always included — O(1) |
| `exact` | `Host("x.com")` | Dict lookup by hostname — O(1) |
| `globs` | `HostGlob("m.*.com")` | Single compiled re2 alternation — O(L) |

Unknown matcher types (e.g. `_And(HostGlob, Path)`) fall into `universal`
conservatively — they are always checked, and `rule.match.matches(f)` does
the full filtering.

### Per-rule fast path

At each rule position `i` in the loop, `i not in candidates` short-circuits
before calling `rule.match.matches(f)`. `candidates` is a `frozenset[int]`
recomputed only when `f.host` changes — this is rare but necessary because
`RewriteHostPrefix` can change the host mid-pipeline (e.g. `m.youtube.com` →
`www.youtube.com`), after which `Host("www.youtube.com")` rules must fire.

### HostGlob merge via `RegexSet`

All `HostGlob` patterns are merged into **one** `regex::RegexSet` at index
construction time — the crate's native multi-pattern automaton:

```rust
// glob "m.*.com" → anchored regex "^m\\..*\\.com$", case-insensitive
let glob_set = RegexSet::new(["(?i)^m\\..*\\.com$", "(?i)^.*\\.hashnode\\.dev$"]);
// set.matches(host) runs ONE combined automaton; yields every matching pattern index
```

`RegexSet` compiles all patterns into a single finite-automaton and matches a
host in O(L) (hostname length), independent of the number of glob rules G — no
backtracking, no catastrophic blowup, guaranteed by the engine. A parallel
`glob_rule_ids: Vec<usize>` maps each set-match index back to its rule index
(replacing Python's named-capture `group_to_rule` map). No optional backend to
install — the standard `regex` crate already is a linear-time engine.

### Complexity

| Phase | Before | After |
|-------|--------|-------|
| Index build | — | O(R) once per top-level call |
| Per-rule match check | O(M) × R | O(1) set lookup; O(M) only for candidates |
| HostGlob batch check | O(G × L) | O(L) via DFA |

R = total rules, M = match cost, G = glob rules, L = hostname length.

---

## Adding a New Rule

1. `canonicalize --probe <url>` — review the suggested `rule(...)`
2. Open `src/rules.rs` — add the suggested `rule(...)` after similar-domain rules
   (before any matching `HostGlob` rule if it rewrites the host — `validate_rules` enforces this)
3. `cargo run -- <url>` — verify output
4. `cargo test` — confirm no regressions
5. Add a UAT row to `tests/uat.rs` (BEFORE→AFTER)
6. Commit: `feat: add <domain> canonicalization rule`

**Choosing a path action:** `ExtractPath` is a regex *search* (keeps only the matched substring). `RewritePath` is a regex *substitution* (use capture groups `$1`, `$2`, … to reconstruct the path). Use `RewritePath` when the canonical path is a transformed version of the original (e.g. strip a slug but keep the parent segment and an embedded ID).

---

## Dependencies

| Crate | Used for |
|-------|----------|
| `url` | WHATWG URL parsing; wrapped by `url_model` for order-preserving query mutation |
| `regex` | Param/path matching, glob translation, and the `RegexSet` HostGlob merge (linear-time DFA) |
| `clap` | CLI in `main.rs` |
| `reqwest` (blocking) | HTTP GET with redirect following (`FollowRedirect`, probe) |
| `tl` | Lightweight HTML parsing — `<link rel=canonical>`, `og:url`, `<title>` in probe |
| `criterion` (dev) | Statistical benchmarks |
| `assert_cmd` / `predicates` (dev) | Process-level CLI e2e tests (`tests/cli.rs`) |
| `plotters` (`figures` feature) | Render benchmark charts |

---

## Distribution & Release

End users need **no Rust toolchain** — the skill fetches a prebuilt binary. The
flow has three pieces:

**1. Build & publish (maintainer) — [`dist`](https://opensource.axo.dev/cargo-dist/).**
Release config lives in `dist-workspace.toml` (4 Unix targets, `installers = []`
because the skill calls a fixed-path binary rather than a PATH installer). Pushing
a version tag triggers `.github/workflows/release.yml` (generated by `dist`), which
builds each target and uploads, per target, a `canonicalize-<triple>.tar.xz`
archive plus a `.sha256` checksum to the GitHub Release.

```bash
# cut a release:
git tag v0.1.0 && git push --tags     # → release.yml builds 4 targets, publishes archives + checksums
# regenerate the workflow after editing dist-workspace.toml:
dist generate
```

Only the `canonicalize` binary is shipped; `gen_figures` is an `examples/` target
(not a `[[bin]]`), so `dist` does not package it.

**2. Fetch & verify (first use) — `skill/scripts/install.sh`.**
SKILL.md's step-0 guard runs `install.sh` if `bin/canonicalize` is absent. It maps
`uname` → target triple, downloads the matching `.tar.xz` + `.sha256` over
`--proto '=https' --tlsv1.2`, **verifies the checksum** before trusting the binary,
then extracts it (from the archive's `canonicalize-<triple>/` subdir) into
`skill/bin/`. On an unsupported platform or download failure it falls back to
`cargo build --release` (requires a toolchain).

**3. Invoke (every use).** The agent runs `"$SKILL_DIR/bin/canonicalize" <url>`
per the stdout/exit contract in SKILL.md.

> The `uname → triple` table in `install.sh` must stay in sync with `targets` in
> `dist-workspace.toml`.

---

## Testing

```bash
cargo test
```

| Location | Purpose |
|----------|---------|
| `src/*.rs` `#[cfg(test)]` | Unit tests for engine primitives, URL model, pipeline, probe helpers |
| `tests/uat.rs` | Pipeline e2e: BEFORE→AFTER tables driven through `canonicalize()` against the full `rules()` list |
| `tests/builtin_rules.rs` | Built-in RULES integration tests (inputs distinct from the UAT) |
| `tests/cli.rs` | Process-level e2e: spawns the compiled binary (`assert_cmd`) and asserts the stdout-only / exit-code contract SKILL.md relies on |
| `tests/perf_ratios.rs` | Machine-independent complexity ratio assertions (regression gate) |

`tests/uat.rs` is the living specification for built-in rules. Each table row is
a `(description, before, after)` triple; the description is printed on failure.

Two complementary e2e layers: `uat.rs` proves *what* gets canonicalized (the
pipeline, called directly), while `cli.rs` proves the *binary delivers it
correctly* (argv → exact stdout, empty stderr on success, exit 1 + message on a
bad URL). `cli.rs` covers offline cases only; the `--online` path is exercised at
the library level in `uat.rs` via the injected resolver.

The `FollowRedirect` HTTP path is tested by **dependency injection**: the
pipeline takes the resolver as a parameter, so tests pass a stub closure instead
of monkeypatching. Offline tests inject a resolver that panics if called — so an
accidental network hit fails loudly rather than passing silently.
