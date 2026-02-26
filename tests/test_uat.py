"""
User Acceptance Tests — end-to-end canonicalization driven by BEFORE→AFTER tables.

Each row is  (description, input_url, expected_url).
The table IS the specification: readable by humans, runnable by pytest.
"""
import sys
from pathlib import Path as _Path
sys.path.insert(0, str(_Path(__file__).parent.parent / "scripts"))

from unittest.mock import patch
import pytest
from engine import canonicalize
from rules import RULES


# ---------------------------------------------------------------------------
# Offline UAT table
# (description, before, after)
# ---------------------------------------------------------------------------

OFFLINE = [
    # ── Universal tracking params (fire on any domain) ─────────────────────

    ("fbclid stripped",
     "https://buzzorange.com/article/?fbclid=IwAR3abc&keep=1",
     "https://buzzorange.com/article/?keep=1"),

    ("utm_* stripped",
     "https://example.com/?utm_source=newsletter&utm_medium=email&utm_campaign=spring&keep=1",
     "https://example.com/?keep=1"),

    ("HubSpot _hsenc/_hsmi stripped",
     "https://example.com/?_hsenc=p2ANqtz&_hsmi=123&keep=1",
     "https://example.com/?keep=1"),

    ("Mailchimp mc_cid/mc_eid stripped",
     "https://example.com/?mc_cid=abc123&mc_eid=def456&keep=1",
     "https://example.com/?keep=1"),

    ("Marketo mkt_tok stripped",
     "https://example.com/?mkt_tok=NzY0LUJVS&keep=1",
     "https://example.com/?keep=1"),

    ("Klaviyo _ke stripped",
     "https://example.com/?_ke=abc&keep=1",
     "https://example.com/?keep=1"),

    ("ActiveCampaign vgo_ee stripped",
     "https://example.com/?vgo_ee=xyz&keep=1",
     "https://example.com/?keep=1"),

    ("Facebook share-meta params stripped",
     "https://example.com/?sfnsn=mo&mibextid=S65Db&fb_source=share&keep=1",
     "https://example.com/?keep=1"),

    # ── Mobile subdomain normalization (HostGlob m.*.com → www.*.com) ──────

    ("m.youtube.com → www.youtube.com",
     "https://m.youtube.com/watch?v=dQw4w9WgXcQ&si=TRACKING",
     "https://www.youtube.com/watch?v=dQw4w9WgXcQ"),

    ("m.facebook.com watch → www + keep video param",
     "https://m.facebook.com/watch/?ref=saved&v=1455603719491961",
     "https://www.facebook.com/watch/?v=1455603719491961"),

    ("m.twitter.com → www (generic rule, utm stripped)",
     "https://m.twitter.com/user/status/123?utm_campaign=foo",
     "https://www.twitter.com/user/status/123"),

    # ── LinkedIn ───────────────────────────────────────────────────────────

    ("LinkedIn learning-login: unwrap redirect, strip account/trk",
     ("https://www.linkedin.com/learning-login/share"
      "?account=352396234&forceAccount=false"
      "&redirect=https%3A%2F%2Fwww.linkedin.com%2Flearning%2Fcourse-name"),
     "https://www.linkedin.com/learning/course-name"),

    ("LinkedIn: strip u= subscriber param",
     "https://www.linkedin.com/learning/agile/course-intro?u=352396234",
     "https://www.linkedin.com/learning/agile/course-intro"),

    # ── Facebook ───────────────────────────────────────────────────────────

    ("Facebook story: mobile→www, keep story_fbid+id, strip wtsid",
     "https://m.facebook.com/story.php?story_fbid=9819635824716580&id=100000107794908&wtsid=rdr_0ROl",
     "https://www.facebook.com/story.php?story_fbid=9819635824716580&id=100000107794908"),

    ("Facebook video: mobile→www, keep v param, strip idorvanity",
     "https://m.facebook.com/kerwei.chien/videos/1371674297242802/?idorvanity=263406633764528",
     "https://www.facebook.com/kerwei.chien/videos/1371674297242802/"),

    # ── YouTube ────────────────────────────────────────────────────────────

    ("YouTube watch: keep v+t, strip si/pp noise",
     "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=30&si=TRACKING&pp=ygUDY2F0",
     "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=30"),

    ("YouTube playlist: keep v+list+index, strip si",
     "https://www.youtube.com/watch?v=abc&list=PL123&index=2&si=TRACKING",
     "https://www.youtube.com/watch?v=abc&list=PL123&index=2"),

    # ── Amazon ─────────────────────────────────────────────────────────────

    ("Amazon: extract /dp/<ASIN>, drop locale prefix and query",
     "https://www.amazon.com/-/zh_TW/Clean-Code/dp/0132350882/ref=sr_1_1?keywords=clean+code",
     "https://www.amazon.com/dp/0132350882"),

    # ── InfoQ China ────────────────────────────────────────────────────────

    ("InfoQ China: strip all query params",
     "https://www.infoq.cn/article/ABC123?ss=foo&source=bar",
     "https://www.infoq.cn/article/ABC123"),

    # ── Mailchimp ──────────────────────────────────────────────────────────

    ("Mailchimp: strip per-subscriber e= ID",
     "https://mailchi.mp/manny-li/063-17460036?e=143e81f948",
     "https://mailchi.mp/manny-li/063-17460036"),

    # ── X (Twitter) ────────────────────────────────────────────────────────

    ("X mobile: m.x.com → x.com (one step), strip launch_app_store",
     "https://m.x.com/mntruell/status/2026736314272591924?launch_app_store=true",
     "https://x.com/mntruell/status/2026736314272591924"),

    ("X: launch_app_store stripped on desktop domain too",
     "https://x.com/mntruell/status/2026736314272591924?launch_app_store=true&keep=1",
     "https://x.com/mntruell/status/2026736314272591924?keep=1"),

    # ── Medium ─────────────────────────────────────────────────────────────

    ("Medium publication article: strip slug, keep 12-char hex ID",
     ("https://medium.com/data-science-collective/"
      "the-complete-guide-to-ai-agent-memory-files-claude-md-agents-md-and-beyond-49ea0df5c5a9"),
     "https://medium.com/data-science-collective/49ea0df5c5a9"),

    ("Medium @user article: strip slug, keep 12-char hex ID",
     "https://medium.com/@john/my-article-title-49ea0df5c5a9",
     "https://medium.com/@john/49ea0df5c5a9"),

    ("Medium article + tracking params: slug stripped, params cleared",
     "https://medium.com/pub/slug-title-49ea0df5c5a9?source=newsletter&utm_source=twitter",
     "https://medium.com/pub/49ea0df5c5a9"),

    # ── DEV.to ─────────────────────────────────────────────────────────────

    ("DEV.to: strip slug, keep short hex ID",
     "https://dev.to/paulasantamaria/introduction-to-yaml-125f",
     "https://dev.to/paulasantamaria/125f"),

    # ── Hashnode ───────────────────────────────────────────────────────────

    ("Hashnode: strip slug, keep CUID",
     "https://johndoe.hashnode.dev/creating-your-first-react-app-ck5h4w9i50021c5s15ks21h2k",
     "https://johndoe.hashnode.dev/ck5h4w9i50021c5s15ks21h2k"),
]


# ---------------------------------------------------------------------------
# Online UAT table  (require HTTP — resolved via mock)
# (description, before, mocked_resolved_url, after)
# ---------------------------------------------------------------------------

ONLINE = [
    ("share.google → YouTube, strip si after redirect",
     "https://share.google/lw51K1njbxbifZ9ci",
     "https://www.youtube.com/watch?v=g5W5wvyexns&si=TRACKING",
     "https://www.youtube.com/watch?v=g5W5wvyexns"),

    ("Facebook /share/p/ → real post, strip rdid after redirect",
     "https://www.facebook.com/share/p/18GKaNgTxp/",
     "https://www.facebook.com/Page/posts/pfbid0abc?rdid=XYZ",
     "https://www.facebook.com/Page/posts/pfbid0abc"),
]


# ---------------------------------------------------------------------------
# Test runners
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("desc,before,after", OFFLINE, ids=[r[0] for r in OFFLINE])
def test_canonical(desc, before, after):
    assert canonicalize(before, rules=RULES) == after


@pytest.mark.parametrize("desc,before,resolved,after", ONLINE, ids=[r[0] for r in ONLINE])
def test_canonical_online(desc, before, resolved, after):
    with patch("engine._http_resolve", return_value=resolved):
        assert canonicalize(before, rules=RULES, online=True) == after
