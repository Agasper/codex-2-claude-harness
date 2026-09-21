# codex-2-claude-harness

<p align="center">
  <img src="assets/hero.png" alt="Codex walking Claude Code on a leash" width="640">
</p>

A [Codex](https://developers.openai.com/codex/cli/) plugin that hands work to [Claude Code](https://claude.com/claude-code) and brings the result back in a form you can act on.

It is the mirror image of [claude-2-codex-harness](https://github.com/Agasper/claude-2-codex-harness): same idea, opposite direction. Codex stays the environment you work in; Claude is called when you want it.

## Why it exists

You can call Claude Code by hand with `claude -p`. Four things make that unreliable, and each is handled here.

**Flags fail in ways that are hard to read.** `--add-dir` is variadic, so a trailing prompt argument gets swallowed and the run dies with `Input must be provided either through stdin or as a prompt argument` — the prompt has to go through stdin. `--output-format stream-json` produces nothing useful without `--verbose`. Claude Code refuses to work under `/tmp` at all, reporting `Path is outside allowed working directories` no matter what `--add-dir` says.

**The permission mode decides whether the job can finish.** `--permission-mode acceptEdits` lets Claude edit files but still blocks commands, so a task told to run its own tests ends with `This command requires approval` and a half-done job. Tasks need `auto`. All verified against live runs, not read off a help page.

**Runs get lost.** A process detached with `nohup` or `&` stops reporting: no completion signal, no visible status.

**You cannot tell a working run from a stuck one.** `claudectl status` distinguishes **RUNNING**, **SILENT** (no events for over three minutes), **DONE**, **FAILED** and **TRUNCATED**, and surfaces permission denials as they happen.

## Install

```bash
codex plugin marketplace add Agasper/codex-2-claude-harness
codex plugin add claude-bridge@codex-2-claude-harness
```

Then run `plugins/claude-bridge/scripts/install.sh` to check prerequisites and put `claudectl` on your `PATH`.

Requires Claude Code (`claude`) already installed and signed in, Python 3.9+, Git and Bash on macOS/Linux. UltraCode with forwarded subagent events requires Claude Code 2.1.211 or later (2.1.219+ for nested subagent events).

## claudectl

```
claudectl run     --cwd DIR (--prompt TEXT | --prompt-file F) [--model M]
claudectl review  --cwd DIR [--base REF] [--prompt TEXT] [--model M]
claudectl resume  --id ID  (--prompt TEXT | --prompt-file F)
claudectl status  [--id ID] [--json]
claudectl watch   [--id ID] [--seconds 60] [--interval 2]
claudectl result  [--id ID]
claudectl cancel  [--id ID]
claudectl list    [--json]
```

### UltraCode and per-run controls

```bash
claudectl run --cwd /path/to/project --prompt-file task.md \
  --model opus --effort ultracode --background \
  --timeout 1800 --idle-timeout 300 --max-budget-usd 5 --max-turns 80

claudectl status --id RUN_ID
claudectl watch --id RUN_ID --seconds 60
claudectl status --id RUN_ID --json
claudectl cancel --id RUN_ID
claudectl resume --id RUN_ID --prompt-file follow-up.md --background
```

`--effort` accepts `low`, `medium`, `high`, `xhigh`, `max`, or `ultracode`. UltraCode requests `xhigh` reasoning and enables Claude's dynamic workflow orchestration. The flag is passed to Claude; just writing "ultracode" in a `-p` prompt does not enable the keyword trigger. Models, account settings and organization caps can limit availability. See [Claude model configuration](https://code.claude.com/docs/en/model-config#adjust-effort-level) and [dynamic workflows](https://code.claude.com/docs/en/workflows).

All limits are opt-in. `--timeout` measures wall-clock seconds; `--idle-timeout` measures seconds without valid stream events, which is not proof of a stuck model. `--max-budget-usd` and `--max-turns` are forwarded to Claude Code. The dollar cap uses Claude's API cost accounting, not a promise about subscription billing. The wall-clock timer begins when the worker launches Claude. An explicit `--effort` takes precedence over an inherited `CLAUDE_CODE_EFFORT_LEVEL` for this child only; organization restrictions remain in force.

`resume` inherits the requested model, effort, limits, and review/implementation mode unless overridden. Limits restart for each follow-up. A session lock prevents two runs from resuming the same Claude session concurrently. Active runs must finish or be cancelled before a follow-up. No live prompt injection or pause is implemented.

### Observability and stopping

The supervisor records Claude's stream incrementally. `status` and `watch` show the requested model/effort, actual model reported by Claude, elapsed time, recent tools, observed `Workflow` calls, and task progress/usage when Claude emits it. UltraCode forwards subagent messages with `--forward-subagent-text`. These are observed events, not a percentage-complete estimate or a guarantee that every internal workflow stage is exposed. Requested UltraCode is not proof that a workflow ran: check observed `Workflow` events.

`watch` observes for 60 seconds by default, then exits without cancelling the task; Ctrl-C only stops the watcher. Repeat it to observe again. `status --json` and `list --json` provide machine-readable snapshots. Full events remain in `run.jsonl`; `progress.json` is the latest compact snapshot, and `result.json` holds the final parent result.

`cancel` sends a request to the supervising worker. The worker terminates Claude's dedicated POSIX process group (TERM, then KILL after a grace period), retains the session ID and records `CANCELLED`. Time limits use the same path and record `TIMED_OUT`. This covers child processes that stay in that group, not independently detached or remote work. Files already changed remain on disk; cancellation is not rollback. A resume needs Claude's persisted transcript; cancelling before Claude has created it may require a new run.

Old run logs remain readable. Cancellation of a still-active legacy run without a supervisor is refused rather than signalling a potentially stale PID. `SILENT` is a warning after three minutes without events, configurable with `CLAUDE_STALL_SECONDS`; it does not stop a run unless an idle timeout was explicitly supplied.

The Bash entry point remains compatible with PATH symlinks. Its Python standard-library supervisor replaces the old shell polling loop so cancellation, process groups, deadlines, atomic status writes, and incremental event parsing have one owner.

### Background runs

`run`, `review` and `resume` block by default. Add `--background` and the command
returns immediately with a run id; when the work finishes, the outcome is queued
back into the calling Codex session with `codex queue`, so Codex keeps working
meanwhile and still learns when the run ended:

```
claudectl review --cwd . --background
started in the background: myrepo-20260905-182720
watch:  claudectl status --id myrepo-20260905-182720
a message will be queued to this Codex session when it ends
```

The completion message needs `CODEX_THREAD_ID`, which Codex sets for the commands
it runs. Started from a plain shell, the run still works — it just has nobody to
notify, and says so.

Each run is stored under `~/.codex/claude-runs/<id>/`: the prompt, the event log, run metadata. Every finished run reports what it cost in dollars, taken from Claude's own result event.

### Which run a command acts on

`--id` is optional. Without it, commands resolve the target in this order:

1. the newest run **started by the current Codex thread**;
2. failing that, the newest run **in the current project**;
3. failing both, the command refuses and tells you to pick one with `claudectl list`.

This matters most for `resume`, which inherits the working directory of the run it continues: choosing a run *is* choosing a project. A plain "most recent run on this machine" would let a second Codex thread, working on a different repository, silently steer your follow-up into it. `claudectl list` marks runs from this thread with `*` and runs from this project with `.`.

## What Claude is allowed to do

The bridge uses Claude Code's permission modes and does not add an OS-level sandbox:

- **`run`** uses `--permission-mode auto`: Claude edits files and runs commands, judging each one. `--add-dir` grants access; it is not an OS sandbox.
- **`review`** adds `--restricted`, which removes command-running tools. It still uses `acceptEdits`, so prompts must explicitly forbid fixes and the resulting diff must be checked. Provide a prepared diff in the prompt file: `--base` describes the comparison but does not collect the diff. UltraCode is rejected in this restricted mode because workflows need command tools; a scoped analysis task can use `run --effort ultracode` with its normal permissions.
- Before every `run`, `claudectl` creates a git tag `claude-baseline-<timestamp>` as a rollback point and as the base for the acceptance diff.
- Claude can delete anything inside the project, exactly as any agent with edit access can. The tag saves only HEAD, not dirty or untracked files. Preserve those separately before launching a writer.

## Development

```bash
tests/smoke.sh
python3 -m unittest discover -s tests -p 'test_*.py' -v
# Both suites are offline: integration tests substitute fake claude/codex binaries.
```

## License

MIT
