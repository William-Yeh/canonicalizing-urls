#!/usr/bin/env bash
# Fetch the canonicalize binary for this platform into skill/bin/canonicalize.
#
# Release assets are produced by `dist` (see dist-workspace.toml): for each
# target it publishes  canonicalize-<triple>.tar.xz  plus a  .sha256  sidecar.
# We download the archive, verify its checksum, then extract the binary.
# Falls back to building from source if no prebuilt asset fits or download fails.
set -euo pipefail

REPO="William-Yeh/canonicalizing-urls"
SKILL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BIN_DIR="$SKILL_DIR/bin"
BIN="$BIN_DIR/canonicalize"

mkdir -p "$BIN_DIR"

# Map uname → Rust target triple (must match the `targets` in dist-workspace.toml).
os="$(uname -s)"
arch="$(uname -m)"
case "$os-$arch" in
  Darwin-arm64)   triple="aarch64-apple-darwin" ;;
  Darwin-x86_64)  triple="x86_64-apple-darwin" ;;
  Linux-x86_64)   triple="x86_64-unknown-linux-gnu" ;;
  Linux-aarch64)  triple="aarch64-unknown-linux-gnu" ;;
  *)              triple="" ;;
esac

build_from_source() {
  echo "install.sh: building from source (cargo build --release)…" >&2
  if ! command -v cargo >/dev/null 2>&1; then
    echo "install.sh: no prebuilt binary for $os-$arch and cargo is not installed." >&2
    echo "  Install Rust (https://rustup.rs) or build on a supported platform." >&2
    exit 1
  fi
  ( cd "$SKILL_DIR/.." && cargo build --release --bin canonicalize )
  cp "$SKILL_DIR/../target/release/canonicalize" "$BIN"
  chmod +x "$BIN"
  echo "install.sh: built $BIN" >&2
}

if [ -z "$triple" ]; then
  build_from_source
  exit 0
fi

base="https://github.com/$REPO/releases/latest/download"
archive="canonicalize-$triple.tar.xz"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

echo "install.sh: downloading $archive" >&2
# --proto/--tlsv1.2 harden the transport (rustup/dist convention).
if ! curl --proto '=https' --tlsv1.2 -fsSL "$base/$archive" -o "$tmp/$archive" \
   || ! curl --proto '=https' --tlsv1.2 -fsSL "$base/$archive.sha256" -o "$tmp/$archive.sha256"; then
  echo "install.sh: download failed; falling back to source build." >&2
  build_from_source
  exit 0
fi

# Verify the checksum before trusting the archive.
echo "install.sh: verifying checksum" >&2
expected="$(awk '{print $1}' "$tmp/$archive.sha256")"
if command -v shasum >/dev/null 2>&1; then
  actual="$(shasum -a 256 "$tmp/$archive" | awk '{print $1}')"
else
  actual="$(sha256sum "$tmp/$archive" | awk '{print $1}')"
fi
if [ "$expected" != "$actual" ]; then
  echo "install.sh: checksum mismatch for $archive (expected $expected, got $actual)." >&2
  echo "  Refusing to install a binary that failed verification." >&2
  exit 1
fi

# Extract the `canonicalize` binary from the archive into bin/.
tar -xJf "$tmp/$archive" -C "$tmp"
extracted="$(find "$tmp" -type f -name canonicalize -perm -u+x | head -n1)"
if [ -z "$extracted" ]; then
  echo "install.sh: 'canonicalize' binary not found in $archive; building from source." >&2
  build_from_source
  exit 0
fi
mv "$extracted" "$BIN"
chmod +x "$BIN"
echo "install.sh: installed $BIN" >&2
