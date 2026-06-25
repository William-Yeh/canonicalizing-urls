//! URL canonicalization engine — match/action primitives, pipeline, rule index.

use crate::url_model::Url;
use regex::Regex;

// ---------------------------------------------------------------------------
// Match primitives  (ADT — `enum` + `match`, compiler-checked exhaustiveness)
// ---------------------------------------------------------------------------

/// A condition deciding whether a [`Rule`] applies to a URL.
#[derive(Debug, Clone)]
pub enum Matcher {
    /// Matches every URL.
    AnyHost,
    /// Exact host match.
    Host(String),
    /// Host glob — `*`/`?` wildcards only, e.g. `"m.*.com"`.
    HostGlob(String),
    /// Path glob — `*`/`?` wildcards only, e.g. `"/share/*"`.
    Path(String),
    /// Both sub-conditions must match.
    And(Box<Matcher>, Box<Matcher>),
}

impl Matcher {
    /// True if this matcher applies to `url`.
    pub fn matches(&self, url: &Url) -> bool {
        match self {
            Matcher::AnyHost => true,
            Matcher::Host(h) => url.host() == Some(h.as_str()),
            Matcher::HostGlob(pat) => url.host().is_some_and(|h| glob_match(pat, h)),
            Matcher::Path(pat) => glob_match(pat, url.path()),
            Matcher::And(l, r) => l.matches(url) && r.matches(url),
        }
    }
}

/// Enables the Python-like `Host("x") & Path("/y")` DSL.
impl std::ops::BitAnd for Matcher {
    type Output = Matcher;
    fn bitand(self, rhs: Matcher) -> Matcher {
        Matcher::And(Box::new(self), Box::new(rhs))
    }
}

// Constructor functions preserve the `Host(...)`, `Path(...)` call-site DSL.
#[allow(non_snake_case)]
pub fn AnyHost() -> Matcher {
    Matcher::AnyHost
}
#[allow(non_snake_case)]
pub fn Host(host: &str) -> Matcher {
    Matcher::Host(host.to_string())
}
#[allow(non_snake_case)]
pub fn HostGlob(pattern: &str) -> Matcher {
    Matcher::HostGlob(pattern.to_string())
}
#[allow(non_snake_case)]
pub fn Path(pattern: &str) -> Matcher {
    Matcher::Path(pattern.to_string())
}

// ---------------------------------------------------------------------------
// Action primitives
// ---------------------------------------------------------------------------

/// A transformation applied to a URL when its [`Rule`] matches.
#[derive(Debug, Clone)]
pub enum Action {
    /// Remove query params matching any pattern (denylist).
    StripParams(Vec<String>),
    /// Remove all query params EXCEPT those matching patterns (allowlist).
    KeepParams(Vec<String>),
    /// Decode a URL-encoded redirect param → returns a new URL string.
    UnwrapRedirectParam(String),
    /// Replace the host.
    RewriteHost(String),
    /// Replace a host prefix (e.g. `m.` → `www.`). No-op if absent.
    RewriteHostPrefix { old: String, new: String },
    /// Remove `n` trailing path segments and clear query+fragment.
    TrimPathSuffix(usize),
    /// Keep only the first regex match in the path; clear query+fragment.
    ExtractPath(String),
    /// Regex substitution on the path; clears query+fragment if changed.
    RewritePath {
        pattern: String,
        replacement: String,
    },
    /// Remove the `#fragment`.
    StripFragment,
    /// Resolve via HTTP and restart the pipeline. Requires `online`.
    FollowRedirect,
}

// Constructor functions for the DSL.
pub fn strip_params(params: &[&str]) -> Action {
    Action::StripParams(params.iter().map(|s| s.to_string()).collect())
}
pub fn keep_params(params: &[&str]) -> Action {
    Action::KeepParams(params.iter().map(|s| s.to_string()).collect())
}
pub fn unwrap_redirect_param(key: &str) -> Action {
    Action::UnwrapRedirectParam(key.to_string())
}
pub fn rewrite_host(host: &str) -> Action {
    Action::RewriteHost(host.to_string())
}
pub fn rewrite_host_prefix(old: &str, new: &str) -> Action {
    Action::RewriteHostPrefix {
        old: old.to_string(),
        new: new.to_string(),
    }
}
pub fn trim_path_suffix(n: usize) -> Action {
    Action::TrimPathSuffix(n)
}
pub fn extract_path(pattern: &str) -> Action {
    Action::ExtractPath(pattern.to_string())
}
pub fn rewrite_path(pattern: &str, replacement: &str) -> Action {
    Action::RewritePath {
        pattern: pattern.to_string(),
        replacement: replacement.to_string(),
    }
}
pub fn strip_fragment() -> Action {
    Action::StripFragment
}
pub fn follow_redirect() -> Action {
    Action::FollowRedirect
}

/// Apply an action to `url` in place.
///
/// Returns `Some(new_url)` only for [`Action::UnwrapRedirectParam`] (the caller
/// switches context to the unwrapped URL); all other actions mutate `url` and
/// return `None`. [`Action::FollowRedirect`] is a no-op here — it is handled by
/// the pipeline, which needs the network and recursion.
pub fn apply(action: &Action, url: &mut Url) -> Option<String> {
    match action {
        Action::StripParams(patterns) => {
            url.retain_args(|k, _| !param_matches(patterns, k));
            None
        }
        Action::KeepParams(patterns) => {
            url.retain_args(|k, _| param_matches(patterns, k));
            None
        }
        Action::UnwrapRedirectParam(key) => url.get_arg(key).map(|v| v.to_string()),
        Action::RewriteHost(host) => {
            url.set_host(host);
            None
        }
        Action::RewriteHostPrefix { old, new } => {
            if let Some(rest) = url.host().and_then(|h| h.strip_prefix(old.as_str())) {
                url.set_host(&format!("{new}{rest}"));
            }
            None
        }
        Action::TrimPathSuffix(n) => {
            url.trim_path_segments(*n); // drops query+fragment internally
            None
        }
        Action::ExtractPath(pattern) => {
            let re = compiled(pattern);
            if let Some(m) = re.find(url.path()) {
                let matched = m.as_str().to_string();
                url.set_path_canonical(&matched);
            }
            None
        }
        Action::RewritePath {
            pattern,
            replacement,
        } => {
            let re = compiled(pattern);
            let path = url.path().to_string();
            let new_path = re.replace(&path, replacement.as_str());
            if new_path != path {
                let new_path = new_path.into_owned();
                url.set_path_canonical(&new_path);
            }
            None
        }
        Action::StripFragment => {
            url.remove_fragment();
            None
        }
        Action::FollowRedirect => None, // handled by the pipeline
    }
}

// ---------------------------------------------------------------------------
// Compiled-regex cache  (compile once per pattern, reuse across calls)
// ---------------------------------------------------------------------------

use std::sync::{Arc, Mutex, OnceLock};

/// Process-wide memo of compiled regexes, keyed by source string.
///
/// Returns a shared `Arc<Regex>`, **not a clone of the `Regex`**. This is the
/// crucial detail: a `regex::Regex` lazily builds an internal DFA cache on first
/// use, *per instance*, so calling `.find()` on a freshly-cloned `Regex` rebuilds
/// that cache every time (tens of µs). Sharing one instance via `Arc` keeps its
/// warm cache, so repeated matches cost only the match itself (~µs).
fn compiled(src: &str) -> Arc<Regex> {
    static CACHE: OnceLock<Mutex<HashMap<String, Arc<Regex>>>> = OnceLock::new();
    let cache = CACHE.get_or_init(|| Mutex::new(HashMap::new()));
    // Scope the lock so it is NOT held during the (slow) Regex::new on a miss.
    {
        let map = cache.lock().unwrap();
        if let Some(re) = map.get(src) {
            return Arc::clone(re);
        }
    }
    let re = Arc::new(Regex::new(src).unwrap_or_else(|e| panic!("invalid regex {src:?}: {e}")));
    cache
        .lock()
        .unwrap()
        .insert(src.to_string(), Arc::clone(&re));
    re
}

/// Memoized `RegexSet` (the merged HostGlob DFA), keyed by the joined patterns.
///
/// `RegexSet::new` compiles the whole multi-pattern automaton — the most
/// expensive single step in index construction — so this caches the *compile*:
/// the set is built at most once even though `RuleIndex` is rebuilt on every
/// top-level `canonicalize()` call. The per-call cost that remains is the key
/// `join` + a `HashMap` lookup + a cheap `RegexSet` clone (internal `Arc`),
/// which is negligible (~µs) next to the compile it avoids.
fn compiled_set(patterns: &[String]) -> RegexSet {
    static CACHE: OnceLock<Mutex<HashMap<String, RegexSet>>> = OnceLock::new();
    let cache = CACHE.get_or_init(|| Mutex::new(HashMap::new()));
    let key = patterns.join("\u{1}"); // unit separator — cannot appear in a glob
    {
        let map = cache.lock().unwrap();
        if let Some(set) = map.get(&key) {
            return set.clone();
        }
    }
    let set = RegexSet::new(patterns).unwrap_or_else(|e| panic!("invalid HostGlob set: {e}"));
    cache.lock().unwrap().insert(key, set.clone());
    set
}

// ---------------------------------------------------------------------------
// Pattern matching: glob (`*`/`?`) and param patterns
// ---------------------------------------------------------------------------

/// Translate a glob to an anchored regex source (no flags).
///
/// Supports only `*` → `.*` and `?` → `.`; every other character is a literal
/// and is regex-escaped. This is a deliberate subset of Python's
/// `fnmatch.translate` — `[...]` character classes are **not** supported (a `[`
/// is literal), because no rule needs them. The current host/path globs use
/// only `*`/`?`.
///
/// Literal chars are escaped in place (no per-char `String` allocation).
fn glob_to_regex_src(pattern: &str) -> String {
    let mut out = String::with_capacity(pattern.len() + 4);
    out.push('^');
    for ch in pattern.chars() {
        match ch {
            '*' => out.push_str(".*"),
            '?' => out.push('.'),
            // Escape the regex metacharacters; everything else passes through.
            c => {
                if matches!(
                    c,
                    '\\' | '.' | '+' | '(' | ')' | '|' | '[' | ']' | '{' | '}' | '^' | '$' | '#'
                ) {
                    out.push('\\');
                }
                out.push(c);
            }
        }
    }
    out.push('$');
    out
}

/// True if `text` matches the glob `pattern` (`*`/`?`, case-insensitive).
///
/// Memoized by the **raw glob pattern**, so the glob→regex translation (a
/// per-char escape loop) runs once per pattern, not per call. The shared
/// `Arc<Regex>` keeps its warm internal cache across calls (see [`compiled`]).
fn glob_match(pattern: &str, text: &str) -> bool {
    static CACHE: OnceLock<Mutex<HashMap<String, Arc<Regex>>>> = OnceLock::new();
    let cache = CACHE.get_or_init(|| Mutex::new(HashMap::new()));
    let re = {
        let mut map = cache.lock().unwrap();
        if let Some(re) = map.get(pattern) {
            Arc::clone(re)
        } else {
            let src = format!("(?i){}", glob_to_regex_src(pattern));
            let re = Arc::new(
                Regex::new(&src).unwrap_or_else(|e| panic!("invalid glob {pattern:?}: {e}")),
            );
            map.insert(pattern.to_string(), Arc::clone(&re));
            re
        }
    };
    re.is_match(text)
}

/// True if `name` matches any pattern. Pattern syntax:
/// `"*"` = all; `/regex/` = slash-delimited regex; `*`/`?` present = glob;
/// otherwise literal equality.
fn param_matches(patterns: &[String], name: &str) -> bool {
    for p in patterns {
        if p == "*" {
            return true;
        }
        if p.len() >= 2 && p.starts_with('/') && p.ends_with('/') {
            let inner = &p[1..p.len() - 1];
            if compiled(inner).is_match(name) {
                return true;
            }
        } else if p.contains('*') || p.contains('?') {
            if glob_match(p, name) {
                return true;
            }
        } else if name == p {
            return true;
        }
    }
    false
}

/// A match condition plus the ordered actions to apply when it matches.
#[derive(Debug, Clone)]
pub struct Rule {
    pub matcher: Matcher,
    pub actions: Vec<Action>,
}

/// Construct a [`Rule`] — preserves the `rule(matcher, vec![..])` DSL.
pub fn rule(matcher: Matcher, actions: Vec<Action>) -> Rule {
    Rule { matcher, actions }
}

// ---------------------------------------------------------------------------
// Rule index — host-keyed pre-filter (the performance core)
// ---------------------------------------------------------------------------

use regex::RegexSet;
use std::collections::{HashMap, HashSet};

/// Host-keyed index reducing per-rule match checks in [`canonicalize`].
///
/// Three buckets, built once:
/// - `universal` — `AnyHost` (and unknown matchers, conservatively): always candidates.
/// - `exact` — `Host("x.com")`: O(1) `HashMap` lookup by hostname.
/// - `glob_set` — all `HostGlob` patterns merged into **one** [`RegexSet`] (a single
///   multi-pattern DFA). `set.matches(host)` runs the host through one combined
///   automaton in O(L) and yields the index of every matching pattern — the honest
///   expression of the "merge all globs into one DFA" optimization. `glob_rule_ids`
///   maps each set-match index back to its rule index.
pub struct RuleIndex {
    universal: Vec<usize>,
    exact: HashMap<String, Vec<usize>>,
    glob_set: Option<RegexSet>,
    glob_rule_ids: Vec<usize>,
}

impl RuleIndex {
    /// Build the index from the full rule list (O(R), once per top-level call).
    pub fn new(rules: &[Rule]) -> Self {
        let mut universal = Vec::new();
        let mut exact: HashMap<String, Vec<usize>> = HashMap::new();
        let mut glob_patterns: Vec<String> = Vec::new();
        let mut glob_rule_ids: Vec<usize> = Vec::new();

        for (i, r) in rules.iter().enumerate() {
            match &r.matcher {
                Matcher::AnyHost => universal.push(i),
                Matcher::HostGlob(pat) => {
                    glob_patterns.push(format!("(?i){}", glob_to_regex_src(pat)));
                    glob_rule_ids.push(i);
                }
                other => {
                    let hosts = hosts_from_matcher(other);
                    if hosts.contains("*") {
                        universal.push(i); // conservative: unknown matcher type
                    } else {
                        for h in hosts {
                            exact.entry(h).or_default().push(i);
                        }
                    }
                }
            }
        }

        let glob_set = if glob_patterns.is_empty() {
            None
        } else {
            Some(compiled_set(&glob_patterns))
        };

        Self {
            universal,
            exact,
            glob_set,
            glob_rule_ids,
        }
    }

    /// Rule indices that could match this host (pre-filter). `None` host → only universal.
    pub fn candidate_indices(&self, host: Option<&str>) -> HashSet<usize> {
        let mut out: HashSet<usize> = self.universal.iter().copied().collect();
        let Some(host) = host else { return out };
        if let Some(ids) = self.exact.get(host) {
            out.extend(ids.iter().copied());
        }
        if let Some(set) = &self.glob_set {
            for set_idx in set.matches(host).into_iter() {
                out.insert(self.glob_rule_ids[set_idx]);
            }
        }
        out
    }
}

// ---------------------------------------------------------------------------
// Pipeline
// ---------------------------------------------------------------------------

/// Maximum `FollowRedirect` recursion depth.
const MAX_DEPTH: usize = 10;

/// Canonicalize `url` by applying all matching rules top-to-bottom.
///
/// `resolve` is the HTTP redirect resolver (dependency injection): production
/// wires in a `reqwest` resolver; tests inject a stub. It is only invoked for
/// [`Action::FollowRedirect`] when `online` is true.
pub fn canonicalize<R: Fn(&str) -> String>(
    url: &str,
    rules: &[Rule],
    online: bool,
    resolve: &R,
) -> String {
    let index = RuleIndex::new(rules);
    canonicalize_inner(url, rules, online, resolve, &index, 0)
}

fn canonicalize_inner<R: Fn(&str) -> String>(
    url: &str,
    rules: &[Rule],
    online: bool,
    resolve: &R,
    index: &RuleIndex,
    depth: usize,
) -> String {
    if depth > MAX_DEPTH {
        return url.to_string();
    }

    // Parse once and keep one live `Url` across the whole rule loop — actions
    // mutate it in place, so we never round-trip through `String` per rule.
    let Ok(mut f) = Url::parse(url) else {
        return url.to_string();
    };

    // `candidates` is recomputed only when the host changes (rare; mid-pipeline
    // host rewrites). `host_of_candidates` records which host they were built for.
    let mut candidates = index.candidate_indices(f.host());
    let mut host_of_candidates: Option<String> = f.host().map(String::from);

    for (i, r) in rules.iter().enumerate() {
        if !candidates.contains(&i) {
            continue;
        }
        if !r.matcher.matches(&f) {
            continue;
        }

        for action in &r.actions {
            if matches!(action, Action::FollowRedirect) {
                if online {
                    let resolved = resolve(&f.to_string());
                    return canonicalize_inner(&resolved, rules, online, resolve, index, depth + 1);
                }
                break; // offline → skip the rest of this rule
            }
            // UnwrapRedirectParam returns the unwrapped URL: switch context but do
            // NOT break — remaining actions run on it (LinkedIn behavior).
            if let Some(new_url) = apply(action, &mut f) {
                match Url::parse(&new_url) {
                    Ok(parsed) => f = parsed,
                    // Unparseable unwrap target: bail out with what we have.
                    Err(_) => return new_url,
                }
            }
        }

        // Pick up host changes from RewriteHost / RewriteHostPrefix.
        if f.host().map(str::to_owned) != host_of_candidates {
            host_of_candidates = f.host().map(String::from);
            candidates = index.candidate_indices(f.host());
        }
    }

    f.to_string()
}

// ---------------------------------------------------------------------------
// Bootstrap lint  (validate_rules)
// ---------------------------------------------------------------------------

/// Hosts a matcher can match. `"*"` means unconstrained.
fn hosts_from_matcher(m: &Matcher) -> HashSet<String> {
    match m {
        Matcher::AnyHost => HashSet::from(["*".to_string()]),
        Matcher::Host(h) => HashSet::from([h.clone()]),
        Matcher::And(l, r) => {
            let left = hosts_from_matcher(l);
            let right = hosts_from_matcher(r);
            if left.contains("*") {
                right
            } else if right.contains("*") {
                left
            } else {
                left.intersection(&right).cloned().collect()
            }
        }
        // HostGlob and Path: unconstrained at the host level (conservative).
        _ => HashSet::from(["*".to_string()]),
    }
}

fn rules_can_overlap(a: &Rule, b: &Rule) -> bool {
    let ha = hosts_from_matcher(&a.matcher);
    let hb = hosts_from_matcher(&b.matcher);
    if ha.contains("*") || hb.contains("*") {
        return true;
    }
    ha.intersection(&hb).next().is_some()
}

fn has_keep_params(r: &Rule) -> bool {
    r.actions.iter().any(|a| matches!(a, Action::KeepParams(_)))
}

fn is_host_rewriting(r: &Rule) -> bool {
    r.actions
        .iter()
        .any(|a| matches!(a, Action::RewriteHost(_) | Action::RewriteHostPrefix { .. }))
}

/// Bootstrap lint: `Err(msg)` on rule ordering/conflict problems.
///
/// 1. Two `KeepParams` rules that can match the same URL (each strips what the
///    other kept).
/// 2. A specific `Host("x")` host-rewriting rule eclipsed by an earlier
///    `HostGlob` rule that also rewrites the host and matches `"x"`.
pub fn validate_rules(rules: &[Rule]) -> Result<(), String> {
    // --- Check 1: conflicting KeepParams ---
    let keep: Vec<usize> = rules
        .iter()
        .enumerate()
        .filter(|(_, r)| has_keep_params(r))
        .map(|(i, _)| i)
        .collect();
    for a in 0..keep.len() {
        for b in (a + 1)..keep.len() {
            let (i, j) = (keep[a], keep[b]);
            if rules_can_overlap(&rules[i], &rules[j]) {
                return Err(format!(
                    "Conflicting KeepParams: rules[{i}] and rules[{j}] can match the same URL. \
                     Use KeepParams only in domain-specific rules (Host(...)), never in AnyHost() \
                     or overlapping rules."
                ));
            }
        }
    }

    // --- Check 2: eclipsed host-rewriting rules ---
    for (i, r) in rules.iter().enumerate() {
        if !is_host_rewriting(r) {
            continue;
        }
        let hosts = hosts_from_matcher(&r.matcher);
        if hosts.contains("*") {
            continue;
        }
        for earlier in &rules[..i] {
            let Matcher::HostGlob(pat) = &earlier.matcher else {
                continue;
            };
            if !is_host_rewriting(earlier) {
                continue;
            }
            for host in &hosts {
                if glob_match(pat, host) {
                    return Err(format!(
                        "rules[{i}] (Host-specific, rewrites host for {host:?}) is eclipsed by an \
                         earlier HostGlob({pat:?}) — the glob fires first and changes the host, so \
                         the specific rule is never reached. Move it before the glob."
                    ));
                }
            }
        }
    }

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::url_model::Url;

    fn fu(s: &str) -> Url {
        Url::parse(s).unwrap()
    }

    // --- Matchers ---

    #[test]
    fn any_host_matches_everything() {
        assert!(AnyHost().matches(&fu("https://example.com/path")));
    }

    #[test]
    fn host_matches_exact() {
        let m = Host("www.linkedin.com");
        assert!(m.matches(&fu("https://www.linkedin.com/learning/course")));
        assert!(!m.matches(&fu("https://linkedin.com/learning/course")));
    }

    #[test]
    fn path_matches_glob() {
        let m = Path("/share/*");
        assert!(m.matches(&fu("https://x.com/share/p/abc123/")));
        assert!(!m.matches(&fu("https://x.com/posts/123")));
    }

    #[test]
    fn glob_brackets_are_literal_not_character_classes() {
        // We support only `*` and `?` (not fnmatch `[...]` classes); a `[` is a
        // literal character. This locks that intentional limitation.
        let m = Path("/a[b]c");
        assert!(m.matches(&fu("https://x.com/a[b]c")));
        assert!(!m.matches(&fu("https://x.com/abc")));
    }

    #[test]
    fn and_combinator() {
        let m = Host("www.linkedin.com") & Path("/learning-login/share");
        assert!(m.matches(&fu("https://www.linkedin.com/learning-login/share?x=1")));
        assert!(!m.matches(&fu("https://www.linkedin.com/learning/course")));
        assert!(!m.matches(&fu("https://other.com/learning-login/share")));
    }

    // --- StripParams ---

    #[test]
    fn strip_exact_param() {
        let mut f = fu("https://example.com/?a=1&fbclid=XYZ");
        apply(&strip_params(&["fbclid"]), &mut f);
        let s = f.to_string();
        assert!(!s.contains("fbclid"));
        assert!(s.contains("a=1"));
    }

    #[test]
    fn strip_glob_param() {
        let mut f = fu("https://example.com/?utm_source=foo&utm_campaign=bar&keep=1");
        apply(&strip_params(&["utm_*"]), &mut f);
        let s = f.to_string();
        assert!(!s.contains("utm_source"));
        assert!(!s.contains("utm_campaign"));
        assert!(s.contains("keep=1"));
    }

    #[test]
    fn strip_wildcard_all() {
        let mut f = fu("https://example.com/?a=1&b=2");
        apply(&strip_params(&["*"]), &mut f);
        assert_eq!(f.to_string(), "https://example.com/");
    }

    #[test]
    fn strip_multiple_patterns() {
        let mut f = fu("https://x.com/?fbclid=X&utm_source=Y&rdid=Z&keep=1");
        apply(&strip_params(&["fbclid", "utm_*", "rdid"]), &mut f);
        let s = f.to_string();
        assert!(s.contains("keep=1"));
        assert!(!s.contains("fbclid"));
        assert!(!s.contains("utm_source"));
        assert!(!s.contains("rdid"));
    }

    // --- Other actions ---

    #[test]
    fn test_unwrap_redirect_param() {
        let mut f = fu("https://www.linkedin.com/learning-login/share\
            ?account=123&redirect=https%3A%2F%2Fwww.linkedin.com%2Flearning%2Fcourse");
        let new_url = apply(&unwrap_redirect_param("redirect"), &mut f);
        assert_eq!(
            new_url.as_deref(),
            Some("https://www.linkedin.com/learning/course")
        );
    }

    #[test]
    fn test_rewrite_host() {
        let mut f = fu("https://m.facebook.com/story.php?id=123");
        apply(&rewrite_host("www.facebook.com"), &mut f);
        assert_eq!(f.to_string(), "https://www.facebook.com/story.php?id=123");
    }

    #[test]
    fn test_trim_path_suffix() {
        let mut f = fu("https://www.linkedin.com/learning/agile/course-introduction");
        apply(&trim_path_suffix(1), &mut f);
        assert_eq!(f.to_string(), "https://www.linkedin.com/learning/agile");
    }

    #[test]
    fn test_extract_path() {
        let mut f = fu("https://www.amazon.com/-/zh_TW/Clean-Code/dp/0132350882");
        apply(&extract_path(r"/dp/[A-Z0-9]+"), &mut f);
        assert_eq!(f.to_string(), "https://www.amazon.com/dp/0132350882");
    }

    #[test]
    fn trim_path_suffix_n0_is_noop() {
        let mut f = fu("https://example.com/learning/agile/course");
        apply(&trim_path_suffix(0), &mut f);
        assert_eq!(f.to_string(), "https://example.com/learning/agile/course");
    }

    #[test]
    fn test_strip_fragment() {
        let mut f = fu("https://example.com/page#section-2");
        apply(&strip_fragment(), &mut f);
        assert_eq!(f.to_string(), "https://example.com/page");
    }

    // --- KeepParams ---

    #[test]
    fn keep_params_strips_non_listed() {
        let mut f = fu("https://www.youtube.com/watch?v=abc&si=XYZ&t=30");
        apply(&keep_params(&["v", "t"]), &mut f);
        let s = f.to_string();
        assert!(s.contains("v=abc"));
        assert!(s.contains("t=30"));
        assert!(!s.contains("si"));
    }

    #[test]
    fn keep_params_empty_list_strips_all() {
        let mut f = fu("https://example.com/?a=1&b=2");
        apply(&keep_params(&[]), &mut f);
        assert_eq!(f.to_string(), "https://example.com/");
    }

    #[test]
    fn keep_params_glob_pattern() {
        let mut f = fu("https://example.com/?v=1&v_extra=2&noise=x");
        apply(&keep_params(&["v*"]), &mut f);
        let s = f.to_string();
        assert!(s.contains("v=1"));
        assert!(s.contains("v_extra=2"));
        assert!(!s.contains("noise"));
    }

    // --- RewritePath ---

    #[test]
    fn rewrite_path_strips_slug_keeps_id() {
        let mut f = fu("https://medium.com/pub/the-full-title-49ea0df5c5a9");
        apply(
            &rewrite_path(r"^(/[^/]+/).*-([0-9a-f]{12})$", r"$1$2"),
            &mut f,
        );
        assert_eq!(f.to_string(), "https://medium.com/pub/49ea0df5c5a9");
    }

    #[test]
    fn rewrite_path_clears_query_and_fragment() {
        let mut f = fu("https://medium.com/pub/title-49ea0df5c5a9?source=newsletter#section");
        apply(
            &rewrite_path(r"^(/[^/]+/).*-([0-9a-f]{12})$", r"$1$2"),
            &mut f,
        );
        assert_eq!(f.to_string(), "https://medium.com/pub/49ea0df5c5a9");
    }

    #[test]
    fn rewrite_path_noop_when_no_match() {
        let mut f = fu("https://example.com/regular-path?keep=1");
        apply(
            &rewrite_path(r"^(/[^/]+/).*-([0-9a-f]{12})$", r"$1$2"),
            &mut f,
        );
        assert_eq!(f.to_string(), "https://example.com/regular-path?keep=1");
    }

    #[test]
    fn rewrite_path_noop_when_already_id_only() {
        let mut f = fu("https://medium.com/pub/49ea0df5c5a9");
        apply(
            &rewrite_path(r"^(/[^/]+/).*-([0-9a-f]{12})$", r"$1$2"),
            &mut f,
        );
        assert_eq!(f.to_string(), "https://medium.com/pub/49ea0df5c5a9");
    }

    // -----------------------------------------------------------------------
    // Pipeline  (offline resolver; FollowRedirect cases inject a stub)
    // -----------------------------------------------------------------------

    /// Resolver that returns the URL unchanged (offline default for non-redirect tests).
    fn no_net(_url: &str) -> String {
        unreachable!("offline test must not hit the resolver")
    }

    fn canon(url: &str, rules: &[Rule]) -> String {
        canonicalize(url, rules, false, &no_net)
    }

    #[test]
    fn pipeline_strips_fbclid() {
        let rules = vec![rule(AnyHost(), vec![strip_params(&["fbclid"])])];
        assert_eq!(
            canon("https://buzzorange.com/article/?fbclid=XYZ&other=1", &rules),
            "https://buzzorange.com/article/?other=1"
        );
    }

    #[test]
    fn pipeline_all_matching_rules_run() {
        let rules = vec![
            rule(AnyHost(), vec![strip_params(&["a"])]),
            rule(AnyHost(), vec![strip_params(&["b"])]),
        ];
        assert_eq!(
            canon("https://example.com/?a=1&b=2&c=3", &rules),
            "https://example.com/?c=3"
        );
    }

    #[test]
    fn pipeline_non_matching_rule_skipped() {
        let rules = vec![rule(Host("other.com"), vec![strip_params(&["*"])])];
        assert_eq!(
            canon("https://example.com/?keep=1", &rules),
            "https://example.com/?keep=1"
        );
    }

    #[test]
    fn pipeline_linkedin_learning_login() {
        let rules = vec![
            rule(AnyHost(), vec![strip_params(&["fbclid", "utm_*"])]),
            rule(
                Host("www.linkedin.com") & Path("/learning-login/share"),
                vec![
                    unwrap_redirect_param("redirect"),
                    strip_params(&["account", "forceAccount", "trk", "shareId"]),
                ],
            ),
        ];
        let before = "https://www.linkedin.com/learning-login/share\
            ?account=352396234&forceAccount=false\
            &redirect=https%3A%2F%2Fwww.linkedin.com%2Flearning%2Fcourse-name\
            %3Ftrk%3Dshare_ent_url%26shareId%3Dabc";
        assert_eq!(
            canon(before, &rules),
            "https://www.linkedin.com/learning/course-name"
        );
    }

    #[test]
    fn follow_redirect_restarts_pipeline() {
        let rules = vec![
            rule(AnyHost(), vec![strip_params(&["rdid", "utm_*"])]),
            rule(
                Host("www.facebook.com") & Path("/share/*"),
                vec![follow_redirect()],
            ),
        ];
        let resolver =
            |_url: &str| "https://www.facebook.com/Page/posts/pfbid0abc?rdid=XYZ".to_string();
        let result = canonicalize(
            "https://www.facebook.com/share/p/18GKaNgTxp/",
            &rules,
            true,
            &resolver,
        );
        assert_eq!(result, "https://www.facebook.com/Page/posts/pfbid0abc");
    }

    #[test]
    fn follow_redirect_skipped_when_offline() {
        let rules = vec![rule(AnyHost(), vec![follow_redirect()])];
        let url = "https://example.com/share/p/abc";
        assert_eq!(canonicalize(url, &rules, false, &no_net), url);
    }

    #[test]
    fn keep_params_pipeline() {
        let rules = vec![
            rule(AnyHost(), vec![strip_params(&["utm_*"])]),
            rule(Host("www.youtube.com"), vec![keep_params(&["v", "t"])]),
        ];
        let url = "https://www.youtube.com/watch?v=abc&t=30&si=XYZ&utm_source=foo";
        assert_eq!(
            canon(url, &rules),
            "https://www.youtube.com/watch?v=abc&t=30"
        );
    }

    // -----------------------------------------------------------------------
    // RuleIndex — incl. DFA-merge (RegexSet) test
    // -----------------------------------------------------------------------

    #[test]
    fn rule_index_dfa_merge_returns_correct_glob_rules() {
        // Two HostGlob rules merged into one RegexSet; assert correct rule ids.
        let rules = vec![
            rule(AnyHost(), vec![strip_params(&["utm_*"])]), // idx 0 (universal)
            rule(HostGlob("m.*.com"), vec![rewrite_host_prefix("m.", "www.")]), // idx 1
            rule(HostGlob("*.hashnode.dev"), vec![strip_fragment()]), // idx 2
        ];
        let index = RuleIndex::new(&rules);

        let c1 = index.candidate_indices(Some("m.youtube.com"));
        assert!(c1.contains(&0) && c1.contains(&1) && !c1.contains(&2));

        let c2 = index.candidate_indices(Some("john.hashnode.dev"));
        assert!(c2.contains(&0) && c2.contains(&2) && !c2.contains(&1));

        // Non-matching host → only the universal rule.
        let c3 = index.candidate_indices(Some("example.org"));
        assert!(c3.contains(&0) && c3.len() == 1);

        // No host → only universal.
        let c4 = index.candidate_indices(None);
        assert!(c4.contains(&0) && c4.len() == 1);
    }

    #[test]
    fn rule_index_exact_host_lookup() {
        let rules = vec![
            rule(AnyHost(), vec![strip_params(&["utm_*"])]),
            rule(Host("www.youtube.com"), vec![keep_params(&["v"])]),
            rule(Host("x.com"), vec![strip_params(&["s"])]),
        ];
        let index = RuleIndex::new(&rules);
        let c = index.candidate_indices(Some("www.youtube.com"));
        assert!(c.contains(&0) && c.contains(&1) && !c.contains(&2));
    }

    // -----------------------------------------------------------------------
    // validate_rules
    // -----------------------------------------------------------------------

    #[test]
    fn validate_rules_passes_strip_and_keep_same_host() {
        let rules = vec![
            rule(Host("x.com"), vec![strip_params(&["a"])]),
            rule(Host("x.com"), vec![keep_params(&["v"])]),
        ];
        assert!(validate_rules(&rules).is_ok());
    }

    #[test]
    fn validate_rules_passes_keep_params_different_hosts() {
        let rules = vec![
            rule(Host("x.com"), vec![keep_params(&["v"])]),
            rule(Host("y.com"), vec![keep_params(&["id"])]),
        ];
        assert!(validate_rules(&rules).is_ok());
    }

    #[test]
    fn validate_rules_conflict_same_host() {
        let rules = vec![
            rule(Host("x.com"), vec![keep_params(&["v"])]),
            rule(Host("x.com"), vec![keep_params(&["id"])]),
        ];
        let err = validate_rules(&rules).unwrap_err();
        assert!(err.contains("Conflicting KeepParams"));
    }

    #[test]
    fn validate_rules_conflict_any_host() {
        let rules = vec![
            rule(AnyHost(), vec![keep_params(&["v"])]),
            rule(Host("x.com"), vec![keep_params(&["id"])]),
        ];
        let err = validate_rules(&rules).unwrap_err();
        assert!(err.contains("Conflicting KeepParams"));
    }

    #[test]
    fn validate_rules_conflict_host_and_host_with_path() {
        let rules = vec![
            rule(Host("x.com") & Path("/a/*"), vec![keep_params(&["v"])]),
            rule(Host("x.com") & Path("/b/*"), vec![keep_params(&["id"])]),
        ];
        let err = validate_rules(&rules).unwrap_err();
        assert!(err.contains("Conflicting KeepParams"));
    }

    #[test]
    fn validate_rules_passes_specific_host_rewrite_before_glob() {
        let rules = vec![
            rule(Host("m.x.com"), vec![rewrite_host("x.com")]),
            rule(HostGlob("m.*.com"), vec![rewrite_host_prefix("m.", "www.")]),
        ];
        assert!(validate_rules(&rules).is_ok());
    }

    #[test]
    fn validate_rules_eclipsed_host_rewrite_after_glob() {
        let rules = vec![
            rule(HostGlob("m.*.com"), vec![rewrite_host_prefix("m.", "www.")]),
            rule(Host("m.x.com"), vec![rewrite_host("x.com")]),
        ];
        let err = validate_rules(&rules).unwrap_err();
        assert!(err.contains("eclipsed"));
    }

    #[test]
    fn validate_rules_passes_glob_no_host_rewrite() {
        let rules = vec![
            rule(HostGlob("m.*.com"), vec![strip_params(&["utm_*"])]),
            rule(Host("m.x.com"), vec![rewrite_host("x.com")]),
        ];
        assert!(validate_rules(&rules).is_ok());
    }

    #[test]
    fn validate_rules_passes_specific_glob_no_overlap() {
        let rules = vec![
            rule(HostGlob("m.*.org"), vec![rewrite_host_prefix("m.", "www.")]),
            rule(Host("m.x.com"), vec![rewrite_host("x.com")]),
        ];
        assert!(validate_rules(&rules).is_ok());
    }
}
