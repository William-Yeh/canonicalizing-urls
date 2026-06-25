//! criterion benchmarks for the rule index — human-readable reports.
//!
//! Mirrors the four claims verified by `tests/perf_ratios.rs` (which are the
//! machine-independent regression gate); these produce statistical reports
//! under `target/criterion/`.
//!
//! Run: `cargo bench`

use canonicalize::engine::{
    canonicalize, rule, strip_params, AnyHost, Host, HostGlob, Rule, RuleIndex,
};
use canonicalize::url_model::Url;
use criterion::{criterion_group, criterion_main, BenchmarkId, Criterion};

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

fn glob_rules(n: usize) -> Vec<Rule> {
    let mut v = vec![rule(AnyHost(), vec![strip_params(&["utm_source"])])];
    for i in 0..n {
        v.push(rule(
            HostGlob(&format!("x{i}.*.net")),
            vec![strip_params(&["g"])],
        ));
    }
    v
}

const HOST: &str = "www.youtube.com";

fn bench_exact_lookup(c: &mut Criterion) {
    let mut g = c.benchmark_group("exact_host_lookup");
    for &r in &[10usize, 100, 1000, 5000] {
        let rules = exact_rules(r);
        let index = RuleIndex::new(&rules);
        g.bench_with_input(BenchmarkId::new("index", r), &index, |b, idx| {
            b.iter(|| idx.candidate_indices(Some(HOST)))
        });
        let url = Url::parse(&format!("https://{HOST}/")).unwrap();
        g.bench_with_input(BenchmarkId::new("naive", r), &rules, |b, rules| {
            b.iter(|| {
                for rl in rules {
                    let _ = rl.matcher.matches(&url);
                }
            })
        });
    }
    g.finish();
}

fn bench_glob_regexset(c: &mut Criterion) {
    // RegexSet (merged DFA) candidate lookup as G HostGlob rules grow.
    let mut g = c.benchmark_group("hostglob_regexset");
    let host = "m.example.com"; // matches none — worst-case full scan
    for &gn in &[5usize, 20, 50, 100] {
        let rules = glob_rules(gn);
        let index = RuleIndex::new(&rules);
        g.bench_with_input(BenchmarkId::new("regexset", gn), &index, |b, idx| {
            b.iter(|| idx.candidate_indices(Some(host)))
        });
    }
    g.finish();
}

fn bench_build_vs_lookup(c: &mut Criterion) {
    let mut g = c.benchmark_group("build_vs_lookup");
    for &r in &[50usize, 200, 1000, 5000] {
        let rules = exact_rules(r);
        g.bench_with_input(BenchmarkId::new("build", r), &rules, |b, rules| {
            b.iter(|| RuleIndex::new(rules))
        });
        let index = RuleIndex::new(&rules);
        g.bench_with_input(BenchmarkId::new("lookup", r), &index, |b, idx| {
            b.iter(|| idx.candidate_indices(Some(HOST)))
        });
    }
    g.finish();
}

fn bench_pipeline(c: &mut Criterion) {
    // Full canonicalize() over R non-matching rules — should stay flat (O(candidates)).
    let mut g = c.benchmark_group("pipeline_flat");
    let url = format!("https://{HOST}/watch?v=abc&utm_source=share");
    let noop = |u: &str| u.to_string();
    for &r in &[10usize, 50, 200, 500] {
        let rules = exact_rules(r);
        g.bench_with_input(BenchmarkId::new("canonicalize", r), &rules, |b, rules| {
            b.iter(|| canonicalize(&url, rules, false, &noop))
        });
    }
    g.finish();
}

criterion_group!(
    benches,
    bench_exact_lookup,
    bench_glob_regexset,
    bench_build_vs_lookup,
    bench_pipeline
);
criterion_main!(benches);
