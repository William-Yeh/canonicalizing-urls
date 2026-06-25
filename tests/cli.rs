//! Process-level e2e tests of the compiled `canonicalize` binary.
//!
//! These spawn the actual binary and assert the stdout/stderr/exit-code
//! contract that SKILL.md depends on — the part the library-level UAT tests
//! (which call `canonicalize()` directly) cannot reach. Offline cases only;
//! the `--online` path needs a live network and is covered at the library
//! level via an injected resolver in `tests/uat.rs`.

use assert_cmd::Command;
use predicates::prelude::*;

fn bin() -> Command {
    Command::cargo_bin("canonicalize").expect("binary builds")
}

#[test]
fn canonicalizes_and_prints_only_the_url_on_stdout() {
    bin()
        .arg("https://m.youtube.com/watch?v=dQw4w9WgXcQ&si=TRACKING")
        .assert()
        .success()
        // stdout is EXACTLY the canonical URL + trailing newline, nothing else.
        .stdout("https://www.youtube.com/watch?v=dQw4w9WgXcQ\n")
        .stderr(predicate::str::is_empty());
}

#[test]
fn unchanged_url_prints_input_unchanged_exit_0() {
    bin()
        .arg("https://example.com/clean")
        .assert()
        .success()
        .stdout("https://example.com/clean\n");
}

#[test]
fn invalid_url_exits_1_with_stderr_message_and_no_stdout() {
    bin()
        .arg("::not a url::")
        .assert()
        .failure()
        .code(1)
        .stdout(predicate::str::is_empty())
        .stderr(predicate::str::contains("invalid URL"));
}

#[test]
fn strips_tracking_params_keeping_order() {
    bin()
        .arg("https://example.com/?utm_source=x&keep=1&utm_medium=y")
        .assert()
        .success()
        .stdout("https://example.com/?keep=1\n");
}

#[test]
fn missing_argument_is_a_clap_usage_error() {
    // No URL argument → clap errors out (exit 2), nothing on stdout.
    bin().assert().failure().stdout(predicate::str::is_empty());
}
