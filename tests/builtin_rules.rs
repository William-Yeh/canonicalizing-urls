//! Built-in RULES integration tests — ported 1:1 from the `test_builtin_rules_*`
//! and `*_online` cases in Python `tests/test_canonicalize.py`. These exercise
//! the real `rules()` list against inputs distinct from the UAT table.

use canonicalize::engine::canonicalize;
use canonicalize::rules::rules;

fn no_net(_url: &str) -> String {
    unreachable!("offline test must not hit the network")
}

fn canon(url: &str) -> String {
    canonicalize(url, &rules(), false, &no_net)
}

#[test]
fn strip_fbclid() {
    assert_eq!(
        canon("https://buzzorange.com/techorange/2025/02/19/ai/?fbclid=XYZ&aem_abc=1"),
        "https://buzzorange.com/techorange/2025/02/19/ai/"
    );
}

#[test]
fn strip_esp_recipient_params() {
    assert_eq!(
        canon("https://example.com/article?mkt_tok=ABC&_ke=def&vgo_ee=ghi&keep=1"),
        "https://example.com/article?keep=1"
    );
}

#[test]
fn strip_facebook_share_params() {
    assert_eq!(
        canon("https://gipi.tw/llm-benchmark-in-software-engineering/?fbclid=XYZ&sfnsn=mo&mibextid=abc&fb_source=share&fb_action_ids=123&keep=1"),
        "https://gipi.tw/llm-benchmark-in-software-engineering/?keep=1"
    );
}

#[test]
fn mailchimp_strip_e() {
    assert_eq!(
        canon("https://mailchi.mp/manny-li/063-17460036?e=143e81f948&fbclid=XYZ&sfnsn=mo"),
        "https://mailchi.mp/manny-li/063-17460036"
    );
}

#[test]
fn linkedin_strip_u() {
    assert_eq!(
        canon("https://www.linkedin.com/learning/agile/course-introduction?u=352396234"),
        "https://www.linkedin.com/learning/agile/course-introduction"
    );
}

#[test]
fn amazon_extract_dp() {
    assert_eq!(
        canon("https://www.amazon.com/-/zh_TW/Clean-Code/dp/0132350882"),
        "https://www.amazon.com/dp/0132350882"
    );
}

#[test]
fn youtube_mobile() {
    assert_eq!(
        canon("https://m.youtube.com/live/MpmrNaxW_O4?app=desktop&si=4gxzf3-pP2jh5yw_&t=274s"),
        "https://www.youtube.com/live/MpmrNaxW_O4?t=274s"
    );
}

#[test]
fn facebook_video() {
    assert_eq!(
        canon("https://m.facebook.com/kerwei.chien/videos/1371674297242802/?idorvanity=263406633764528"),
        "https://www.facebook.com/kerwei.chien/videos/1371674297242802/"
    );
}

#[test]
fn facebook_watch() {
    assert_eq!(
        canon("https://m.facebook.com/watch/?ref=saved&v=1455603719491961&_rdr"),
        "https://www.facebook.com/watch/?v=1455603719491961"
    );
}

#[test]
fn facebook_mobile() {
    let result = canon("https://m.facebook.com/story.php?story_fbid=9819635824716580&id=100000107794908&wtsid=rdr_0ROl");
    assert!(result.starts_with("https://www.facebook.com/"));
    assert!(result.contains("story_fbid=9819635824716580"));
    assert!(result.contains("id=100000107794908"));
    assert!(!result.contains("wtsid"));
}

#[test]
fn share_google_online() {
    let resolver =
        |_url: &str| "https://www.youtube.com/watch?v=g5W5wvyexns&si=TRACKING".to_string();
    let result = canonicalize(
        "https://share.google/lw51K1njbxbifZ9ci",
        &rules(),
        true,
        &resolver,
    );
    assert_eq!(result, "https://www.youtube.com/watch?v=g5W5wvyexns");
}

#[test]
fn medium_publication() {
    assert_eq!(
        canon("https://medium.com/data-science-collective/the-complete-guide-to-ai-agent-memory-files-claude-md-agents-md-and-beyond-49ea0df5c5a9"),
        "https://medium.com/data-science-collective/49ea0df5c5a9"
    );
}

#[test]
fn medium_user() {
    assert_eq!(
        canon("https://medium.com/@john/my-article-title-49ea0df5c5a9"),
        "https://medium.com/@john/49ea0df5c5a9"
    );
}

#[test]
fn devto() {
    assert_eq!(
        canon("https://dev.to/paulasantamaria/introduction-to-yaml-125f"),
        "https://dev.to/paulasantamaria/125f"
    );
}

#[test]
fn hashnode() {
    assert_eq!(
        canon(
            "https://johndoe.hashnode.dev/creating-your-first-react-app-ck5h4w9i50021c5s15ks21h2k"
        ),
        "https://johndoe.hashnode.dev/ck5h4w9i50021c5s15ks21h2k"
    );
}
