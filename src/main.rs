//! CLI entry point. Thin imperative shell over the `canonicalize` library.
//!
//! Contract (relied on by SKILL.md):
//!   stdout = result only (the canonical URL, or the probe suggestion block)
//!   stderr = diagnostics
//!   exit 0 = success (unchanged input prints unchanged)
//!   exit 1 = hard error (e.g. unparseable URL) — caller leaves the URL as-is

use std::process::ExitCode;
use std::time::Duration;

use canonicalize::engine::canonicalize;
use canonicalize::probe;
use canonicalize::rules::rules;

use clap::Parser;

/// Canonicalize a URL: strip tracking params, unwrap redirects, normalize hosts,
/// extract canonical paths, resolve opaque short-links.
#[derive(Parser)]
#[command(name = "canonicalize", version, about)]
struct Cli {
    /// The URL to canonicalize.
    url: String,

    /// Allow HTTP requests (for FollowRedirect rules).
    #[arg(long)]
    online: bool,

    /// Discover and suggest rules for an unknown URL.
    #[arg(long)]
    probe: bool,
}

/// Follow HTTP redirects and return the final URL. On any network error,
/// returns the input unchanged (non-fatal — pipeline proceeds with what it has).
fn http_resolve(url: &str) -> String {
    let client = match reqwest::blocking::Client::builder()
        .timeout(Duration::from_secs(10))
        .user_agent("Mozilla/5.0 (compatible; url-canonicalizer/1.0)")
        .build()
    {
        Ok(c) => c,
        Err(e) => {
            eprintln!("warning: HTTP client init failed: {e}");
            return url.to_string();
        }
    };
    match client.get(url).send() {
        Ok(resp) => resp.url().as_str().to_string(),
        Err(e) => {
            eprintln!("warning: redirect resolution failed for {url}: {e}");
            url.to_string()
        }
    }
}

fn main() -> ExitCode {
    let cli = Cli::parse();

    // Hard-error on unparseable input (exit 1) — mirrors the chosen error model.
    if canonicalize::url_model::Url::parse(&cli.url).is_err() {
        eprintln!("error: invalid URL: {}", cli.url);
        return ExitCode::FAILURE;
    }

    let rules = rules();

    if cli.probe {
        probe::probe(&cli.url, &http_resolve);
        return ExitCode::SUCCESS;
    }

    let result = canonicalize(&cli.url, &rules, cli.online, &http_resolve);
    println!("{result}");
    ExitCode::SUCCESS
}
