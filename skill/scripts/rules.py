"""Built-in canonicalization rules.

Add new rules here. Rules are applied top-to-bottom; all matching rules run.
For rule syntax, see engine.py and DESIGN.md.
"""

from engine import (
    AnyHost, ExtractPath, FollowRedirect, Host, HostGlob, KeepParams, Path,
    RewriteHost, RewriteHostPrefix, RewritePath, Rule, StripParams,
    UnwrapRedirectParam, validate_rules,
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
            "launch_app_store",                      # X (mobile app-store redirect hint)
        ])],
    ),

    # --- X (Twitter) — m.x.com → x.com directly (x.com has no www subdomain) ---
    Rule(
        match=Host("m.x.com"),
        actions=[RewriteHost("x.com")],
    ),

    # --- Mobile subdomain normalization (m.*.com → www.*.com) ---
    Rule(
        match=HostGlob("m.*.com"),
        actions=[RewriteHostPrefix("m.", "www.")],
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

    # --- Medium (medium.com) ---
    # Articles: /pub-or-@user/verbose-slug-{12hexid} → /pub-or-@user/{12hexid}
    Rule(
        match=Host("medium.com"),
        actions=[RewritePath(
            pattern=r"^(/[^/]+/).*-([0-9a-f]{12})$",
            replacement=r"\1\2",
        )],
    ),

    # --- DEV.to (dev.to) ---
    # Articles: /user/verbose-slug-{4-8hexid} → /user/{4-8hexid}
    Rule(
        match=Host("dev.to"),
        actions=[RewritePath(
            pattern=r"^(/[^/]+/).*-([0-9a-f]{4,8})$",
            replacement=r"\1\2",
        )],
    ),

    # --- Hashnode (*.hashnode.dev) ---
    # Articles: /verbose-slug-{cuid} → /{cuid}  (CUIDs: ck + 22-24 lowercase alphanumeric)
    Rule(
        match=HostGlob("*.hashnode.dev"),
        actions=[RewritePath(
            pattern=r"^/.*-(ck[a-z0-9]{22,24})$",
            replacement=r"/\1",
        )],
    ),
]

validate_rules(RULES)  # bootstrap lint: raises ValueError on conflicting KeepParams
