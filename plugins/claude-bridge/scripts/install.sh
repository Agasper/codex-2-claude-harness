#!/usr/bin/env bash
# Prepares claudectl: checks prerequisites and puts a symlink on PATH.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${CLAUDECTL_BIN_DIR:-$HOME/.local/bin}"
ok=0
say() { printf '%s\n' "$*"; }
bad() { printf '  ✗ %s\n' "$*"; ok=1; }
good() { printf '  ✓ %s\n' "$*"; }

say "Prerequisites:"
for c in bash python3 git; do
  command -v "$c" >/dev/null 2>&1 && good "$c" || bad "$c not found"
done
if command -v claude >/dev/null 2>&1; then
  good "claude $(claude --version 2>/dev/null | head -1)"
else
  bad "claude not found — install Claude Code: https://claude.com/claude-code"
fi

say ""
say "Installing claudectl:"
mkdir -p "$BIN_DIR"
ln -sf "$HERE/claudectl.sh" "$BIN_DIR/claudectl" && good "$BIN_DIR/claudectl -> $HERE/claudectl.sh"
case ":$PATH:" in
  *":$BIN_DIR:"*) good "$BIN_DIR is already on PATH" ;;
  *) bad "$BIN_DIR is not on PATH — add to ~/.zshrc: export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
esac

say ""
if [ "$ok" -eq 0 ]; then say "Done. Try: claudectl list"; else say "Some items need attention — see ✗ above."; fi
exit "$ok"
