import sys
from pathlib import Path as _Path
sys.path.insert(0, str(_Path(__file__).parent.parent / "scripts"))

from unittest.mock import patch
from furl import furl as Furl
import pytest
from engine import (
    canonicalize, AnyHost, Host, Path, Rule, StripParams, KeepParams,
    UnwrapRedirectParam, RewriteHost, TrimPathSuffix, ExtractPath, StripFragment,
    FollowRedirect, validate_rules,
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
    with patch("engine._http_resolve", return_value=resolved):
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


def test_builtin_rules_youtube_mobile():
    url = "https://m.youtube.com/live/MpmrNaxW_O4?app=desktop&si=4gxzf3-pP2jh5yw_&t=274s"
    assert canonicalize(url) == "https://www.youtube.com/live/MpmrNaxW_O4?t=274s"


def test_builtin_rules_facebook_video():
    url = "https://m.facebook.com/kerwei.chien/videos/1371674297242802/?idorvanity=263406633764528"
    assert canonicalize(url) == "https://www.facebook.com/kerwei.chien/videos/1371674297242802/"


def test_builtin_rules_facebook_watch():
    url = "https://m.facebook.com/watch/?ref=saved&v=1455603719491961&_rdr"
    assert canonicalize(url) == "https://www.facebook.com/watch/?v=1455603719491961"


def test_builtin_rules_facebook_mobile():
    url = "https://m.facebook.com/story.php?story_fbid=9819635824716580&id=100000107794908&wtsid=rdr_0ROl"
    result = canonicalize(url)
    assert result.startswith("https://www.facebook.com/")
    assert "story_fbid=9819635824716580" in result  # content param preserved
    assert "id=100000107794908" in result            # content param preserved
    assert "wtsid" not in result


# ---------------------------------------------------------------------------
# KeepParams
# ---------------------------------------------------------------------------

def test_keep_params_strips_non_listed():
    f = Furl("https://www.youtube.com/watch?v=abc&si=XYZ&t=30")
    KeepParams(params=["v", "t"]).apply(f)
    assert "v=abc" in f.url
    assert "t=30" in f.url
    assert "si" not in f.url


def test_keep_params_empty_list_strips_all():
    f = Furl("https://example.com/?a=1&b=2")
    KeepParams(params=[]).apply(f)
    assert f.url == "https://example.com/"


def test_keep_params_glob_pattern():
    f = Furl("https://example.com/?v=1&v_extra=2&noise=x")
    KeepParams(params=["v*"]).apply(f)
    assert "v=1" in f.url
    assert "v_extra=2" in f.url
    assert "noise" not in f.url


def test_keep_params_pipeline():
    rules = [
        Rule(match=AnyHost(), actions=[StripParams(params=["utm_*"])]),
        Rule(match=Host("www.youtube.com"), actions=[KeepParams(params=["v", "t"])]),
    ]
    url = "https://www.youtube.com/watch?v=abc&t=30&si=XYZ&utm_source=foo"
    assert canonicalize(url, rules=rules) == "https://www.youtube.com/watch?v=abc&t=30"


# ---------------------------------------------------------------------------
# validate_rules lint check
# ---------------------------------------------------------------------------

def test_validate_rules_passes_strip_and_keep_same_host():
    # StripParams + KeepParams on same host: no conflict
    rules = [
        Rule(match=Host("x.com"), actions=[StripParams(params=["a"])]),
        Rule(match=Host("x.com"), actions=[KeepParams(params=["v"])]),
    ]
    validate_rules(rules)  # must not raise


def test_validate_rules_passes_keep_params_different_hosts():
    rules = [
        Rule(match=Host("x.com"), actions=[KeepParams(params=["v"])]),
        Rule(match=Host("y.com"), actions=[KeepParams(params=["id"])]),
    ]
    validate_rules(rules)  # must not raise


def test_validate_rules_conflict_same_host():
    rules = [
        Rule(match=Host("x.com"), actions=[KeepParams(params=["v"])]),
        Rule(match=Host("x.com"), actions=[KeepParams(params=["id"])]),
    ]
    with pytest.raises(ValueError, match="Conflicting KeepParams"):
        validate_rules(rules)


def test_validate_rules_conflict_any_host():
    rules = [
        Rule(match=AnyHost(), actions=[KeepParams(params=["v"])]),
        Rule(match=Host("x.com"), actions=[KeepParams(params=["id"])]),
    ]
    with pytest.raises(ValueError, match="Conflicting KeepParams"):
        validate_rules(rules)


def test_validate_rules_conflict_host_and_host_with_path():
    # Host("x.com") & Path("/a") overlaps with Host("x.com") & Path("/b")
    # (same host — we're conservative at host level)
    rules = [
        Rule(match=Host("x.com") & Path("/a/*"), actions=[KeepParams(params=["v"])]),
        Rule(match=Host("x.com") & Path("/b/*"), actions=[KeepParams(params=["id"])]),
    ]
    with pytest.raises(ValueError, match="Conflicting KeepParams"):
        validate_rules(rules)
