# Design: `canonicalizing-urls` Skill

**Date:** 2026-02-26

## Overview

A Claude Code skill that canonicalizes URLs — stripping tracking params, unwrapping redirects, normalizing hosts, extracting canonical paths, and resolving opaque short-links. Triggers both proactively (when Claude uses URLs in context) and explicitly (user invokes `/canonicalize`).

---

## File Structure

```
canonicalizing-urls/
├── SKILL.md
└── scripts/
    └── canonicalize.py
```

No `references/` or `assets/` needed. The rules *are* the script.

---

## Rule Format

Rules live in `canonicalize.py` as declarative Python objects. Each rule has a `match` condition and one or more `actions`. Rules apply top-to-bottom; all matching rules run (not first-match-only).

```python
RULES = [
    # Universal: strip tracking params from any domain
    Rule(
        match=AnyHost(),
        actions=[StripParams(params=["fbclid", "utm_*", "wts*", "aem_*", "rdid"])],
    ),
    # LinkedIn: unwrap share redirect, strip account params
    Rule(
        match=Host("www.linkedin.com") & Path("/learning-login/share"),
        actions=[
            UnwrapRedirectParam("redirect"),
            StripParams(params=["account", "forceAccount", "trk", "shareId"]),
        ],
    ),
    # LinkedIn: strip account param on course pages
    Rule(
        match=Host("www.linkedin.com"),
        actions=[StripParams(params=["u"])],
    ),
    # Facebook: mobile → desktop
    Rule(
        match=Host("m.facebook.com"),
        actions=[RewriteHost("www.facebook.com")],
    ),
    # Facebook: opaque share links — must resolve via HTTP
    Rule(
        match=Host("www.facebook.com") & Path("/share/*"),
        actions=[FollowRedirect()],
    ),
    # Amazon: extract canonical product path
    Rule(
        match=Host("www.amazon.com"),
        actions=[ExtractPath(pattern=r"/dp/[A-Z0-9]+")],
    ),
]
```

### Match Primitives

| Primitive | Meaning |
|-----------|---------|
| `AnyHost()` | Any domain |
| `Host("x.com")` | Exact host match |
| `Path("/foo/*")` | Glob path match |
| `A & B` | Both conditions must hold |

### Action Primitives

| Action | Network? | Description |
|--------|----------|-------------|
| `StripParams(params=[…])` | no | Remove params by name or glob (`utm_*`) |
| `UnwrapRedirectParam("key")` | no | Decode URL-encoded redirect param and use as new URL |
| `RewriteHost("x.com")` | no | Replace domain |
| `TrimPathSuffix(n=N)` | no | Remove N trailing path segments |
| `ExtractPath(pattern=r"…")` | no | Find matching path segment, discard the rest |
| `StripFragment()` | no | Remove `#fragment` |
| `FollowRedirect()` | **yes** | HTTP GET, follow redirects, restart pipeline with final URL |

### `StripParams` param syntax

- Exact string: `"forceAccount"` — exact match
- Glob: `"utm_*"` — prefix wildcard
- Regex escape hatch: `"/^custom_.+/"` — full regex

---

## Script CLI

```bash
# Apply static rules only (fast, offline)
python canonicalize.py <url>

# Apply static + dynamic rules (makes HTTP requests for FollowRedirect)
python canonicalize.py --online <url>

# Discovery mode: probe URL to suggest new rules
python canonicalize.py --probe <url>
```

---

## Probe Algorithm (Rule Discovery)

When `--probe` is passed, the script runs a differential HTTP test to discover which URL parts are safe to remove. Steps in order:

### 1. Params

```
a. Fetch URL with ALL params stripped
   → same content?  → suggest StripParams("*")
   → different?     → proceed to per-param test

b. Strip each param individually
   → same?  → mark strippable
   → diff?  → mark load-bearing

c. Emit: StripParams([strippable_params])
```

### 2. Host

```
a. Try m.→www. rewrite      → if redirect or same canonical → RewriteHost
b. Try https upgrade         → always apply (implicit)
```

### 3. Path

```
a. Fetch URL → read <link rel="canonical"> or follow redirects
b. Compare canonical path to original path:
   - canonical = right-trim of original  → TrimPathSuffix(n=N)
   - canonical = embedded sub-segment    → ExtractPath(pattern=generalize(segment))
   - canonical = left-trim of original   → TrimPathPrefix(n=N)
c. Verify: apply suggested action → does it produce the canonical URL?
d. Generalize: replace literal IDs with regex (e.g. "0132350882" → [A-Z0-9]+)
```

### "Same content" signals (checked in priority order)

1. `Location:` redirect header — definitive
2. `<link rel="canonical">` — strong
3. `<og:url>` meta tag — strong
4. `<title>` tag — moderate
5. Content hash — last resort (noisy)

### Probe output example

```
Probing: https://www.linkedin.com/learning-login/share?account=352396234&redirect=https%3A%2F%2F...
  strip all params       → different (redirect= is load-bearing)
  strip account=         → same (canonical: /learning/course-name)
  strip forceAccount=    → same
  strip redirect=        → different
  → Suggested rule:
      match: Host("www.linkedin.com") & Path("/learning-login/share")
      actions: [UnwrapRedirectParam("redirect"), StripParams(["account","forceAccount"])]
  → Generalize to all /learning-login/share paths? [y/n]
```

---

## Rule Learning Workflow

1. User provides a URL that matches no rule (or explicit "add rule for this")
2. Claude runs `--probe` → reviews suggested rule
3. Claude asks user: generalize to pattern or keep specific?
4. Claude appends confirmed rule to `RULES` list in `canonicalize.py`
5. Claude runs the script on the original URL to verify output matches expectation

---

## Triggering

**Proactive:** Claude detects a URL being used in a context where clean URLs matter
(saving to Notion, quoting in a document, creating a hyperlink). Claude silently
runs the script (`--online` if short-link detected); if output differs, substitutes
the canonical form and notes it inline.

**Explicit:** User invokes `/canonicalize <url>` or says "clean this URL" / "canonicalize this".
Claude runs the script and returns the result. If no rule matches, Claude offers to
probe and add a new rule.

---

## Known Rule Examples (from design session)

| Before | After | Rule type |
|--------|-------|-----------|
| `https://buzzorange.com/...?fbclid=...` | Strip `fbclid` | `StripParams` (AnyHost) |
| `https://www.linkedin.com/learning-login/share?redirect=...&account=...` | Unwrap redirect, strip account | `UnwrapRedirectParam` + `StripParams` |
| `https://www.linkedin.com/learning/.../course-introduction?u=352396234` | Strip `u=` | `StripParams` |
| `https://m.facebook.com/story.php?story_fbid=...` | `www.facebook.com` | `RewriteHost` |
| `https://www.facebook.com/share/p/18GKaNgTxp/` | Follow redirect, strip `rdid` | `FollowRedirect` + `StripParams` |
| `https://www.amazon.com/-/zh_TW/Clean-Code-.../dp/0132350882` | `/dp/0132350882` | `ExtractPath` |

---

## Out of Scope

- URL shorteners (bit.ly, t.co) — handled by `FollowRedirect` as a rule, not special-cased
- Login-gated pages — probe cannot verify these; rules must be added manually
- Canonicalization of URL *content* (lowercasing, encoding normalization) — not addressed
