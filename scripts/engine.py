"""URL canonicalization engine — primitives, pipeline, and probe algorithm."""

from __future__ import annotations
import fnmatch
import itertools
import logging
import re
from dataclasses import dataclass
from urllib.parse import unquote

logger = logging.getLogger(__name__)

import httpx
from bs4 import BeautifulSoup
from furl import furl as Furl

try:
    import re2 as _glob_re_module  # google-re2: true DFA, O(L) host matching
except ImportError:
    import re as _glob_re_module  # type: ignore[no-redef]


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
class HostGlob(_MatchBase):
    pattern: str  # fnmatch glob, e.g. "m.*.com"

    def matches(self, f: Furl) -> bool:
        return fnmatch.fnmatch(f.host, self.pattern)


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

def _param_matches(patterns: list[str], name: str) -> bool:
    """Return True if name matches any pattern in patterns.

    Pattern syntax:
      "*"          — match everything
      "utm_*"      — fnmatch glob
      "/^custom_/" — regex (delimited by /)
      "exact"      — literal match
    """
    for p in patterns:
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


@dataclass
class StripParams:
    """Remove query params matching any pattern in params."""
    params: list[str]

    def apply(self, f: Furl) -> None:
        to_remove = [k for k in list(f.args.keys()) if _param_matches(self.params, k)]
        for k in set(to_remove):
            del f.args[k]


@dataclass
class KeepParams:
    """Remove all query params EXCEPT those matching patterns in params.

    Use in domain-specific rules where you know the full set of meaningful
    params (allowlist). Prefer StripParams for universal/broad rules.
    Never use KeepParams in AnyHost() rules — validate_rules() will catch it.
    """
    params: list[str]

    def apply(self, f: Furl) -> None:
        to_remove = [k for k in list(f.args.keys()) if not _param_matches(self.params, k)]
        for k in set(to_remove):
            del f.args[k]


@dataclass
class UnwrapRedirectParam:
    """Decode a URL-encoded redirect param; returns new URL string."""
    key: str

    def apply(self, f: Furl) -> str | None:
        val = f.args.get(self.key)
        return unquote(str(val)) if val else None


@dataclass
class RewriteHost:
    """Replace the domain."""
    host: str

    def apply(self, f: Furl) -> None:
        f.host = self.host


@dataclass
class RewriteHostPrefix:
    """Replace a host prefix. No-op if host doesn't start with old."""
    old: str
    new: str

    def apply(self, f: Furl) -> None:
        if f.host.startswith(self.old):
            f.host = self.new + f.host[len(self.old):]


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
# Bootstrap lint check
# ---------------------------------------------------------------------------

def _hosts_from_matcher(matcher: _MatchBase) -> frozenset:
    """Return the set of hosts that can match. '*' means unconstrained."""
    if isinstance(matcher, AnyHost):
        return frozenset(["*"])
    if isinstance(matcher, Host):
        return frozenset([matcher.host])
    if isinstance(matcher, _And):
        left = _hosts_from_matcher(matcher.left)
        right = _hosts_from_matcher(matcher.right)
        # _And is more specific — if one side constrains the host, prefer it
        if left == frozenset(["*"]):
            return right
        if right == frozenset(["*"]):
            return left
        return left & right  # intersection (empty if different hosts — impossible match)
    return frozenset(["*"])  # unknown matcher → conservative


def _rules_can_overlap(a: Rule, b: Rule) -> bool:
    """True if rules a and b can match the same URL (conservative heuristic)."""
    ha = _hosts_from_matcher(a.match)
    hb = _hosts_from_matcher(b.match)
    if "*" in ha or "*" in hb:
        return True
    return bool(ha & hb)


def validate_rules(rules: list) -> None:
    """Bootstrap lint: raise ValueError if two KeepParams rules can overlap.

    Two overlapping KeepParams rules are destructive — each strips what the
    other kept, leaving no params. Called automatically at the bottom of
    rules.py on import.
    """
    keep_rules = [(i, r) for i, r in enumerate(rules)
                  if any(isinstance(a, KeepParams) for a in r.actions)]
    for (i, ri), (j, rj) in itertools.combinations(keep_rules, 2):
        if _rules_can_overlap(ri, rj):
            raise ValueError(
                f"Conflicting KeepParams: rules[{i}] and rules[{j}] can match "
                f"the same URL. Use KeepParams only in domain-specific rules "
                f"(Host(...)), never in AnyHost() or overlapping rules."
            )


# ---------------------------------------------------------------------------
# Rule index
# ---------------------------------------------------------------------------

def _glob_to_inner_regex(pattern: str) -> str:
    """Convert fnmatch glob to a regex fragment safe for re2/re fullmatch().

    fnmatch.translate() returns '(?s:PAT)\\Z' (Python ≥3.12) or '(?s:PAT)\\z'
    (Python ≥3.14). Strip the trailing anchor because fullmatch() already
    anchors both ends, and re2 supports neither \\Z nor \\z.
    The (?s:...) inline-DOTALL group is preserved — re2 supports it.
    """
    t = fnmatch.translate(pattern)
    if t.endswith("\\Z") or t.endswith("\\z"):
        t = t[:-2]
    return t


class _RuleIndex:
    """Host-keyed index that reduces per-rule match checks in canonicalize().

    Three buckets built once at construction time:
      universal  AnyHost() rules — always candidates
      exact      Host("x.com") rules — O(1) dict lookup by hostname
      globs      HostGlob("m.*.com") rules — single compiled re2 alternation

    candidate_indices(host) returns a frozenset[int] of rule positions that
    *could* match this host. Rules outside the set are skipped without calling
    rule.match.matches(). The set is recomputed only when the host changes
    mid-pipeline (e.g. RewriteHostPrefix rewrites m.youtube.com → www.youtube.com),
    so subsequent Host("www.youtube.com") rules correctly become candidates.

    See DESIGN.md § Rule Indexing.
    """

    def __init__(self, rules: list[Rule]) -> None:
        self._universal: set[int] = set()
        self._exact: dict[str, set[int]] = {}
        self._globs: list[tuple[int, str]] = []  # (rule_idx, glob_pattern)
        self._glob_re: re.Pattern[str] | None = None  # re2.Pattern if google-re2 installed
        self._group_to_rule: dict[str, int] = {}  # group name → rule index

        for i, rule in enumerate(rules):
            m = rule.match
            if isinstance(m, AnyHost):
                self._universal.add(i)
            elif isinstance(m, HostGlob):
                self._globs.append((i, m.pattern))
            else:
                hosts = _hosts_from_matcher(m)
                if "*" in hosts:
                    self._universal.add(i)  # conservative: unknown matcher type
                else:
                    for h in hosts:
                        self._exact.setdefault(h, set()).add(i)

        if self._globs:
            parts = []
            for j, (rule_idx, pat) in enumerate(self._globs):
                group = f"g{j}"
                self._group_to_rule[group] = rule_idx
                parts.append(f"(?P<{group}>{_glob_to_inner_regex(pat)})")
            self._glob_re = _glob_re_module.compile("(?i)" + "|".join(parts))

    def candidate_indices(self, host: str | None) -> frozenset[int]:
        """Rule indices that could match this host, used as a pre-filter.

        If host is None (URL has no host component), only AnyHost() rules are
        returned — exact and glob buckets require a concrete hostname.
        """
        if host is None:
            return frozenset(self._universal)
        indices = set(self._universal)
        indices.update(self._exact.get(host, set()))
        if self._glob_re is not None:
            m = self._glob_re.fullmatch(host)
            if m:
                indices.update(
                    self._group_to_rule[k]
                    for k, v in m.groupdict().items()
                    if v is not None
                )
        return frozenset(indices)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; url-canonicalizer/1.0)"}


def _http_resolve(url: str, timeout: int = 10) -> str:
    """Follow HTTP redirects and return final URL."""
    try:
        resp = httpx.get(url, follow_redirects=True, timeout=timeout, headers=_HEADERS)
        return str(resp.url)
    except Exception as exc:
        logger.debug("_http_resolve failed for %s: %s", url, exc)
        return url


def _fetch_signals(url: str, timeout: int = 10) -> dict:
    """Fetch URL and extract canonical signals."""
    try:
        resp = httpx.get(url, follow_redirects=True, timeout=timeout, headers=_HEADERS)
        final_url = str(resp.url)
        soup = BeautifulSoup(resp.text, "html.parser")
    except Exception as exc:
        logger.debug("_fetch_signals failed for %s: %s", url, exc)
        return {"final_url": url, "error": str(exc)}

    canonical_tag = soup.find("link", rel="canonical")
    og_url_tag = soup.find("meta", property="og:url")
    return {
        "final_url": final_url,
        "canonical": canonical_tag.get("href") if canonical_tag else None,
        "og_url": og_url_tag.get("content") if og_url_tag else None,
        "title": soup.title.string.strip() if soup.title else None,
    }


def _best_canonical(signals: dict, original_url: str) -> str | None:
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

def canonicalize(
    url: str,
    rules: list[Rule],
    online: bool = False,
    _depth: int = 0,
    _index: _RuleIndex | None = None,  # internal: passed through FollowRedirect recursion;
                                        # passing an index built from a *different* rules list
                                        # will silently apply wrong candidates.
) -> str:
    """Apply all matching rules to url. Returns canonical URL."""
    if _depth > 10:
        logger.debug("canonicalize: max redirect depth reached for %s", url)
        return url

    if _index is None:
        _index = _RuleIndex(rules)

    prev_host: str | None = None
    candidates: frozenset[int] = frozenset()
    current_host: str | None = Furl(url).host  # one-time parse; updated after host-changing actions

    for i, rule in enumerate(rules):
        if current_host != prev_host:
            candidates = _index.candidate_indices(current_host)
            prev_host = current_host
        if i not in candidates:
            continue
        f = Furl(url)  # only instantiated for candidate rules
        if not rule.match.matches(f):
            continue
        for action in rule.actions:
            if isinstance(action, FollowRedirect):
                if online:
                    return canonicalize(
                        _http_resolve(url), rules=rules, online=online,
                        _depth=_depth + 1, _index=_index,
                    )
                break  # skip if offline
            new_url = action.apply(f)
            if new_url is not None:
                # Don't break — remaining actions (e.g. StripParams) run on the unwrapped URL,
                # which is the intended behavior for rules like LinkedIn learning-login/share.
                f = Furl(new_url)
                url = new_url
            else:
                url = f.url
        current_host = f.host  # pick up host changes from RewriteHost / RewriteHostPrefix

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
