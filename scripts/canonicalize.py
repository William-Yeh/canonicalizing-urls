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
# Rule / action stubs (filled in later tasks)
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
                # UnwrapRedirectParam: URL replaced — switch f to new URL, continue remaining actions
                f = Furl(new_url)
                url = new_url
            else:
                url = f.url

    return url


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
