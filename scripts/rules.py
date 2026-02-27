"""Built-in canonicalization rules.

Add new rules here. Rules are applied top-to-bottom; all matching rules run.
For rule syntax, see engine.py and DESIGN.md.
"""

from engine import (
    AnyHost, ExtractPath, FollowRedirect, Host, Path,
    RewriteHost, Rule, StripParams, UnwrapRedirectParam,
)

RULES: list = [
    # --- Universal tracking params ---
    Rule(
        match=AnyHost(),
        actions=[StripParams(params=[
            "fbclid", "utm_*", "wts*", "aem_*", "rdid",
            "_hsenc", "_hsmi", "mc_cid", "mc_eid",   # HubSpot/Mailchimp
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
        match=Host("www.facebook.com") & Path("/share/*"),
        actions=[FollowRedirect()],
    ),

    # --- Amazon ---
    Rule(
        match=Host("www.amazon.com"),
        actions=[ExtractPath(pattern=r"/dp/[A-Z0-9]+")],
    ),
]
