# canonicalizing-urls Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a Claude Code skill that canonicalizes URLs via a Python rule engine — stripping tracking params, unwrapping redirects, normalizing hosts, extracting canonical paths, and resolving opaque short-links.

**Architecture:** Declarative `Rule` objects (match + actions) live in `canonicalize.py`. Rules run top-to-bottom, all matching rules apply. `FollowRedirect` is the only network action; it restarts the pipeline with the resolved URL. A `--probe` mode runs differential HTTP tests to discover and suggest new rules.

**Tech Stack:** Python 3.9+, delivered as a PEP 723 inline-deps script via `uv run`. Dependencies: `httpx` (HTTP), `beautifulsoup4` (HTML parsing), `furl` (URL manipulation), `click` (CLI). `re`, `fnmatch` from stdlib.

---

## Task 1: Scaffold the skill directory

**Files:**
- Create: `scripts/canonicalize.py`
- Create: `tests/test_canonicalize.py`
- Create: `SKILL.md` (placeholder)

**Step 1: Create directory structure**

```bash
mkdir -p scripts tests
touch scripts/canonicalize.py tests/test_canonicalize.py SKILL.md
```

**Step 2: Write `scripts/canonicalize.py` skeleton**

```python
#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.9"
# dependencies = [
#   "httpx",
#   "beautifulsoup4",
#   "furl",
#   "click",
# ]
# ///
"""URL canonicalization rule engine."""

from __future__ import annotations
import fnmatch
import re
from dataclasses import dataclass
from typing import List, Optional
from urllib.parse import unquote

import click
import httpx
from bs4 import BeautifulSoup
from furl import furl as Furl

# --- Rules defined at bottom of file ---
RULES: list = []


@click.command()
@click.argument("url")
@click.option("--online", is_flag=True, help="Allow HTTP requests (for FollowRedirect rules)")
@click.option("--probe", "do_probe", is_flag=True, help="Discover and suggest rules for unknown URL")
def main(url: str, online: bool, do_probe: bool) -> None:
    if do_probe:
        probe(url)
    else:
        click.echo(canonicalize(url, online=online))


if __name__ == "__main__":
    main()
```

**Step 3: Write `tests/test_canonicalize.py` skeleton**

```python
import sys
sys.path.insert(0, "scripts")

from furl import furl as Furl
from canonicalize import canonicalize, AnyHost, Host, Path, Rule, StripParams
```

**Step 4: Verify script runs**

```bash
uv run scripts/canonicalize.py --help
```
Expected: click-formatted usage with `--online` and `--probe` options (uv auto-installs deps on first run).

**Step 5: Commit**

```bash
git init
git add scripts/ tests/ SKILL.md
git commit -m "chore: scaffold canonicalizing-urls skill"
```

---

## Task 2: Match primitives

**Files:**
- Modify: `scripts/canonicalize.py`
- Modify: `tests/test_canonicalize.py`

**Step 1: Write failing tests**

```python
def fu(url): return Furl(url)

def test_any_host_matches_everything():
    assert AnyHost().matches(fu("https://example.com/path"))

def test_host_matches_exact():
    m = Host("www.linkedin.com")
    assert m.matches(fu("https://www.linkedin.com/learning/course"))
    assert not m.matches(fu("https://linkedin.com/learning/course"))

def test_path_matches_glob():
    m = Path("/share/*")
    assert m.matches(fu("https://x.com/share/p/abc123/"))
    assert not m.matches(fu("https://x.com/posts/123"))

def test_and_combinator():
    m = Host("www.linkedin.com") & Path("/learning-login/share")
    assert m.matches(fu("https://www.linkedin.com/learning-login/share?x=1"))
    assert not m.matches(fu("https://www.linkedin.com/learning/course"))
    assert not m.matches(fu("https://other.com/learning-login/share"))
```

**Step 2: Run — verify FAIL**

```bash
python -m pytest tests/test_canonicalize.py -v -k "test_any_host or test_host or test_path or test_and"
```
Expected: `ImportError: cannot import name 'AnyHost'`

**Step 3: Implement match primitives in `canonicalize.py`**

Add after imports, before `RULES`:

```python
# ---------------------------------------------------------------------------
# Match primitives
# ---------------------------------------------------------------------------

class _MatchBase:
    def __and__(self, other: "_MatchBase") -> "_And":
        return _And(self, other)


class AnyHost(_MatchBase):
    def matches(self, f: Furl) -> bool:
        return True


@dataclass
class Host(_MatchBase):
    host: str

    def matches(self, f: Furl) -> bool:
        return f.host == self.host


@dataclass
class Path(_MatchBase):
    pattern: str  # glob, e.g. "/share/*"

    def matches(self, f: Furl) -> bool:
        return fnmatch.fnmatch(str(f.path), self.pattern)


@dataclass
class _And(_MatchBase):
    left: _MatchBase
    right: _MatchBase

    def matches(self, f: Furl) -> bool:
        return self.left.matches(f) and self.right.matches(f)
```

**Step 4: Run — verify PASS**

```bash
python -m pytest tests/test_canonicalize.py -v -k "test_any_host or test_host or test_path or test_and"
```
Expected: 4 passed.

**Step 5: Commit**

```bash
git add scripts/canonicalize.py tests/test_canonicalize.py
git commit -m "feat: add match primitives (AnyHost, Host, Path, &)"
```

---

## Task 3: StripParams action

**Files:**
- Modify: `scripts/canonicalize.py`
- Modify: `tests/test_canonicalize.py`

**Step 1: Write failing tests**

```python
def test_strip_exact_param():
    f = Furl("https://example.com/?a=1&fbclid=XYZ")
    StripParams(params=["fbclid"]).apply(f)
    assert "fbclid" not in f.url
    assert "a=1" in f.url

def test_strip_glob_param():
    f = Furl("https://example.com/?utm_source=foo&utm_campaign=bar&keep=1")
    StripParams(params=["utm_*"]).apply(f)
    assert "utm_source" not in f.url
    assert "utm_campaign" not in f.url
    assert "keep=1" in f.url

def test_strip_wildcard_all():
    f = Furl("https://example.com/?a=1&b=2")
    StripParams(params=["*"]).apply(f)
    assert f.url == "https://example.com/"

def test_strip_multiple_patterns():
    f = Furl("https://x.com/?fbclid=X&utm_source=Y&rdid=Z&keep=1")
    StripParams(params=["fbclid", "utm_*", "rdid"]).apply(f)
    assert "keep=1" in f.url
    assert "fbclid" not in f.url
    assert "utm_source" not in f.url
    assert "rdid" not in f.url
```

**Step 2: Run — verify FAIL**

```bash
python -m pytest tests/test_canonicalize.py -v -k "test_strip"
```
Expected: `ImportError: cannot import name 'StripParams'`

**Step 3: Implement StripParams**

```python
# ---------------------------------------------------------------------------
# Action primitives
# ---------------------------------------------------------------------------

@dataclass
class StripParams:
    """Remove query params by exact name, glob (utm_*), or /regex/."""
    params: List[str]

    def _matches(self, name: str) -> bool:
        for p in self.params:
            if p == "*":
                return True
            if p.startswith("/") and p.endswith("/"):
                if re.search(p[1:-1], name):
                    return True
            elif "*" in p or "?" in p:
                if fnmatch.fnmatch(name, p):
                    return True
            elif name == p:
                return True
        return False

    def apply(self, f: Furl) -> None:
        to_remove = [k for k in list(f.args.keys()) if self._matches(k)]
        for k in set(to_remove):
            del f.args[k]
```

**Step 4: Run — verify PASS**

```bash
python -m pytest tests/test_canonicalize.py -v -k "test_strip"
```
Expected: 4 passed.

**Step 5: Commit**

```bash
git add scripts/canonicalize.py tests/test_canonicalize.py
git commit -m "feat: add StripParams action with exact/glob/wildcard support"
```

---

## Task 4: Remaining static actions

**Files:**
- Modify: `scripts/canonicalize.py`
- Modify: `tests/test_canonicalize.py`

**Step 1: Write failing tests**

```python
def test_unwrap_redirect_param():
    f = Furl("https://www.linkedin.com/learning-login/share"
             "?account=123&redirect=https%3A%2F%2Fwww.linkedin.com%2Flearning%2Fcourse")
    new_url = UnwrapRedirectParam("redirect").apply(f)
    assert new_url == "https://www.linkedin.com/learning/course"

def test_rewrite_host():
    f = Furl("https://m.facebook.com/story.php?id=123")
    RewriteHost("www.facebook.com").apply(f)
    assert f.url == "https://www.facebook.com/story.php?id=123"

def test_trim_path_suffix():
    f = Furl("https://www.linkedin.com/learning/agile/course-introduction")
    TrimPathSuffix(n=1).apply(f)
    assert f.url == "https://www.linkedin.com/learning/agile"

def test_extract_path():
    f = Furl("https://www.amazon.com/-/zh_TW/Clean-Code/dp/0132350882")
    ExtractPath(pattern=r"/dp/[A-Z0-9]+").apply(f)
    assert f.url == "https://www.amazon.com/dp/0132350882"

def test_strip_fragment():
    f = Furl("https://example.com/page#section-2")
    StripFragment().apply(f)
    assert f.url == "https://example.com/page"
```

**Step 2: Run — verify FAIL**

```bash
python -m pytest tests/test_canonicalize.py -v -k "test_unwrap or test_rewrite or test_trim or test_extract or test_strip_frag"
```

**Step 3: Implement the static actions**

```python
@dataclass
class UnwrapRedirectParam:
    """Decode a URL-encoded redirect param; returns new URL string."""
    key: str

    def apply(self, f: Furl) -> Optional[str]:
        val = f.args.get(self.key)
        return unquote(str(val)) if val else None


@dataclass
class RewriteHost:
    """Replace the domain."""
    host: str

    def apply(self, f: Furl) -> None:
        f.host = self.host


@dataclass
class TrimPathSuffix:
    """Remove N trailing path segments and clear query+fragment."""
    n: int

    def apply(self, f: Furl) -> None:
        f.path.segments = f.path.segments[:-self.n]
        f.args.clear()
        f.remove(fragment=True)


@dataclass
class ExtractPath:
    """Find first regex match in path; discard everything outside it."""
    pattern: str

    def apply(self, f: Furl) -> None:
        m = re.search(self.pattern, str(f.path))
        if m:
            f.path = m.group(0)
            f.args.clear()
            f.remove(fragment=True)


class StripFragment:
    """Remove URL fragment (#...)."""

    def apply(self, f: Furl) -> None:
        f.remove(fragment=True)
```

**Step 4: Run — verify PASS**

```bash
python -m pytest tests/test_canonicalize.py -v -k "test_unwrap or test_rewrite or test_trim or test_extract or test_strip_frag"
```
Expected: 5 passed.

**Step 5: Commit**

```bash
git add scripts/canonicalize.py tests/test_canonicalize.py
git commit -m "feat: add static actions (UnwrapRedirectParam, RewriteHost, TrimPathSuffix, ExtractPath, StripFragment)"
```

---

## Task 5: Rule class and pipeline

**Files:**
- Modify: `scripts/canonicalize.py`
- Modify: `tests/test_canonicalize.py`

**Step 1: Write failing tests**

```python
def test_pipeline_strips_fbclid():
    rules = [Rule(match=AnyHost(), actions=[StripParams(params=["fbclid"])])]
    assert canonicalize("https://buzzorange.com/article/?fbclid=XYZ&other=1", rules=rules) \
        == "https://buzzorange.com/article/?other=1"

def test_pipeline_all_matching_rules_run():
    """All matching rules run, not just the first."""
    rules = [
        Rule(match=AnyHost(), actions=[StripParams(params=["a"])]),
        Rule(match=AnyHost(), actions=[StripParams(params=["b"])]),
    ]
    assert canonicalize("https://example.com/?a=1&b=2&c=3", rules=rules) \
        == "https://example.com/?c=3"

def test_pipeline_non_matching_rule_skipped():
    rules = [Rule(match=Host("other.com"), actions=[StripParams(params=["*"])])]
    assert canonicalize("https://example.com/?keep=1", rules=rules) \
        == "https://example.com/?keep=1"

def test_pipeline_linkedin_learning_login():
    rules = [
        Rule(match=AnyHost(), actions=[StripParams(params=["fbclid", "utm_*"])]),
        Rule(
            match=Host("www.linkedin.com") & Path("/learning-login/share"),
            actions=[
                UnwrapRedirectParam("redirect"),
                StripParams(params=["account", "forceAccount", "trk", "shareId"]),
            ],
        ),
    ]
    before = ("https://www.linkedin.com/learning-login/share"
              "?account=352396234&forceAccount=false"
              "&redirect=https%3A%2F%2Fwww.linkedin.com%2Flearning%2Fcourse-name"
              "%3Ftrk%3Dshare_ent_url%26shareId%3Dabc")
    assert canonicalize(before, rules=rules) == "https://www.linkedin.com/learning/course-name"
```

**Step 2: Run — verify FAIL**

```bash
python -m pytest tests/test_canonicalize.py -v -k "test_pipeline"
```
Expected: `ImportError` or `NameError` on `Rule`/`canonicalize`.

**Step 3: Implement Rule and canonicalize()**

```python
class FollowRedirect:
    """Resolve URL via HTTP and restart pipeline. Requires online=True."""
    pass


@dataclass
class Rule:
    match: _MatchBase
    actions: list


def canonicalize(url: str, rules: list = None, online: bool = False) -> str:
    """Apply all matching rules to url. Returns canonical URL."""
    if rules is None:
        rules = RULES

    for rule in rules:
        f = Furl(url)
        if not rule.match.matches(f):
            continue
        for action in rule.actions:
            if isinstance(action, FollowRedirect):
                if online:
                    return canonicalize(_http_resolve(url), rules=rules, online=online)
                break  # skip if offline
            new_url = action.apply(f)
            if new_url is not None:
                # UnwrapRedirectParam: complete URL replacement — restart from new URL
                url = new_url
                break
            else:
                url = f.url

    return url
```

**Step 4: Run — verify PASS**

```bash
python -m pytest tests/test_canonicalize.py -v -k "test_pipeline"
```
Expected: 4 passed.

**Step 5: Commit**

```bash
git add scripts/canonicalize.py tests/test_canonicalize.py
git commit -m "feat: add Rule class and canonicalize() pipeline"
```

---

## Task 6: FollowRedirect (online mode)

**Files:**
- Modify: `scripts/canonicalize.py`
- Modify: `tests/test_canonicalize.py`

**Step 1: Write failing tests**

```python
from unittest.mock import patch

def test_follow_redirect_restarts_pipeline():
    rules = [
        Rule(match=AnyHost(), actions=[StripParams(params=["rdid", "utm_*"])]),
        Rule(match=Host("www.facebook.com") & Path("/share/*"),
             actions=[FollowRedirect()]),
    ]
    resolved = "https://www.facebook.com/Page/posts/pfbid0abc?rdid=XYZ"
    with patch("canonicalize._http_resolve", return_value=resolved):
        result = canonicalize(
            "https://www.facebook.com/share/p/18GKaNgTxp/",
            rules=rules, online=True,
        )
    assert result == "https://www.facebook.com/Page/posts/pfbid0abc"

def test_follow_redirect_skipped_when_offline():
    rules = [Rule(match=AnyHost(), actions=[FollowRedirect()])]
    url = "https://example.com/share/p/abc"
    assert canonicalize(url, rules=rules, online=False) == url
```

**Step 2: Run — verify FAIL**

```bash
python -m pytest tests/test_canonicalize.py -v -k "test_follow"
```

**Step 3: Implement `_http_resolve()` using httpx**

```python
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; url-canonicalizer/1.0)"}


def _http_resolve(url: str, timeout: int = 10) -> str:
    """Follow HTTP redirects and return final URL."""
    try:
        resp = httpx.get(url, follow_redirects=True, timeout=timeout, headers=_HEADERS)
        return str(resp.url)
    except Exception:
        return url
```

**Step 4: Run — verify PASS**

```bash
python -m pytest tests/test_canonicalize.py -v -k "test_follow"
```
Expected: 2 passed.

**Step 5: Commit**

```bash
git add scripts/canonicalize.py tests/test_canonicalize.py
git commit -m "feat: implement FollowRedirect with httpx pipeline restart"
```

---

## Task 7: Probe algorithm

**Files:**
- Modify: `scripts/canonicalize.py`
- Modify: `tests/test_canonicalize.py`

**Step 1: Implement `_fetch_signals()` using httpx + bs4**

```python
def _fetch_signals(url: str, timeout: int = 10) -> dict:
    """Fetch URL and extract canonical signals."""
    try:
        resp = httpx.get(url, follow_redirects=True, timeout=timeout, headers=_HEADERS)
        final_url = str(resp.url)
        soup = BeautifulSoup(resp.text, "html.parser")
    except Exception as e:
        return {"final_url": url, "error": str(e)}

    canonical_tag = soup.find("link", rel="canonical")
    og_url_tag = soup.find("meta", property="og:url")
    return {
        "final_url": final_url,
        "canonical": canonical_tag["href"] if canonical_tag else None,
        "og_url": og_url_tag.get("content") if og_url_tag else None,
        "title": soup.title.string.strip() if soup.title else None,
    }


def _best_canonical(signals: dict, original_url: str) -> Optional[str]:
    """Return best canonical signal in priority order."""
    return (signals.get("canonical")
            or signals.get("og_url")
            or (signals["final_url"] if signals["final_url"] != original_url else None))


def _same_content(a: dict, b: dict) -> bool:
    if a.get("final_url") and a["final_url"] == b.get("final_url"):
        return True
    if a.get("canonical") and a["canonical"] == b.get("canonical"):
        return True
    if a.get("og_url") and a["og_url"] == b.get("og_url"):
        return True
    if a.get("title") and a["title"] == b.get("title"):
        return True
    return False
```

**Step 2: Implement `probe()` using furl for URL construction**

```python
def probe(url: str) -> None:
    """Differential HTTP test to suggest canonicalization rules for url."""
    print(f"\nProbing: {url}\n")
    base = _fetch_signals(url)
    base_canonical = _best_canonical(base, url)
    print(f"  Baseline canonical: {base_canonical or '(none found)'}")

    original = Furl(url)
    suggestions: list[str] = []

    # --- 1. Params ---
    if original.args:
        no_params = Furl(url)
        no_params.args.clear()
        if _same_content(base, _fetch_signals(no_params.url)):
            print("  strip ALL params → same ✓")
            suggestions.append('StripParams(params=["*"])')
        else:
            strippable = []
            for p in list(original.args.keys()):
                test = Furl(url)
                del test.args[p]
                if _same_content(base, _fetch_signals(test.url)):
                    print(f"  strip {p!r} → same ✓")
                    strippable.append(p)
                else:
                    print(f"  strip {p!r} → different ✗")
            if strippable:
                suggestions.append(f"StripParams(params={strippable})")

    # --- 2. Host ---
    if original.host.startswith("m."):
        www_host = "www." + original.host[2:]
        test = Furl(url)
        test.host = www_host
        if _same_content(base, _fetch_signals(test.url)):
            print(f"  rewrite host → {www_host!r} ✓")
            suggestions.append(f'RewriteHost("{www_host}")')

    # --- 3. Path ---
    canonical_url = base_canonical or base["final_url"]
    if canonical_url and canonical_url != url:
        canon_path = str(Furl(canonical_url).path)
        orig_path = str(original.path)
        if orig_path.endswith(canon_path) and canon_path != orig_path:
            pattern = re.sub(r"[A-Z0-9]{6,}", "[A-Z0-9]+", canon_path)
            print(f"  extract path {canon_path!r} ✓")
            suggestions.append(f'ExtractPath(pattern=r"{pattern}")')
        elif orig_path.startswith(canon_path) and canon_path != orig_path:
            removed = orig_path[len(canon_path):]
            n = len([s for s in removed.split("/") if s])
            print(f"  trim {n} path suffix segment(s) ✓")
            suggestions.append(f"TrimPathSuffix(n={n})")

    # --- Report ---
    print(f"\n  Suggested rule:")
    if suggestions:
        print(f"    Rule(")
        print(f"        match=Host({original.host!r}),")
        print(f"        actions=[{', '.join(suggestions)}],")
        print(f"    ),")
    else:
        print("    (no automatic suggestion — review manually)")
```

**Step 3: Smoke test probe manually**

```bash
uv run scripts/canonicalize.py --probe \
  "https://buzzorange.com/techorange/2025/02/19/ai-and-learning/?fbclid=IwY2xjawIjUsNleHRuA2FlbQIxMQABHWJdrbxJNEqmsD1f96cwE_HRnWG-3pOmhBJShJEkJzXqCsc3h7QwNKrQKQ_aem_5Fx7F4GyHHFI1exewhoPOg"
```
Expected: suggests `StripParams(params=["fbclid", ...])`.

**Step 4: Commit**

```bash
git add scripts/canonicalize.py
git commit -m "feat: implement --probe with httpx + beautifulsoup4"
```

---

## Task 8: Initial RULES

**Files:**
- Modify: `scripts/canonicalize.py`

**Step 1: Write tests for built-in rules**

```python
def test_builtin_rules_strip_fbclid():
    url = "https://buzzorange.com/techorange/2025/02/19/ai/?fbclid=XYZ&aem_abc=1"
    assert canonicalize(url) == "https://buzzorange.com/techorange/2025/02/19/ai/"

def test_builtin_rules_linkedin_strip_u():
    url = "https://www.linkedin.com/learning/agile/course-introduction?u=352396234"
    assert canonicalize(url) == "https://www.linkedin.com/learning/agile/course-introduction"

def test_builtin_rules_amazon_extract_dp():
    url = "https://www.amazon.com/-/zh_TW/Clean-Code/dp/0132350882"
    assert canonicalize(url) == "https://www.amazon.com/dp/0132350882"

def test_builtin_rules_facebook_mobile():
    url = "https://m.facebook.com/story.php?story_fbid=9819635824716580&id=100000107794908&wtsid=rdr_0ROl"
    result = canonicalize(url)
    assert result.startswith("https://www.facebook.com/")
    assert "wtsid" not in result
```

**Step 2: Run — verify FAIL**

```bash
python -m pytest tests/test_canonicalize.py -v -k "test_builtin"
```

**Step 3: Replace `RULES = []` with initial rules**

```python
RULES = [
    # --- Universal tracking params ---
    Rule(
        match=AnyHost(),
        actions=[StripParams(params=[
            "fbclid", "utm_*", "wts*", "aem_*", "rdid",
            "_hsenc", "_hsmi", "mc_cid", "mc_eid",   # HubSpot/Mailchimp
        ])],
    ),

    # --- LinkedIn ---
    Rule(
        match=Host("www.linkedin.com") & Path("/learning-login/share"),
        actions=[
            UnwrapRedirectParam("redirect"),
            StripParams(params=["account", "forceAccount", "trk", "shareId"]),
        ],
    ),
    Rule(
        match=Host("www.linkedin.com"),
        actions=[StripParams(params=["u"])],
    ),

    # --- Facebook ---
    Rule(
        match=Host("m.facebook.com"),
        actions=[RewriteHost("www.facebook.com")],
    ),
    Rule(
        match=Host("www.facebook.com") & Path("/share/*"),
        actions=[FollowRedirect()],
    ),

    # --- Amazon ---
    Rule(
        match=Host("www.amazon.com"),
        actions=[ExtractPath(pattern=r"/dp/[A-Z0-9]+")],
    ),
]
```

**Step 4: Run — verify PASS**

```bash
python -m pytest tests/test_canonicalize.py -v -k "test_builtin"
```
Expected: 4 passed.

**Step 5: Run all tests**

```bash
python -m pytest tests/ -v
```
Expected: all passed.

**Step 6: Commit**

```bash
git add scripts/canonicalize.py tests/test_canonicalize.py
git commit -m "feat: add initial built-in rules (tracking, LinkedIn, Facebook, Amazon)"
```

---

## Task 9: Write SKILL.md

**Files:**
- Modify: `SKILL.md`

**Step 1: Write SKILL.md**

```markdown
---
name: canonicalizing-urls
description: >
  Use when working with any URL that may contain tracking parameters, redirect
  wrappers, locale prefixes, or opaque share links — to produce a clean,
  canonical form. Triggers proactively when Claude uses a URL in a context where
  cleanliness matters (saving to Notion, quoting in a document, creating a
  hyperlink). Also triggers explicitly when the user says "canonicalize",
  "clean this URL", "strip tracking params", or invokes /canonicalize.
---

# canonicalizing-urls

Canonicalize URLs by running `scripts/canonicalize.py` (requires `uv`).

## Proactive use

When a URL appears in a context where it will be saved, shared, or cited:
1. `uv run scripts/canonicalize.py <url>` (offline, static rules only)
2. If the URL matches an opaque short-link pattern (e.g. `/share/p/`), add `--online`
3. If the result differs from the input, substitute the canonical form
4. Note the change inline: "(canonicalized: removed fbclid)"

## Explicit use

When the user asks to canonicalize a URL:
1. `uv run scripts/canonicalize.py <url>`
2. If unchanged and URL looks non-canonical, run `--probe` to discover rules

## Adding a new rule

When the script returns unchanged output but the URL is clearly non-canonical:
1. `uv run scripts/canonicalize.py --probe <url>` — review suggested actions
2. Ask user: generalize to a pattern, or keep domain-specific?
3. Add the confirmed `Rule(...)` to `RULES` in `scripts/canonicalize.py`
   (insert after similar-domain rules, before the closing bracket)
4. `uv run scripts/canonicalize.py <original_url>` — verify output
5. Commit: `feat: add <domain> canonicalization rule`
```

**Step 2: Commit**

```bash
git add SKILL.md
git commit -m "feat: write SKILL.md with triggering description and workflow"
```

---

## Task 10: Package the skill

**Step 1: Run all tests one final time**

```bash
python -m pytest tests/ -v
```
Expected: all passed.

**Step 2: Package**

```bash
python /Users/william/.claude/plugins/cache/anthropic-agent-skills/document-skills/69c0b1a06741/skills/skill-creator/scripts/package_skill.py .
```
Expected: `canonicalizing-urls.skill` created.

**Step 3: Final commit**

```bash
git add canonicalizing-urls.skill
git commit -m "chore: package skill for distribution"
```

---

## Summary

| Task | What it builds |
|------|---------------|
| 1 | Scaffold + PEP 723 uv inline-deps skeleton |
| 2 | Match primitives (`AnyHost`, `Host`, `Path`, `&`) — accept `Furl` objects |
| 3 | `StripParams` with exact/glob/wildcard — mutates `Furl` in place |
| 4 | Static actions (`UnwrapRedirectParam`, `RewriteHost`, `TrimPathSuffix`, `ExtractPath`, `StripFragment`) |
| 5 | `Rule` class + `canonicalize()` pipeline |
| 6 | `FollowRedirect` + `_http_resolve()` via httpx |
| 7 | `--probe` algorithm via httpx + beautifulsoup4 + furl |
| 8 | Initial built-in rules |
| 9 | `SKILL.md` |
| 10 | Package |
