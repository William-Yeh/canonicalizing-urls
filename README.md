# canonicalizing-urls

[![CI](https://github.com/William-Yeh/canonicalizing-urls/actions/workflows/ci.yml/badge.svg)](https://github.com/William-Yeh/canonicalizing-urls/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Agent Skills](https://img.shields.io/badge/Agent_Skills-compatible-blueviolet)](https://agentskills.dev)

An agent skill that canonicalizes URLs — stripping tracking params, unwrapping redirects, normalizing hosts, extracting canonical paths, and resolving opaque short-links.

## Installation

### Recommended: `npx skills`

```bash
npx skills add William-Yeh/canonicalizing-urls
```

### Manual installation

Copy the skill directory to your agent's skill folder:

| Agent | Directory |
|-------|-----------|
| Claude Code | `~/.claude/skills/` |
| Cursor | `.cursor/skills/` |
| Gemini CLI | `.gemini/skills/` |
| Amp | `.amp/skills/` |
| Roo Code | `.roo/skills/` |
| Copilot | `.github/skills/` |

## Usage

**Explicit:** Ask Claude to canonicalize a URL:

- `"Canonicalize this URL: https://www.linkedin.com/learning-login/share?redirect=...&account=123"`
- `"Clean up the tracking params in this URL"`
- `/canonicalize https://buzzorange.com/...?fbclid=XYZ`

**Proactive:** Claude silently canonicalizes URLs when you save to Notion, create hyperlinks, or quote URLs in documents. If the URL changes, Claude notes it inline: "(canonicalized: removed fbclid)".

**Add a rule:** When a URL isn't cleaned up, Claude can probe it:

- `"Add a canonicalization rule for this URL: <url>"`

## Built-in rules

| Domain | What it cleans |
|--------|---------------|
| Any | `fbclid`, `sfnsn`, `mibextid`, `fb_*` (Facebook/Meta) |
| Any | `utm_*`, `wts*`, `aem_*`, `rdid` |
| Any | `_hsenc`, `_hsmi`, `mc_cid`, `mc_eid` (HubSpot/Mailchimp) |
| Any | `mkt_tok` (Marketo), `_ke` (Klaviyo), `vgo_ee` (ActiveCampaign) |
| Any | `launch_app_store` (X mobile app-store redirect hint) |
| `m.x.com` | Rewrite directly to `x.com` (X has no `www.` subdomain) |
| `www.linkedin.com/learning-login/share` | Unwrap redirect, strip `account`/`trk`/`shareId` |
| `www.linkedin.com` | Strip `u` param |
| `m.*.com` | Rewrite mobile subdomain to `www.` (e.g. `m.youtube.com` → `www.youtube.com`) |
| `www.facebook.com` | Keep only `v`, `story_fbid`, `id`, `set` params |
| `www.facebook.com/share/*` | Follow redirect to real URL |
| `share.google` | Follow redirect to real URL (then YouTube rules apply) |
| `www.youtube.com` | Keep only `v`, `t`, `list`, `index` params |
| `www.amazon.com` | Extract `/dp/<ASIN>` path |
| `www.infoq.cn` | Strip all params |
| `mailchi.mp` | Strip all params (removes per-subscriber `e=` ID) |
| `medium.com` | Strip verbose slug, keep 12-char hex article ID (e.g. `/pub/long-title-49ea0df5c5a9` → `/pub/49ea0df5c5a9`) |
| `dev.to` | Strip verbose slug, keep short hex article ID |
| `*.hashnode.dev` | Strip verbose slug, keep CUID |

## Testing

```bash
uv run --group dev pytest tests/ -v
```

`tests/test_uat.py` contains a human-readable BEFORE→AFTER table that acts as
both the regression suite and the acceptance spec for all built-in rules.

For performance benchmarks and complexity verification, see [BENCHMARK.md](BENCHMARK.md).

## Requirements

- `uv` (for running the script with inline deps)

## License

Apache-2.0 © 2026 William Yeh <william.yeh@gmail.com>
