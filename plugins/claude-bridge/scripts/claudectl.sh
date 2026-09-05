#!/usr/bin/env bash
# claudectl — the single entry point for running Claude Code from Codex.
# Every claude flag is baked in here. Callers only get the modes below.
set -uo pipefail

RUNS_DIR="${CLAUDE_RUNS_DIR:-$HOME/.codex/claude-runs}"
STALL_SECONDS="${CLAUDE_STALL_SECONDS:-180}"

die() { echo "ERROR: $*" >&2; exit 2; }

usage() {
  cat <<'USAGE'
claudectl — run Claude Code through a fixed set of modes.

  claudectl run     --cwd DIR (--prompt TEXT | --prompt-file F) [--model M]
      Task with edit access. Blocks until done — run it in the background.

  claudectl review  --cwd DIR [--base REF] [--prompt TEXT] [--model M]
      Review with no command execution at all. Blocks until done.

  claudectl resume  --id ID (--prompt TEXT | --prompt-file F)
      Follow-up in the same Claude session. Blocks until done.

  claudectl status  [--id ID]      Run state: running / silent / done / failed.
  claudectl result  [--id ID]      Outcome: Claude's answer, cost, what changed.
  claudectl cancel  [--id ID]      Stop a run.
  claudectl list                   Recent runs.

There are no other flags. Permission mode, allowed directories and the output
format are handled inside.
USAGE
}

# ---------- helpers ----------

json_get() { python3 -c "
import json,sys
try: print(json.load(open(sys.argv[1])).get(sys.argv[2],''))
except Exception: print('')
" "$1" "$2" 2>/dev/null; }

json_set() { python3 -c "
import json,sys,os
p=sys.argv[1]
d=json.load(open(p)) if os.path.exists(p) else {}
for i in range(2,len(sys.argv),2): d[sys.argv[i]]=sys.argv[i+1]
json.dump(d,open(p,'w'),ensure_ascii=False,indent=1)
" "$@"; }

current_project() { git rev-parse --show-toplevel 2>/dev/null || pwd; }

# Newest run whose meta field $1 equals $2. Exits 1 when there is no match.
find_run() {
python3 - "$RUNS_DIR" "$1" "${2:-}" <<'FIND'
import json,os,sys
runs,field,want=sys.argv[1],sys.argv[2],sys.argv[3]
if not want or not os.path.isdir(runs): sys.exit(1)
best=None
for name in os.listdir(runs):
    meta=os.path.join(runs,name,'meta.json')
    if not os.path.exists(meta): continue
    try: d=json.load(open(meta))
    except Exception: continue
    if str(d.get(field,'')) != want: continue
    try: t=int(d.get('started_at') or 0)
    except Exception: t=0
    if best is None or t>best[0]: best=(t,os.path.join(runs,name))
if best is None: sys.exit(1)
print(best[1])
FIND
}

# Picks the run to act on. With no --id the choice is never "whatever ran last on
# this machine": resume inherits the working directory of the run it continues,
# so a run started by another Codex thread in another project must not be
# reachable by accident.
resolve_run() {
  local id="${1:-}" r here rcwd
  here=$(current_project)
  if [ -n "$id" ]; then
    [ -d "$RUNS_DIR/$id" ] || die "no such run: $id"
    r="$RUNS_DIR/$id"
    rcwd=$(json_get "$r/meta.json" cwd)
    if [ -n "$rcwd" ] && [ "$rcwd" != "$here" ]; then
      echo "WARNING: run $id belongs to $rcwd, not to $here" >&2
    fi
    echo "$r"; return 0
  fi
  if r=$(find_run thread_id "${CODEX_THREAD_ID:-}"); then echo "$r"; return 0; fi
  if r=$(find_run cwd "$here"); then
    echo "note: no run from this Codex thread, using the newest run of $here" >&2
    echo "$r"; return 0
  fi
  die "no run belongs to this thread or to $here — choose one explicitly: claudectl list, then --id <ID>"
}

human_age() {
  local s=$1
  if   [ "$s" -lt 60 ]   ; then echo "${s}s"
  elif [ "$s" -lt 3600 ] ; then echo "$((s/60))m $((s%60))s"
  else echo "$((s/3600))h $(((s%3600)/60))m"; fi
}

mtime() { stat -f %m "$1" 2>/dev/null || stat -c %Y "$1" 2>/dev/null || echo 0; }

# Prints events from the stream-json log starting at line $2, then the line count.
print_events() {
python3 - "$1" "$2" <<'PY'
import json,sys,time
path,start=sys.argv[1],int(sys.argv[2])
try: lines=open(path,encoding='utf-8',errors='replace').read().splitlines()
except FileNotFoundError: lines=[]
for line in lines[start:]:
    try: d=json.loads(line)
    except Exception: continue
    t=d.get('type'); ts=time.strftime('%H:%M:%S')
    if t=='assistant':
        for c in (d.get('message') or {}).get('content') or []:
            k=c.get('type')
            if k=='tool_use':
                name=c.get('name','?')
                inp=c.get('input') or {}
                detail=inp.get('command') or inp.get('file_path') or inp.get('pattern') or ''
                print(f"[{ts}] {name:9} {str(detail)[:110]}",flush=True)
            elif k=='text':
                txt=(c.get('text') or '').replace('\n',' ').strip()
                if txt: print(f"[{ts}] says      {txt[:130]}",flush=True)
    elif t=='system' and d.get('subtype')=='permission_denied':
        print(f"[{ts}] DENIED    {d.get('tool_name','?')}: {str(d.get('decision_reason',''))[:90]}",flush=True)
print(f"__LINES__{len(lines)}")
PY
}

result_field() {
  python3 -c "
import json,sys
val=''
for line in open(sys.argv[1],encoding='utf-8',errors='replace'):
    try: d=json.loads(line)
    except Exception: continue
    if d.get('type')=='result': val=d.get(sys.argv[2],'')
print(val)
" "$1" "$2" 2>/dev/null
}

# ---------- core: a single run ----------

# execute_run <run_dir> <cwd> <mode> <prompt_file> <model> <resume_session|"">
execute_run() {
  local RUN="$1" CWD="$2" MODE="$3" PROMPT_FILE="$4" MODEL="$5" RESUME="$6"
  local args=(-p --output-format stream-json --verbose --add-dir "$CWD")

  if [ "$MODE" = "review" ]; then
    # A reviewer has no business executing anything: --restricted drops Bash and
    # the other command-running tools outright.
    args+=(--restricted --permission-mode acceptEdits)
  else
    # acceptEdits allows file edits but still blocks commands, so a task that has
    # to run its own tests dies with "This command requires approval". auto is the
    # mode that lets Claude judge each command — verified 2026-09-05.
    args+=(--permission-mode auto)
  fi
  [ -n "$MODEL" ] && args+=(--model "$MODEL")
  if [ -n "$RESUME" ]; then
    args+=(--resume "$RESUME")
  else
    args+=(--session-id "$(json_get "$RUN/meta.json" session_id)")
  fi

  echo "launching: claude ${args[*]}" > "$RUN/cmd.txt"

  # The prompt goes through stdin on purpose: --add-dir is variadic and swallows
  # a trailing prompt argument, which fails with a confusing "input must be
  # provided" error. Verified 2026-09-05.
  ( cd "$CWD" && exec claude "${args[@]}" ) \
      < "$PROMPT_FILE" > "$RUN/run.jsonl" 2> "$RUN/err.log" &
  local pid=$!
  json_set "$RUN/meta.json" pid "$pid" state running

  local line=0 warned=0 out
  while kill -0 "$pid" 2>/dev/null; do
    out=$(print_events "$RUN/run.jsonl" "$line")
    echo "$out" | grep -v '^__LINES__' | grep -v '^$'
    line=$(echo "$out" | sed -n 's/^__LINES__//p' | tail -1); line=${line:-0}
    local age=$(( $(date +%s) - $(mtime "$RUN/run.jsonl") ))
    if [ "$age" -gt "$STALL_SECONDS" ] && [ "$warned" -eq 0 ]; then
      echo "[!] Claude has been silent for $(human_age "$age") — it may be stuck. Check: claudectl status"
      warned=1
    elif [ "$age" -le "$STALL_SECONDS" ]; then warned=0; fi
    sleep 3
  done
  wait "$pid"; local code=$?

  out=$(print_events "$RUN/run.jsonl" "$line")
  echo "$out" | grep -v '^__LINES__' | grep -v '^$'

  local subtype sid cost turns
  subtype=$(result_field "$RUN/run.jsonl" subtype)
  sid=$(result_field "$RUN/run.jsonl" session_id)
  cost=$(result_field "$RUN/run.jsonl" total_cost_usd)
  turns=$(result_field "$RUN/run.jsonl" num_turns)

  local state="done"
  if [ "$code" -ne 0 ]; then state="failed"
  elif [ -z "$subtype" ]; then state="incomplete"
  elif [ "$subtype" != "success" ]; then state="failed"; fi
  json_set "$RUN/meta.json" state "$state" exit_code "$code" session_id "${sid:-}" \
           cost_usd "${cost:-}" turns "${turns:-}" finished_at "$(date +%s)"

  echo
  case "$state" in
    done)       echo "OUTCOME: Claude finished normally." ;;
    failed)     echo "OUTCOME: Claude failed (exit $code, result ${subtype:-none}). Tail of err.log:"; tail -5 "$RUN/err.log" ;;
    incomplete) echo "OUTCOME: Claude was cut off before producing a result. Tail of err.log:"; tail -5 "$RUN/err.log" ;;
  esac
  [ -n "$cost" ] && echo "cost: \$$cost over ${turns:-?} turn(s)"
  summarize_changes "$RUN"
  echo "run id: $(basename "$RUN")"
  [ "$state" = "done" ] && return 0 || return 1
}

summarize_changes() {
  local RUN="$1" CWD BASE
  CWD=$(json_get "$RUN/meta.json" cwd); BASE=$(json_get "$RUN/meta.json" baseline_tag)
  [ -d "$CWD/.git" ] || return 0
  local dirty commits
  dirty=$(git -C "$CWD" status --short 2>/dev/null | wc -l | tr -d ' ')
  commits=0
  [ -n "$BASE" ] && commits=$(git -C "$CWD" rev-list --count "$BASE"..HEAD 2>/dev/null || echo 0)
  echo "changes: $dirty uncommitted file(s), $commits new commit(s) since ${BASE:-—}"
}

# ---------- modes ----------

cmd_run() {
  local CWD="" PROMPT="" PROMPT_FILE="" MODEL="" MODE="run" BASE=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --cwd) CWD="$2"; shift 2;;
      --prompt) PROMPT="$2"; shift 2;;
      --prompt-file) PROMPT_FILE="$2"; shift 2;;
      --model) MODEL="$2"; shift 2;;
      --base) BASE="$2"; shift 2;;
      --readonly) MODE="review"; shift;;
      *) die "unknown flag: $1 (see claudectl --help)";;
    esac
  done
  [ -n "$CWD" ] || die "--cwd is required"
  [ -d "$CWD" ] || die "no such directory: $CWD"
  CWD=$(cd "$CWD" && pwd)
  command -v claude >/dev/null 2>&1 || die "claude is not installed"
  case "$CWD" in
    /tmp|/tmp/*|/private/tmp|/private/tmp/*) die "Claude Code refuses to work under /tmp (path is outside allowed working directories) — use a directory under your home";;
  esac

  # Validate everything before creating anything: a rejected invocation must not
  # leave an empty run directory behind.
  if [ -n "$PROMPT_FILE" ]; then
    [ -f "$PROMPT_FILE" ] || die "no such file: $PROMPT_FILE"
  elif [ -z "$PROMPT" ]; then
    die "--prompt or --prompt-file is required"
  fi

  local ID RUN
  ID="$(basename "$CWD")-$(date +%Y%m%d-%H%M%S)"
  RUN="$RUNS_DIR/$ID"; mkdir -p "$RUN"

  if [ -n "$PROMPT_FILE" ]; then cp "$PROMPT_FILE" "$RUN/prompt.md"
  else printf '%s\n' "$PROMPT" > "$RUN/prompt.md"; fi

  if [ "$MODE" = "review" ]; then
    local target="the uncommitted changes"
    [ -n "$BASE" ] && target="the changes relative to branch $BASE"
    { echo; echo "Review $target. Do not fix anything — only list the problems you find,"
      echo "each with file and line, what is wrong, and why it is a defect."
      echo "If there are no problems, say so."; } >> "$RUN/prompt.md"
  fi

  local HEAD_SHA="" TAG=""
  if git -C "$CWD" rev-parse --git-dir >/dev/null 2>&1; then
    HEAD_SHA=$(git -C "$CWD" rev-parse HEAD 2>/dev/null || echo "")
    if [ "$MODE" = "run" ] && [ -n "$HEAD_SHA" ]; then
      TAG="claude-baseline-$(date +%Y%m%d-%H%M%S)"
      git -C "$CWD" tag "$TAG" >/dev/null 2>&1 || TAG=""
    fi
  fi
  json_set "$RUN/meta.json" id "$ID" cwd "$CWD" mode "$MODE" model "$MODEL" \
           baseline_sha "$HEAD_SHA" baseline_tag "$TAG" started_at "$(date +%s)" state starting \
           thread_id "${CODEX_THREAD_ID:-}" session_id "$(python3 -c 'import uuid;print(uuid.uuid4())')"

  echo "run $ID | mode ${MODE} | model ${MODEL:-<default>} | project $CWD"
  [ -n "$TAG" ] && echo "rollback tag: $TAG"
  echo "---"
  execute_run "$RUN" "$CWD" "$MODE" "$RUN/prompt.md" "$MODEL" ""
}

cmd_resume() {
  local ID="" PROMPT="" PROMPT_FILE="" MODEL=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --id) ID="$2"; shift 2;;
      --model) MODEL="$2"; shift 2;;
      --prompt) PROMPT="$2"; shift 2;;
      --prompt-file) PROMPT_FILE="$2"; shift 2;;
      *) die "unknown flag: $1";;
    esac
  done
  local OLD; OLD=$(resolve_run "$ID") || exit 2
  local SID; SID=$(json_get "$OLD/meta.json" session_id)
  [ -n "$SID" ] || die "run $(basename "$OLD") has no session id — nothing to continue"
  local CWD; CWD=$(json_get "$OLD/meta.json" cwd)

  local NEW_ID RUN
  NEW_ID="$(basename "$OLD")-r$(date +%H%M%S)"
  RUN="$RUNS_DIR/$NEW_ID"; mkdir -p "$RUN"
  if [ -n "$PROMPT_FILE" ]; then cp "$PROMPT_FILE" "$RUN/prompt.md"
  elif [ -n "$PROMPT" ]; then printf '%s\n' "$PROMPT" > "$RUN/prompt.md"
  else die "--prompt or --prompt-file is required"; fi

  json_set "$RUN/meta.json" id "$NEW_ID" cwd "$CWD" mode resume model "$MODEL" \
           baseline_tag "$(json_get "$OLD/meta.json" baseline_tag)" \
           baseline_sha "$(json_get "$OLD/meta.json" baseline_sha)" \
           started_at "$(date +%s)" state starting parent "$(basename "$OLD")" \
           thread_id "${CODEX_THREAD_ID:-}" session_id "$SID"
  echo "follow-up $NEW_ID | Claude session $SID | project $CWD"
  echo "---"
  execute_run "$RUN" "$CWD" "run" "$RUN/prompt.md" "$MODEL" "$SID"
}

cmd_status() {
  local ID=""; [ "${1:-}" = "--id" ] && ID="$2"
  local RUN; RUN=$(resolve_run "$ID") || exit 2
  local state pid started log_age now
  state=$(json_get "$RUN/meta.json" state); pid=$(json_get "$RUN/meta.json" pid)
  started=$(json_get "$RUN/meta.json" started_at); now=$(date +%s)
  log_age=$(( now - $(mtime "$RUN/run.jsonl") ))

  local alive=no
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && alive=yes

  local verdict
  if [ "$alive" = "yes" ]; then
    if [ "$log_age" -gt "$STALL_SECONDS" ]; then verdict="SILENT — no events for $(human_age "$log_age"), possibly stuck"
    else verdict="RUNNING — last event $(human_age "$log_age") ago"; fi
  else
    case "$state" in
      done) verdict="DONE — finished normally";;
      failed) verdict="FAILED — exit $(json_get "$RUN/meta.json" exit_code)";;
      incomplete) verdict="TRUNCATED — no result produced";;
      cancelled) verdict="CANCELLED";;
      *) verdict="NO PROCESS, no outcome recorded — killed from outside";;
    esac
  fi

  echo "run:      $(basename "$RUN")"
  echo "state:    $verdict"
  echo "elapsed:  $(human_age $(( now - ${started:-$now} )))"
  echo "project:  $(json_get "$RUN/meta.json" cwd)"
  echo "latest:"
  print_events "$RUN/run.jsonl" "$(( $(wc -l < "$RUN/run.jsonl" 2>/dev/null || echo 0) - 6 ))" 2>/dev/null \
    | grep -v '^__LINES__' | tail -5 | sed 's/^/  /'
  echo "log:      $RUN/run.jsonl"
}

cmd_result() {
  local ID=""; [ "${1:-}" = "--id" ] && ID="$2"
  local RUN; RUN=$(resolve_run "$ID") || exit 2
  echo "run: $(basename "$RUN") | state: $(json_get "$RUN/meta.json" state) | cost: \$$(json_get "$RUN/meta.json" cost_usd)"
  echo "--- Claude's answer ---"
  local ans; ans=$(result_field "$RUN/run.jsonl" result)
  if [ -n "$ans" ]; then echo "$ans"; else echo "(empty — Claude produced no final answer)"; fi
  echo
  summarize_changes "$RUN"
  local CWD BASE; CWD=$(json_get "$RUN/meta.json" cwd); BASE=$(json_get "$RUN/meta.json" baseline_tag)
  if [ -d "$CWD/.git" ]; then
    echo "--- changed files ---"; git -C "$CWD" status --short | head -40
    if [ -n "$BASE" ]; then
      local n; n=$(git -C "$CWD" rev-list --count "$BASE"..HEAD 2>/dev/null || echo 0)
      [ "$n" != "0" ] && { echo "--- commits by Claude ---"; git -C "$CWD" log --oneline "$BASE"..HEAD; }
    fi
  fi
}

cmd_cancel() {
  local ID=""; [ "${1:-}" = "--id" ] && ID="$2"
  local RUN; RUN=$(resolve_run "$ID") || exit 2
  local pid; pid=$(json_get "$RUN/meta.json" pid)
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null; sleep 1; kill -9 "$pid" 2>/dev/null
    json_set "$RUN/meta.json" state cancelled
    echo "run $(basename "$RUN") stopped"
  else
    echo "run $(basename "$RUN") is not active (state: $(json_get "$RUN/meta.json" state))"
  fi
}

cmd_list() {
  [ -d "$RUNS_DIR" ] || { echo "no runs yet"; return 0; }
  local d here; here=$(current_project)
  echo "  * this thread   . this project"
  for d in $(ls -1dt "$RUNS_DIR"/*/ 2>/dev/null | head -10); do
    d=${d%/}
    local st mark=" "
    st=$(json_get "$d/meta.json" state); st=${st:-"(unknown)"}
    [ "$(json_get "$d/meta.json" cwd)" = "$here" ] && mark="."
    [ -n "${CODEX_THREAD_ID:-}" ] && \
      [ "$(json_get "$d/meta.json" thread_id)" = "$CODEX_THREAD_ID" ] && mark="*"
    printf '%s %-46s %-12s %s\n' "$mark" "$(basename "$d")" "$st" "$(json_get "$d/meta.json" cwd)"
  done
}

# ---------- dispatch ----------
case "${1:-}" in
  run)    shift; cmd_run "$@";;
  review) shift; cmd_run --readonly "$@";;
  resume) shift; cmd_resume "$@";;
  status) shift; cmd_status "$@";;
  result) shift; cmd_result "$@";;
  cancel) shift; cmd_cancel "$@";;
  list)   shift; cmd_list "$@";;
  -h|--help|help|"") usage;;
  *) die "unknown mode: $1";;
esac
