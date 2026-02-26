import sys
from pathlib import Path as _Path
sys.path.insert(0, str(_Path(__file__).parent.parent / "scripts"))

from furl import furl as Furl
from canonicalize import canonicalize, AnyHost, Host, Path, Rule, StripParams


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
