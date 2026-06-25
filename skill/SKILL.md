---
name: canonicalizing-urls
description: Use when working with any URL that may contain tracking parameters, redirect wrappers, locale prefixes, or opaque share links.
metadata:
  author: William-Yeh
---

# canonicalizing-urls

Canonicalize URLs by running the `canonicalize` binary.

## Step 0: ensure the binary exists (run once)

The binary is fetched per-platform on first use (no Rust toolchain needed):

```sh
[ -x "$SKILL_DIR/bin/canonicalize" ] || "$SKILL_DIR/scripts/install.sh"
```

`$SKILL_DIR` is this skill's directory. `install.sh` downloads the matching
release binary into `bin/canonicalize` (falling back to `cargo build --release`
if no prebuilt asset fits the platform). After that, invoke `"$SKILL_DIR/bin/canonicalize"`.

**Contract:** stdout is the canonical URL (single line) and nothing else;
diagnostics go to stderr; exit 0 on success (unchanged input prints unchanged),
exit 1 on a hard error (e.g. unparseable URL) — on exit 1, leave the URL as-is.

## Proactive use

When a URL appears in a context where it will be saved, shared, or cited:
1. `"$SKILL_DIR/bin/canonicalize" <url>` (offline, static rules only)
2. If the URL matches an opaque short-link pattern (e.g. `/share/p/`), add `--online`
3. If the result differs from the input, substitute the canonical form
4. Note the change inline: "(canonicalized: removed fbclid)"

## Explicit use

When the user asks to canonicalize a URL:
1. `"$SKILL_DIR/bin/canonicalize" <url>`
2. If unchanged and the URL looks non-canonical, run `--probe` to discover rules

## Adding a new rule

When the binary returns unchanged output but the URL is clearly non-canonical:
1. `"$SKILL_DIR/bin/canonicalize" --probe <url>` — review the suggested `rule(...)`.
   For a host-rewriting rule, probe also prints an ordering hint on stderr,
   e.g. `↳ insert before rules[3] (HostGlob("m.*.com"))` — use that index.
2. Ask the user: generalize to a pattern, or keep domain-specific?
3. Add a failing UAT row to `tests/uat.rs` (BEFORE→AFTER)
4. Add the confirmed `rule(...)` to `rules()` in `src/rules.rs`
   - Place it at the index the probe hint named. The rule of thumb: a specific
     `Host(...)` rule that rewrites the host must come **before** any `HostGlob`
     that matches it (else the glob fires first and eclipses it).
   - You don't have to get this right by hand: `validate_rules` (run by `cargo test`)
     fails the build if a rule is misordered and names the exact target index to move it to.
   - Otherwise insert after similar-domain rules
   - `RewritePath` uses Rust regex replacement syntax: `$1`, `$2` (not `\1`, `\2`)
5. `cargo run -- <original_url>` — verify output
6. `cargo test` — confirm all tests pass
7. Rebuild + reinstall the binary; commit: `feat: add <domain> canonicalization rule`
