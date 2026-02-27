"""URL canonicalization engine — primitives, pipeline, and probe algorithm."""

from __future__ import annotations
import fnmatch
import re
from dataclasses import dataclass
from typing import List, Optional
from urllib.parse import unquote

import httpx
from bs4 import BeautifulSoup
from furl import furl as Furl


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
        if self.n <= 0:
            return
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


class FollowRedirect:
    """Resolve URL via HTTP and restart pipeline. Requires online=True."""
    pass


@dataclass
class Rule:
    match: _MatchBase
    actions: list


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; url-canonicalizer/1.0)"}


def _http_resolve(url: str, timeout: int = 10) -> str:
    """Follow HTTP redirects and return final URL."""
    try:
        resp = httpx.get(url, follow_redirects=True, timeout=timeout, headers=_HEADERS)
        return str(resp.url)
    except Exception:
        return url


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
        "canonical": canonical_tag.get("href") if canonical_tag else None,
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


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def canonicalize(url: str, rules: list = None, online: bool = False) -> str:
    """Apply all matching rules to url. Returns canonical URL."""
    if rules is None:
        from rules import RULES
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
                # Don't break — remaining actions (e.g. StripParams) run on the unwrapped URL,
                # which is the intended behavior for rules like LinkedIn learning-login/share.
                f = Furl(new_url)
                url = new_url
            else:
                url = f.url

    return url


# ---------------------------------------------------------------------------
# Probe algorithm
# ---------------------------------------------------------------------------

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
