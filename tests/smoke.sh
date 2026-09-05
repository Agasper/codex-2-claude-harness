#!/usr/bin/env bash
# Fast claudectl checks that need no Claude call and cost nothing.
set -uo pipefail
CTL="$(cd "$(dirname "${BASH_SOURCE[0]}")/../plugins/claude-bridge/scripts" && pwd)/claudectl.sh"
fails=0

check() { # description, expected exit code, command...
  local desc="$1" want="$2"; shift 2
  local out code
  out=$("$@" 2>&1); code=$?
  if [ "$code" -eq "$want" ]; then printf '  ✓ %s\n' "$desc"
  else printf '  ✗ %s (exit %s, expected %s)\n     %s\n' "$desc" "$code" "$want" "$(echo "$out" | head -1)"; fails=1; fi
}

echo "claudectl smoke tests:"
bash -n "$CTL" && echo "  ✓ syntax" || { echo "  ✗ syntax"; fails=1; }
check "usage with no arguments"          0 bash "$CTL"
check "unknown mode is rejected"         2 bash "$CTL" no-such-mode
check "unknown flag is rejected"         2 bash "$CTL" run --cwd "$HOME" --nosuchflag
check "run without --cwd is rejected"    2 bash "$CTL" run --prompt x
check "run without a prompt is rejected" 2 bash "$CTL" run --cwd "$HOME"
check "missing directory is rejected"    2 bash "$CTL" run --cwd /no/such/path --prompt x
check "/tmp is rejected with a reason"   2 bash "$CTL" run --cwd /tmp --prompt x
check "unknown run id is rejected"       2 bash "$CTL" status --id no-such-run

TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/runs" "$TMP/projA" "$TMP/projB"
mkrun() {
  mkdir -p "$TMP/runs/$1"
  printf '{"id":"%s","cwd":"%s","thread_id":"%s","started_at":%s,"state":"done","mode":"run"}\n' \
    "$1" "$2" "$3" "$4" > "$TMP/runs/$1/meta.json"
  : > "$TMP/runs/$1/run.jsonl"
}
mkrun projA-old "$TMP/projA" THREAD-A 100
mkrun projB-new "$TMP/projB" THREAD-B 200   # newer, but another thread and project

picked() { ( cd "$2" && CLAUDE_RUNS_DIR="$TMP/runs" CODEX_THREAD_ID="$1" \
    bash "$CTL" status 2>/dev/null | awk '/^run:/{print $2}' ); }

echo
echo "run selection:"
got=$(picked THREAD-A "$TMP/projA")
[ "$got" = "projA-old" ] && echo "  ✓ own thread wins over a newer foreign run" \
  || { echo "  ✗ own thread: picked '$got'"; fails=1; }
got=$(picked THREAD-A "$TMP/projB")
[ "$got" = "projA-old" ] && echo "  ✓ thread binding survives a wrong working directory" \
  || { echo "  ✗ wrong directory: picked '$got'"; fails=1; }
got=$(picked "" "$TMP/projA")
[ "$got" = "projA-old" ] && echo "  ✓ without a thread id, falls back to the current project" \
  || { echo "  ✗ fallback: picked '$got'"; fails=1; }
out=$( cd "$TMP" && CLAUDE_RUNS_DIR="$TMP/runs" CODEX_THREAD_ID=THREAD-UNKNOWN bash "$CTL" status 2>&1 ); code=$?
[ "$code" -eq 2 ] && [ -z "${out##*no run belongs*}" ] \
  && echo "  ✓ neither thread nor project matches: refuses instead of guessing" \
  || { echo "  ✗ unmatched: exit $code"; fails=1; }

echo
[ "$fails" -eq 0 ] && echo "all green" || echo "failures present"
exit "$fails"
