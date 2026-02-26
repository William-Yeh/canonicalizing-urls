import sys
from pathlib import Path as _Path
sys.path.insert(0, str(_Path(__file__).parent.parent / "scripts"))

from furl import furl as Furl
from canonicalize import (
    canonicalize, AnyHost, Host, Path, Rule, StripParams,
    UnwrapRedirectParam, RewriteHost, TrimPathSuffix, ExtractPath, StripFragment
)


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
