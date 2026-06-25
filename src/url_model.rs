//! Mutable, order-preserving URL model — the Rust replacement for Python's `furl`.
//!
//! `url::Url` parses to the WHATWG spec but does not preserve query-parameter
//! insertion order across mutation. Canonicalization rules (and the UAT table)
//! depend on exact output strings with original param order, so we hold the
//! query as an ordered `Vec<(String, String)>` and re-serialize it ourselves.

use url::Url as Inner;

/// An order-preserving, mutable URL.
///
/// The query string is parsed out into [`Url::args`] (an ordered list of
/// decoded `(key, value)` pairs) and the inner [`url::Url`] is kept query-free;
/// [`Url::to_string`] re-serializes the query from `args`, preserving insertion
/// order exactly the way Python's `furl` does.
#[derive(Debug, Clone)]
pub struct Url {
    inner: Inner, // authority + path + fragment; query always cleared
    /// Query parameters in insertion order, percent-decoded.
    pub args: Vec<(String, String)>,
}

impl Url {
    /// Parse a URL string. Returns `Err` on a hard parse failure.
    pub fn parse(s: &str) -> Result<Self, url::ParseError> {
        let parsed = Inner::parse(s)?;
        let args: Vec<(String, String)> = parsed
            .query_pairs()
            .map(|(k, v)| (k.into_owned(), v.into_owned()))
            .collect();
        let mut inner = parsed;
        inner.set_query(None);
        Ok(Self { inner, args })
    }

    /// Host component, if present.
    pub fn host(&self) -> Option<&str> {
        self.inner.host_str()
    }

    /// Replace the host, leaving scheme/path/query/fragment intact.
    pub fn set_host(&mut self, host: &str) {
        // url::Url::set_host validates; canonicalization hosts are always valid.
        let _ = self.inner.set_host(Some(host));
    }

    /// Path component (always begins with `/` for hierarchical URLs).
    pub fn path(&self) -> &str {
        self.inner.path()
    }

    /// Replace the path.
    pub fn set_path(&mut self, path: &str) {
        self.inner.set_path(path);
    }

    /// Replace the path and drop the query + fragment.
    ///
    /// Rewriting a URL's canonical path (extract/rewrite/trim) yields a fresh
    /// identity, so any tracking query params and `#fragment` no longer apply.
    /// Centralizes that invariant for all path-rewriting actions.
    pub fn set_path_canonical(&mut self, path: &str) {
        self.inner.set_path(path);
        self.args.clear();
        self.inner.set_fragment(None);
    }

    /// First value for `key`, percent-decoded (matches furl `f.args.get`).
    pub fn get_arg(&self, key: &str) -> Option<&str> {
        self.args
            .iter()
            .find(|(k, _)| k == key)
            .map(|(_, v)| v.as_str())
    }

    /// Keep only the args for which `pred(key, value)` is true (order-preserving).
    pub fn retain_args<F: FnMut(&str, &str) -> bool>(&mut self, mut pred: F) {
        self.args.retain(|(k, v)| pred(k, v));
    }

    /// Remove all query parameters.
    pub fn clear_args(&mut self) {
        self.args.clear();
    }

    /// Remove the last `n` non-empty path segments, dropping query + fragment
    /// (furl `TrimPathSuffix` — a path trim yields a fresh canonical identity).
    pub fn trim_path_segments(&mut self, n: usize) {
        if n == 0 {
            return;
        }
        let segments: Vec<&str> = self
            .inner
            .path()
            .split('/')
            .filter(|s| !s.is_empty())
            .collect();
        let keep = segments.len().saturating_sub(n);
        let new_path = format!("/{}", segments[..keep].join("/"));
        self.set_path_canonical(&new_path);
    }

    /// Drop the `#fragment`.
    pub fn remove_fragment(&mut self) {
        self.inner.set_fragment(None);
    }
}

impl std::fmt::Display for Url {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        // Re-attach the query from `args` (order-preserving) without mutating self.
        if self.args.is_empty() {
            return write!(f, "{}", self.inner);
        }
        let mut out = self.inner.clone();
        let mut serializer = out.query_pairs_mut();
        serializer.clear();
        for (k, v) in &self.args {
            serializer.append_pair(k, v);
        }
        drop(serializer);
        write!(f, "{out}")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // --- parse / round-trip (the UAT byte-identity foundation) ---

    #[test]
    fn roundtrips_simple_url() {
        let u = Url::parse("https://example.com/path?a=1&b=2#frag").unwrap();
        assert_eq!(u.to_string(), "https://example.com/path?a=1&b=2#frag");
    }

    #[test]
    fn roundtrips_no_query_no_fragment() {
        let u = Url::parse("https://example.com/path").unwrap();
        assert_eq!(u.to_string(), "https://example.com/path");
    }

    #[test]
    fn empty_query_serializes_without_question_mark() {
        // furl: clearing all args drops the "?" entirely.
        let mut u = Url::parse("https://example.com/?a=1").unwrap();
        u.clear_args();
        assert_eq!(u.to_string(), "https://example.com/");
    }

    // --- ordered args (insertion order preserved) ---

    #[test]
    fn args_preserve_insertion_order() {
        let u = Url::parse("https://x.com/?z=1&a=2&m=3").unwrap();
        let keys: Vec<&str> = u.args.iter().map(|(k, _)| k.as_str()).collect();
        assert_eq!(keys, vec!["z", "a", "m"]);
    }

    #[test]
    fn removing_middle_arg_keeps_others_in_order() {
        let mut u = Url::parse("https://x.com/?a=1&b=2&c=3").unwrap();
        u.retain_args(|k, _| k != "b");
        assert_eq!(u.to_string(), "https://x.com/?a=1&c=3");
    }

    #[test]
    fn get_arg_returns_first_value() {
        let u = Url::parse("https://x.com/?redirect=https%3A%2F%2Ffoo.com%2Fc").unwrap();
        assert_eq!(u.get_arg("redirect"), Some("https://foo.com/c"));
    }

    // --- host get/set ---

    #[test]
    fn host_accessor() {
        let u = Url::parse("https://m.facebook.com/story.php?id=123").unwrap();
        assert_eq!(u.host(), Some("m.facebook.com"));
    }

    #[test]
    fn set_host_rewrites_authority() {
        let mut u = Url::parse("https://m.facebook.com/story.php?id=123").unwrap();
        u.set_host("www.facebook.com");
        assert_eq!(u.to_string(), "https://www.facebook.com/story.php?id=123");
    }

    // --- path get/set + segment trim ---

    #[test]
    fn path_accessor() {
        let u = Url::parse("https://www.amazon.com/-/zh_TW/Clean-Code/dp/0132350882").unwrap();
        assert_eq!(u.path(), "/-/zh_TW/Clean-Code/dp/0132350882");
    }

    #[test]
    fn set_path_replaces_path() {
        let mut u = Url::parse("https://www.amazon.com/-/zh_TW/Clean-Code/dp/0132350882").unwrap();
        u.set_path("/dp/0132350882");
        assert_eq!(u.to_string(), "https://www.amazon.com/dp/0132350882");
    }

    #[test]
    fn trim_path_suffix_removes_trailing_segments() {
        // furl: TrimPathSuffix(n=1) on /learning/agile/course → /learning/agile
        let mut u =
            Url::parse("https://www.linkedin.com/learning/agile/course-introduction").unwrap();
        u.trim_path_segments(1);
        assert_eq!(u.path(), "/learning/agile");
    }

    #[test]
    fn trim_path_suffix_zero_is_noop() {
        let mut u = Url::parse("https://example.com/learning/agile/course").unwrap();
        u.trim_path_segments(0);
        assert_eq!(u.path(), "/learning/agile/course");
    }

    // --- fragment ---

    #[test]
    fn alphanumeric_arg_values_stay_literal() {
        // UAT values are IDs/hex/slugs — must NOT be percent-mangled on re-serialize.
        let mut u = Url::parse("https://www.youtube.com/watch?v=dQw4w9WgXcQ&si=X&t=30").unwrap();
        u.retain_args(|k, _| k != "si");
        assert_eq!(
            u.to_string(),
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=30"
        );
    }

    #[test]
    fn remove_fragment() {
        let mut u = Url::parse("https://example.com/page#section-2").unwrap();
        u.remove_fragment();
        assert_eq!(u.to_string(), "https://example.com/page");
    }
}
