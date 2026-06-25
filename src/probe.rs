//! Differential HTTP probe — discovers canonicalization rules for an unknown URL.
//!
//! Functional core: [`best_canonical`] / [`same_content`] / suggestion building
//! (pure, unit-tested). Imperative shell: [`fetch_signals`] (network + `tl` HTML
//! extraction) and [`probe`] (orchestration + stdout report).

use std::io::Read;

use crate::url_model::Url;
use regex::Regex;

/// Canonical signals extracted from a fetched page.
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct Signals {
    pub final_url: String,
    pub canonical: Option<String>,
    pub og_url: Option<String>,
    pub title: Option<String>,
}

/// Best canonical signal in priority order: `<link rel=canonical>`, then
/// `og:url`, then the final (post-redirect) URL if it differs from the original.
pub fn best_canonical(s: &Signals, original_url: &str) -> Option<String> {
    s.canonical
        .clone()
        .or_else(|| s.og_url.clone())
        .or_else(|| (s.final_url != original_url).then(|| s.final_url.clone()))
}

/// True if two signal sets describe the same content (any matching strong signal).
pub fn same_content(a: &Signals, b: &Signals) -> bool {
    if !a.final_url.is_empty() && a.final_url == b.final_url {
        return true;
    }
    if a.canonical.is_some() && a.canonical == b.canonical {
        return true;
    }
    if a.og_url.is_some() && a.og_url == b.og_url {
        return true;
    }
    if a.title.is_some() && a.title == b.title {
        return true;
    }
    false
}

/// Extract canonical signals from an HTML document (pure — no network).
pub fn extract_signals(final_url: &str, html: &str) -> Signals {
    let dom = match tl::parse(html, tl::ParserOptions::default()) {
        Ok(d) => d,
        Err(_) => {
            return Signals {
                final_url: final_url.to_string(),
                ..Default::default()
            }
        }
    };
    let parser = dom.parser();

    let attr = |selector: &str, attr: &str| -> Option<String> {
        dom.query_selector(selector)?
            .next()?
            .get(parser)?
            .as_tag()?
            .attributes()
            .get(attr)
            .flatten()
            .map(|b| b.as_utf8_str().into_owned())
    };

    let canonical = attr("link[rel=canonical]", "href");
    let og_url = attr(r#"meta[property="og:url"]"#, "content");
    let title = dom
        .query_selector("title")
        .and_then(|mut it| it.next())
        .and_then(|h| h.get(parser))
        .map(|n| n.inner_text(parser).trim().to_string())
        .filter(|t| !t.is_empty());

    Signals {
        final_url: final_url.to_string(),
        canonical,
        og_url,
        title,
    }
}

/// Fetch a URL and extract canonical signals (network shell).
fn fetch_signals(url: &str) -> Signals {
    let client = reqwest::blocking::Client::builder()
        .timeout(std::time::Duration::from_secs(10))
        .user_agent("Mozilla/5.0 (compatible; url-canonicalizer/1.0)")
        .build();
    let Ok(client) = client else {
        return Signals {
            final_url: url.to_string(),
            ..Default::default()
        };
    };
    match client.get(url).send() {
        Ok(mut resp) => {
            let final_url = resp.url().as_str().to_string();
            // Bounded read: the canonical signals live in <head>, so cap the body
            // to avoid an unbounded allocation from a hostile/huge response.
            const MAX_BODY: u64 = 512 * 1024;
            let mut buf = Vec::new();
            let _ = std::io::copy(&mut resp.by_ref().take(MAX_BODY), &mut buf);
            let html = String::from_utf8_lossy(&buf);
            extract_signals(&final_url, &html)
        }
        Err(e) => {
            eprintln!("warning: fetch failed for {url}: {e}");
            Signals {
                final_url: url.to_string(),
                ..Default::default()
            }
        }
    }
}

/// Differential HTTP probe: suggest canonicalization rules for `url`.
/// Prints a paste-ready `rule(...)` block to stdout. `resolve` is unused here
/// (signals carry the post-redirect URL) but kept for a uniform shell signature.
pub fn probe<R: Fn(&str) -> String>(url: &str, _resolve: &R) {
    eprintln!("Probing: {url}");
    let base = fetch_signals(url);
    let base_canonical = best_canonical(&base, url);
    eprintln!(
        "  Baseline canonical: {}",
        base_canonical.as_deref().unwrap_or("(none found)")
    );

    let Ok(original) = Url::parse(url) else {
        eprintln!("  invalid URL");
        return;
    };
    let mut suggestions: Vec<String> = Vec::new();

    // --- 1. Params ---
    if !original.args.is_empty() {
        let mut no_params = original.clone();
        no_params.clear_args();
        if same_content(&base, &fetch_signals(&no_params.to_string())) {
            eprintln!("  strip ALL params → same ✓");
            suggestions.push(r#"strip_params(&["*"])"#.to_string());
        } else {
            let mut strippable: Vec<String> = Vec::new();
            for (k, _) in &original.args {
                let mut test = original.clone();
                test.retain_args(|key, _| key != k);
                if same_content(&base, &fetch_signals(&test.to_string())) {
                    eprintln!("  strip {k:?} → same ✓");
                    strippable.push(k.clone());
                } else {
                    eprintln!("  strip {k:?} → different ✗");
                }
            }
            if !strippable.is_empty() {
                let quoted: Vec<String> = strippable.iter().map(|s| format!("{s:?}")).collect();
                suggestions.push(format!("strip_params(&[{}])", quoted.join(", ")));
            }
        }
    }

    // --- 2. Host ---
    if let Some(host) = original.host() {
        if let Some(rest) = host.strip_prefix("m.") {
            let www_host = format!("www.{rest}");
            let mut test = original.clone();
            test.set_host(&www_host);
            if same_content(&base, &fetch_signals(&test.to_string())) {
                eprintln!("  rewrite host → {www_host:?} ✓");
                suggestions.push(format!("rewrite_host({www_host:?})"));
            }
        }
    }

    // --- 3. Path ---
    let canonical_url = base_canonical
        .clone()
        .unwrap_or_else(|| base.final_url.clone());
    if !canonical_url.is_empty() && canonical_url != url {
        if let Ok(canon) = Url::parse(&canonical_url) {
            let canon_path = canon.path();
            let orig_path = original.path();
            if orig_path.ends_with(canon_path) && canon_path != orig_path {
                let re = Regex::new(r"[A-Z0-9]{6,}").unwrap();
                let pattern = re.replace_all(canon_path, "[A-Z0-9]+");
                eprintln!("  extract path {canon_path:?} ✓");
                suggestions.push(format!("extract_path(r{:?})", pattern.into_owned()));
            } else if orig_path.starts_with(canon_path) && canon_path != orig_path {
                let removed = &orig_path[canon_path.len()..];
                let n = removed.split('/').filter(|s| !s.is_empty()).count();
                eprintln!("  trim {n} path suffix segment(s) ✓");
                suggestions.push(format!("trim_path_suffix({n})"));
            }
        }
    }

    // --- Report (stdout = paste-ready rule block) ---
    let host = original.host().unwrap_or("");
    if suggestions.is_empty() {
        println!("// (no automatic suggestion — review manually)");
    } else {
        println!("rule(");
        println!("    Host({host:?}),");
        println!("    vec![{}],", suggestions.join(", "));
        println!("),");
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sig(
        final_url: &str,
        canonical: Option<&str>,
        og: Option<&str>,
        title: Option<&str>,
    ) -> Signals {
        Signals {
            final_url: final_url.to_string(),
            canonical: canonical.map(String::from),
            og_url: og.map(String::from),
            title: title.map(String::from),
        }
    }

    #[test]
    fn best_canonical_prefers_link_canonical() {
        let s = sig(
            "https://final",
            Some("https://canon"),
            Some("https://og"),
            None,
        );
        assert_eq!(
            best_canonical(&s, "https://orig").as_deref(),
            Some("https://canon")
        );
    }

    #[test]
    fn best_canonical_falls_back_to_og_then_final() {
        let s = sig("https://final", None, Some("https://og"), None);
        assert_eq!(
            best_canonical(&s, "https://orig").as_deref(),
            Some("https://og")
        );

        let s2 = sig("https://final", None, None, None);
        assert_eq!(
            best_canonical(&s2, "https://orig").as_deref(),
            Some("https://final")
        );
    }

    #[test]
    fn best_canonical_none_when_final_equals_original() {
        let s = sig("https://orig", None, None, None);
        assert_eq!(best_canonical(&s, "https://orig"), None);
    }

    #[test]
    fn same_content_matches_on_any_strong_signal() {
        let a = sig("https://a", Some("https://c"), None, None);
        let b = sig("https://b", Some("https://c"), None, None);
        assert!(same_content(&a, &b)); // canonical matches

        let c = sig("https://x", None, None, Some("Same Title"));
        let d = sig("https://y", None, None, Some("Same Title"));
        assert!(same_content(&c, &d)); // title matches

        let e = sig("https://x", None, None, Some("A"));
        let f = sig("https://y", None, None, Some("B"));
        assert!(!same_content(&e, &f));
    }

    #[test]
    fn extract_signals_reads_canonical_og_title() {
        let html = r#"<html><head>
            <title>  Hello World  </title>
            <link rel="canonical" href="https://example.com/canonical"/>
            <meta property="og:url" content="https://example.com/og"/>
            </head><body>x</body></html>"#;
        let s = extract_signals("https://example.com/final", html);
        assert_eq!(
            s.canonical.as_deref(),
            Some("https://example.com/canonical")
        );
        assert_eq!(s.og_url.as_deref(), Some("https://example.com/og"));
        assert_eq!(s.title.as_deref(), Some("Hello World"));
    }
}
