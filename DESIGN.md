# Design: canonicalizing-urls

## Overview

A Claude Code skill that canonicalizes URLs — stripping tracking params,
unwrapping redirects, normalizing hosts, extracting canonical paths, and
resolving opaque short-links via HTTP.

---

## File Structure

```
scripts/
  engine.py        ← primitives, pipeline, probe algorithm
  rules.py         ← RULES list (the one file that grows)
  canonicalize.py  ← PEP 723 entry point + thin click CLI
tests/
  test_canonicalize.py
SKILL.md           ← triggering conditions + Claude workflows
```

**Separation of concerns:**

| File | Role | Changes when |
|------|------|-------------|
| `engine.py` | Rule language + execution | Adding new primitive types |
| `rules.py` | Domain-specific rules | Adding support for a new site |
| `canonicalize.py` | CLI glue | Changing CLI flags |

`canonicalize.py` holds the PEP 723 `# /// script` inline-deps header so it
can be executed directly via `uv run scripts/canonicalize.py <url>` with zero
setup. `engine.py` and `rules.py` are plain Python modules loaded via
`sys.path.insert`.

---

## Rule Language

Rules are declarative Python objects. Each `Rule` has a `match` condition and
an `actions` list. Rules run top-to-bottom; **all matching rules apply** (not
first-match-only).

```python
Rule(
    match=Host("www.linkedin.com") & Path("/learning-login/share"),
    actions=[
        UnwrapRedirectParam("redirect"),
        StripParams(params=["account", "forceAccount", "trk", "shareId"]),
    ],
)
```

### Match primitives

| Primitive | Matches |
|-----------|---------|
| `AnyHost()` | Every URL |
| `Host("x.com")` | Exact host |
| `Path("/foo/*")` | Glob path (`fnmatch`) |
| `A & B` | Both conditions (`_And`) |

### Action primitives

| Action | Network? | Effect |
|--------|----------|--------|
| `StripParams(params=[…])` | no | Remove query params matching patterns (denylist) |
| `KeepParams(params=[…])` | no | Remove all query params EXCEPT those matching patterns (allowlist) |
| `UnwrapRedirectParam("key")` | no | URL-decode redirect param → new URL |
| `RewriteHost("x.com")` | no | Replace domain |
| `TrimPathSuffix(n=N)` | no | Remove N trailing path segments |
| `ExtractPath(pattern=r"…")` | no | Regex-extract path sub-segment |
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
canonicalize(url, rules, online) → str

for each rule in rules:
    f = Furl(url)
    if not rule.match.matches(f): continue

    for each action in rule.actions:
        if action is FollowRedirect:
            if online:
                resolved = _http_resolve(url)
                return canonicalize(resolved, rules, online)  # recursive restart
            else:
                break  # skip rule remainder when offline

        new_url = action.apply(f)
        if new_url is not None:          # UnwrapRedirectParam returned a new URL
            f = Furl(new_url)            # switch context to new URL
            url = new_url               # don't break — remaining actions continue
        else:
            url = f.url                 # action mutated f in place

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

---

## Probe Algorithm (`--probe`)

Runs differential HTTP tests against a URL to discover which parts are safe
to remove. Output is a suggested `Rule(...)` block ready to paste into
`rules.py`.

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

Implemented in `_fetch_signals()` (httpx + BeautifulSoup) and `_same_content()`.

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

`validate_rules(rules)` is called automatically at the bottom of `rules.py`
on import. It raises `ValueError` if two `KeepParams` rules can match the
same URL.

**Algorithm:** For each pair of rules that both contain a `KeepParams` action,
extract the host constraint from each matcher (`_hosts_from_matcher`), then
check if those host sets can overlap:

- `AnyHost()` → `{"*"}` (unconstrained — overlaps with everything)
- `Host("x.com")` → `{"x.com"}`
- `_And(Host("x.com"), Path("/foo"))` → `{"x.com"}` (Path doesn't constrain host)

This check is **conservative at the host level**: two `KeepParams` rules with
the same host but non-overlapping paths (e.g. `/a/*` vs `/b/*`) are still
flagged. This is intentional — the path-level overlap analysis would be
complex and error-prone, and having two `KeepParams` for the same host almost
always indicates a design mistake anyway.

## Adding a New Rule

1. `uv run scripts/canonicalize.py --probe <url>` — review output
2. Open `scripts/rules.py` — add the suggested `Rule(...)` after similar-domain rules
3. `uv run scripts/canonicalize.py <url>` — verify output
4. `uv run --group dev pytest tests/ -v` — confirm no regressions
5. Optionally add a test to `tests/test_canonicalize.py`
6. Commit: `feat: add <domain> canonicalization rule`

---

## Dependencies

| Package | Used for |
|---------|----------|
| `httpx` | HTTP GET with redirect following (`FollowRedirect`, probe) |
| `beautifulsoup4` | Parse `<link rel="canonical">` and `<og:url>` in probe |
| `furl` | Mutable URL objects; actions mutate `Furl` in place |
| `click` | CLI in `canonicalize.py` |
| `re`, `fnmatch` | Param matching and path extraction (stdlib) |

---

## Testing

```bash
uv run --group dev pytest tests/ -v
```

Tests import directly from `engine` and `rules` (via `pythonpath = ["scripts"]`
in `pyproject.toml`). No test touches the CLI layer; `canonicalize.py` is
covered by the `engine` tests transitively.

The `FollowRedirect` HTTP path is tested via `unittest.mock.patch("engine._http_resolve")`.
