#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.9"
# dependencies = [
#   "httpx",
#   "beautifulsoup4",
#   "furl",
#   "click",
# ]
# ///
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import click
from engine import canonicalize, probe
from rules import RULES


@click.command()
@click.argument("url")
@click.option("--online", is_flag=True, help="Allow HTTP requests (for FollowRedirect rules)")
@click.option("--probe", "do_probe", is_flag=True, help="Discover and suggest rules for unknown URL")
def main(url: str, online: bool, do_probe: bool) -> None:
    if do_probe:
        probe(url)
    else:
        click.echo(canonicalize(url, rules=RULES, online=online))


if __name__ == "__main__":
    main()
