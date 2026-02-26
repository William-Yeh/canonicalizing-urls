import sys
from pathlib import Path as _Path
sys.path.insert(0, str(_Path(__file__).parent.parent / "scripts"))

from unittest.mock import patch
from furl import furl as Furl
from canonicalize import (
    canonicalize, AnyHost, Host, Path, Rule, StripParams,
    UnwrapRedirectParam, RewriteHost, TrimPathSuffix, ExtractPath, StripFragment,
    FollowRedirect,
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


def test_trim_path_suffix_n0_is_noop():
    f = Furl("https://example.com/learning/agile/course")
    TrimPathSuffix(n=0).apply(f)
    assert f.url == "https://example.com/learning/agile/course"


def test_strip_fragment():
    f = Furl("https://example.com/page#section-2")
    StripFragment().apply(f)
    assert f.url == "https://example.com/page"


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
