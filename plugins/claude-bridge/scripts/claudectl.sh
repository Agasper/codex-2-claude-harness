#!/usr/bin/env bash
# Keep the public Bash entry point (including PATH symlinks). Python loads the
# runner into memory before executing, so edits cannot corrupt an in-flight run.
set -eu
RUNNER="$(python3 -c 'import pathlib,sys; print(pathlib.Path(sys.argv[1]).resolve().with_name("claudectl.py"))' "$0")"
exec python3 "$RUNNER" "$@"
