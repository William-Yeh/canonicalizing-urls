//! Machine-independent performance-ratio assertions — ported from Python
//! `tests/perf_bench.py`. These guard the complexity contract (not absolute
//! speed): they FAIL the build if an O(1) operation silently becomes O(R).
//!
//! Ratio thresholds (machine-independent):
//!   "flat"  → max/min < 5×  across data points
//!   "grows" → last/first > expected_growth × 0.1
//!
//! Run: `cargo test --test perf_ratios -- --nocapture`

use std::time::Instant;

use canonicalize::engine::{
    canonicalize, extract_path, rewrite_path, rule, strip_params, AnyHost, Host, HostGlob, Rule,
    RuleIndex,
};

/// Regexes (glob/path) must be compiled ONCE, not per `canonicalize()` call.
///
/// If they recompile per call, a ruleset using `HostGlob`/`ExtractPath`/
/// `RewritePath` costs *dramatically* more than a params-only ruleset — the
/// original regression showed 30–90× (hundreds of µs from cold-cache regex
/// clones). With compilation amortized, the gap is a small constant factor that
/// reflects only the per-call *match* cost.
///
/// The threshold is 20×, not tight: the params-only baseline is itself very
/// cheap (~5 µs after the pipeline parses once instead of per-rule), so even a
/// healthy regex-heavy run sits around 10×. The guard's job is to catch the
/// per-call-recompilation regression (which would blow past 20×), not to police
/// the ratio between two already-fast paths.
#[test]
fn regex_compiled_once_not_per_call() {
    let noop = |u: &str| u.to_string();
    let url = "https://m.youtube.com/foo/bar-49ea0df5c5a9";

    // Params-only baseline: no regex compilation in the hot path.
    let params_rules = vec![
        rule(AnyHost(), vec![strip_params(&["utm_source"])]),
        rule(Host("static.example.com"), vec![strip_params(&["x"])]),
    ];
    // Regex-heavy: glob host match + path extract + path rewrite each call.
    let regex_rules = vec![
        rule(HostGlob("m.*.com"), vec![extract_path(r"/foo/[a-z0-9-]+")]),
        rule(
            Host("medium.com"),
            vec![rewrite_path(r"^(/[^/]+/).*-([0-9a-f]{12})$", r"$1$2")],
        ),
    ];

    let t_params = measure_us(
        || {
            let _ = canonicalize(url, &params_rules, false, &noop);
        },
        50,
        3000,
    );
    let t_regex = measure_us(
        || {
            let _ = canonicalize(url, &regex_rules, false, &noop);
        },
        50,
        3000,
    );

    let ratio = t_regex / t_params;
    eprintln!("  params-only={t_params:.2}µs  regex-heavy={t_regex:.2}µs  ratio={ratio:.1}×");
    assert!(
        ratio < 20.0,
        "regex-using rules are {ratio:.1}× slower than params-only — regexes are likely \
         recompiled per call instead of once. Expected < 20×."
    );
}

fn exact_rules(n: usize) -> Vec<Rule> {
    let mut v = vec![rule(AnyHost(), vec![strip_params(&["utm_source"])])];
    for i in 0..n {
        v.push(rule(
            Host(&format!("host{i}.example.com")),
            vec![strip_params(&["p"])],
        ));
    }
    v
}

/// Mean call time in microseconds.
fn measure_us<F: FnMut()>(mut f: F, warmup: usize, iters: usize) -> f64 {
    for _ in 0..warmup {
        f();
    }
    let t0 = Instant::now();
    for _ in 0..iters {
        f();
    }
    t0.elapsed().as_secs_f64() * 1e6 / iters as f64
}

#[test]
fn bench1_exact_host_lookup_is_flat_vs_naive_on() {
    let host = "www.youtube.com";
    let r_values = [10usize, 100, 1000, 5000];
    let mut t_indices = Vec::new();
    let mut t_naives = Vec::new();

    for &r in &r_values {
        let rules = exact_rules(r);
        let index = RuleIndex::new(&rules);

        let t_idx = measure_us(
            || {
                let _ = index.candidate_indices(Some(host));
            },
            50,
            5000,
        );

        // Naive: ask every matcher directly (no parsing — isolate the skip decision).
        let url = canonicalize::url_model::Url::parse(&format!("https://{host}/")).unwrap();
        let t_naive = measure_us(
            || {
                for rl in &rules {
                    let _ = rl.matcher.matches(&url);
                }
            },
            20,
            2000,
        );

        eprintln!(
            "  R={r:>5}  index={t_idx:>8.3}µs  naive={t_naive:>10.3}µs  speedup={:.1}×",
            t_naive / t_idx
        );
        t_indices.push(t_idx);
        t_naives.push(t_naive);
    }

    let flat = t_indices.iter().cloned().fold(f64::MIN, f64::max)
        / t_indices.iter().cloned().fold(f64::MAX, f64::min);
    let r_growth = *r_values.last().unwrap() as f64 / r_values[0] as f64; // 500×
    let naive_growth = t_naives.last().unwrap() / t_naives[0];

    assert!(
        flat < 5.0,
        "index lookup should be flat (O(1)); max/min was {flat:.1}× (≥5×)"
    );
    assert!(
        naive_growth > r_growth * 0.1,
        "naive scan should grow with R; last/first was {naive_growth:.1}× (expected > {:.0}×)",
        r_growth * 0.1
    );
}

#[test]
fn bench3_index_build_grows_lookup_flat() {
    let host = "www.youtube.com";
    let r_values = [50usize, 200, 1000, 5000];
    let mut t_builds = Vec::new();
    let mut t_lookups = Vec::new();

    for &r in &r_values {
        let rules = exact_rules(r);
        let t_build = measure_us(
            || {
                let _ = RuleIndex::new(&rules);
            },
            5,
            200,
        );
        let index = RuleIndex::new(&rules);
        let t_lookup = measure_us(
            || {
                let _ = index.candidate_indices(Some(host));
            },
            50,
            10000,
        );

        eprintln!("  R={r:>5}  build={t_build:>9.1}µs  lookup={t_lookup:>8.3}µs");
        t_builds.push(t_build);
        t_lookups.push(t_lookup);
    }

    let r_growth = *r_values.last().unwrap() as f64 / r_values[0] as f64; // 100×
    let build_growth = t_builds.last().unwrap() / t_builds[0];
    let lookup_flat = t_lookups.iter().cloned().fold(f64::MIN, f64::max)
        / t_lookups.iter().cloned().fold(f64::MAX, f64::min);

    assert!(
        build_growth > r_growth * 0.1,
        "index build should grow with R; last/first was {build_growth:.1}× (expected > {:.0}×)",
        r_growth * 0.1
    );
    assert!(
        lookup_flat < 5.0,
        "lookup should be flat (O(1)); max/min was {lookup_flat:.1}× (≥5×)"
    );
}
