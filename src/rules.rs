//! Built-in canonicalization rules — the one file that grows.
//!
//! Rules are applied top-to-bottom; **all matching rules run** (not first-match).
//! For rule syntax see [`crate::engine`] and DESIGN.md.
//!
//! Note: `RewritePath` uses Rust `regex` replacement syntax — `$1`, `$2` for
//! capture groups (not Python's `\1`, `\2`).

use crate::engine::{
    extract_path, follow_redirect, keep_params, rewrite_host, rewrite_host_prefix, rewrite_path,
    rule, strip_params, unwrap_redirect_param, AnyHost, Host, HostGlob, Path, Rule,
};

/// Build the built-in rule list. (A function, not a `static`, because the DSL
/// constructors allocate `String`s — cheap, built once per process.)
pub fn rules() -> Vec<Rule> {
    vec![
        // --- Universal tracking params ---
        rule(
            AnyHost(),
            vec![strip_params(&[
                "fbclid",
                "sfnsn",
                "mibextid",
                "fb_*", // Facebook/Meta
                "utm_*",
                "wts*",
                "aem_*",
                "rdid",
                "_hsenc",
                "_hsmi",
                "mc_cid",
                "mc_eid",           // HubSpot/Mailchimp
                "mkt_tok",          // Marketo
                "_ke",              // Klaviyo
                "vgo_ee",           // ActiveCampaign
                "launch_app_store", // X (mobile app-store redirect hint)
            ])],
        ),
        // --- X (Twitter) — m.x.com → x.com directly (x.com has no www subdomain) ---
        rule(Host("m.x.com"), vec![rewrite_host("x.com")]),
        // --- Mobile subdomain normalization (m.*.com → www.*.com) ---
        rule(HostGlob("m.*.com"), vec![rewrite_host_prefix("m.", "www.")]),
        // --- LinkedIn ---
        rule(
            Host("www.linkedin.com") & Path("/learning-login/share"),
            vec![
                unwrap_redirect_param("redirect"),
                strip_params(&["account", "forceAccount", "trk", "shareId"]),
            ],
        ),
        rule(Host("www.linkedin.com"), vec![strip_params(&["u"])]),
        // --- Facebook ---
        rule(
            Host("www.facebook.com"),
            vec![keep_params(&["v", "story_fbid", "id", "set"])],
        ),
        rule(
            Host("www.facebook.com") & Path("/share/*"),
            vec![follow_redirect()],
        ),
        // --- Google Share (opaque short-links → follow redirect) ---
        rule(Host("share.google"), vec![follow_redirect()]),
        // --- YouTube ---
        rule(
            Host("www.youtube.com"),
            vec![keep_params(&["v", "t", "list", "index"])],
        ),
        // --- Amazon ---
        rule(Host("www.amazon.com"), vec![extract_path(r"/dp/[A-Z0-9]+")]),
        // --- InfoQ China ---
        rule(Host("www.infoq.cn"), vec![strip_params(&["*"])]),
        // --- Mailchimp campaign links (e= is per-subscriber recipient ID) ---
        rule(Host("mailchi.mp"), vec![strip_params(&["*"])]),
        // --- Medium (medium.com) ---
        // Articles: /pub-or-@user/verbose-slug-{12hexid} → /pub-or-@user/{12hexid}
        rule(
            Host("medium.com"),
            vec![rewrite_path(r"^(/[^/]+/).*-([0-9a-f]{12})$", r"$1$2")],
        ),
        // --- DEV.to (dev.to) ---
        // Articles: /user/verbose-slug-{4-8hexid} → /user/{4-8hexid}
        rule(
            Host("dev.to"),
            vec![rewrite_path(r"^(/[^/]+/).*-([0-9a-f]{4,8})$", r"$1$2")],
        ),
        // --- Hashnode (*.hashnode.dev) ---
        // Articles: /verbose-slug-{cuid} → /{cuid}  (CUIDs: ck + 22-24 lowercase alphanumeric)
        rule(
            HostGlob("*.hashnode.dev"),
            vec![rewrite_path(r"^/.*-(ck[a-z0-9]{22,24})$", r"/$1")],
        ),
    ]
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::engine::validate_rules;

    #[test]
    fn validate_rules_passes_builtin_rules() {
        // The actual RULES list must pass all validation checks.
        validate_rules(&rules()).expect("built-in rules must validate");
    }
}
