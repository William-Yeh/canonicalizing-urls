"""Built-in canonicalization rules.

Add new rules here. Rules are applied top-to-bottom; all matching rules run.
For rule syntax, see engine.py and DESIGN.md.
"""

from engine import (
    AnyHost, ExtractPath, FollowRedirect, Host, KeepParams, Path,
    RewriteHost, Rule, StripParams, UnwrapRedirectParam, validate_rules,
)

RULES: list = [
    # --- Universal tracking params ---
    Rule(
        match=AnyHost(),
        actions=[StripParams(params=[
            "fbclid", "sfnsn", "mibextid", "fb_*",    # Facebook/Meta
            "utm_*", "wts*", "aem_*", "rdid",
            "_hsenc", "_hsmi", "mc_cid", "mc_eid",   # HubSpot/Mailchimp
            "mkt_tok",                               # Marketo
            "_ke",                                   # Klaviyo
            "vgo_ee",                                # ActiveCampaign
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
        match=Host("www.facebook.com"),
        actions=[KeepParams(params=["v", "story_fbid", "id", "set"])],
    ),
    Rule(
        match=Host("www.facebook.com") & Path("/share/*"),
        actions=[FollowRedirect()],
    ),

    # --- Google Share (opaque short-links → follow redirect) ---
    Rule(
        match=Host("share.google"),
        actions=[FollowRedirect()],
    ),

    # --- YouTube ---
    Rule(
        match=Host("m.youtube.com"),
        actions=[RewriteHost("www.youtube.com")],
    ),
    Rule(
        match=Host("www.youtube.com"),
        actions=[KeepParams(params=["v", "t", "list", "index"])],
    ),

    # --- Amazon ---
    Rule(
        match=Host("www.amazon.com"),
        actions=[ExtractPath(pattern=r"/dp/[A-Z0-9]+")],
    ),

    # --- InfoQ China ---
    Rule(
        match=Host("www.infoq.cn"),
        actions=[StripParams(params=["*"])],
    ),

    # --- Mailchimp campaign links (e= is per-subscriber recipient ID) ---
    Rule(
        match=Host("mailchi.mp"),
        actions=[StripParams(params=["*"])],
    ),
]

validate_rules(RULES)  # bootstrap lint: raises ValueError on conflicting KeepParams
