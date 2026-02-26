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
class Rule:
    match: _MatchBase
    actions: list


def canonicalize(url: str, online: bool = False) -> str:
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
