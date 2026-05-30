#!/bin/sh
# jp installer for macOS and Linux.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/pehqge/jp-cli/main/scripts/install.sh | sh
#
# Downloads the standalone `jp` binary for your platform from the latest GitHub
# release and installs it to ~/.local/bin (override with JP_BIN_DIR).
set -eu

REPO="pehqge/jp-cli"
BIN_DIR="${JP_BIN_DIR:-$HOME/.local/bin}"

os="$(uname -s)"
arch="$(uname -m)"

case "$os" in
  Darwin)
    case "$arch" in
      arm64)  asset="jp-macos-arm64" ;;
      x86_64) asset="jp-macos-x86_64" ;;
      *) echo "jp: unsupported macOS architecture: $arch" >&2; exit 1 ;;
    esac
    ;;
  Linux)
    case "$arch" in
      x86_64) asset="jp-linux-x86_64" ;;
      *) echo "jp: unsupported Linux architecture: $arch" >&2; exit 1 ;;
    esac
    ;;
  *)
    echo "jp: unsupported OS: $os (try: pipx install jp-cli)" >&2
    exit 1
    ;;
esac

url="https://github.com/$REPO/releases/latest/download/$asset"

echo "jp: downloading $asset ..."
mkdir -p "$BIN_DIR"
tmp="$(mktemp)"
if ! curl -fsSL "$url" -o "$tmp"; then
  echo "jp: download failed from $url" >&2
  echo "    The release may not include a binary yet. Try: pipx install jp-cli" >&2
  rm -f "$tmp"
  exit 1
fi
chmod +x "$tmp"
mv "$tmp" "$BIN_DIR/jp"
echo "jp: installed to $BIN_DIR/jp"

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *)
    echo ""
    echo "jp: WARNING -- $BIN_DIR is not on your PATH."
    echo "    Add this line to your shell rc (~/.zshrc or ~/.bashrc):"
    echo "      export PATH=\"$BIN_DIR:\$PATH\""
    echo "    Then restart your terminal."
    ;;
esac

echo ""
echo "jp: done. Run 'jp --help' to get started."
