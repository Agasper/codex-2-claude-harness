---
name: claude-bridge
description: Delegate implementation, analysis, or review to Claude Code through claudectl, including UltraCode workflows. Use for explicit Claude delegation, monitoring progress, cancelling long runs, accepting results, or continuing a Claude session.
---

# Claude Code through claudectl

Use `claudectl` for Claude task launches rather than assembling `claude -p` commands. It supplies the permission mode, stdin prompt, stream format, process supervision, and completion notification. Choose which work to delegate from the user's instructions; installing this skill does not authorize automatic delegation of every task.

## Launch

```bash
claudectl run --cwd /path/to/project --prompt-file task.md --background
claudectl run --cwd /path/to/project --prompt-file task.md --model opus --effort ultracode --background
claudectl review --cwd /path/to/project --prompt-file review.md --effort high --background
```

`--effort` accepts `low`, `medium`, `high`, `xhigh`, `max`, and `ultracode`. Preserve the user's model selection; omit `--model` to use their Claude default. UltraCode forwards the real `--effort ultracode` flag and subagent events. Writing "ultracode" only in a print-mode prompt is insufficient. The requested mode can be constrained by the model or organization settings; do not claim a workflow ran unless the event log shows it.

Optional controls on `run`, `review`, and `resume`:

- `--timeout SECONDS`: wall-clock limit for this attempt.
- `--idle-timeout SECONDS`: stop after no valid stream events for this interval. Silence can also mean a slow request.
- `--max-budget-usd AMOUNT`, `--max-turns COUNT`: Claude's own limits. Report dollar figures as CLI accounting, not verified subscription charges.

Use concrete scope, allowed files and acceptance checks in the prompt. Pass a user-supplied task document unchanged. Existing authorization for a concrete task remains valid; ask only when material scope or authority is missing.

Before write tasks, inspect dirty/untracked files and avoid overlap with other writers. The automatic baseline tag records HEAD only, not those files. Use a suitable local snapshot or isolated checkout if needed. The working directory cannot be under `/tmp`; prompt files can.

`review` disables command tools but still exposes file edits. Supply the prepared diff and relevant new files, forbid fixes in the prompt, and check the final diff. `--base` alone does not collect a diff. UltraCode is rejected in `review` because workflows need command tools. When the user explicitly requests an UltraCode analysis, use `run` with a narrow analysis-only prompt and disclose its normal implementation permissions when relevant.

## Monitor and stop

```bash
claudectl status --id RUN_ID
claudectl status --id RUN_ID --json
claudectl watch --id RUN_ID --seconds 45 --interval 3
claudectl result --id RUN_ID
claudectl cancel --id RUN_ID
claudectl list --json
```

Use `--background` for work expected to take more than a minute. Keep the returned ID. Never improvise `nohup`, `&`, or `disown`. Completion is queued to the originating Codex task when `CODEX_THREAD_ID` is present; check `meta.json`/`console.log` if delivery fails.

`status`/`watch` report the actual model from Claude's initialization, requested effort, elapsed time, tools, observed Workflow calls, and task lifecycle/usage events when available. The full stream is in `~/.codex/claude-runs/ID/run.jsonl`; `progress.json` is a compact snapshot. This is event visibility, not percentage complete or exhaustive workflow internals. Do not infer completion from silence.

`watch` ends after 60 seconds by default and leaves the run active; use a window of at most 60 seconds when calling from Codex so the user still receives updates. Ctrl-C in the watcher does not cancel Claude.

- `RUNNING` / `STARTING`: the worker owns the run.
- `SILENT`: no stream events for over three minutes by default; inspect the last activity.
- `CANCELLING`: stop requested, awaiting worker confirmation.
- `CANCELLED`: the worker completed cancellation.
- `TIMED_OUT`: the worker stopped at a supplied time limit.
- `DONE`: the parent result reports success; acceptance is still required.
- `FAILED` / `TRUNCATED`: inspect `err.log`, `stop_reason`, and partial changes.

Cancellation terminates the local Claude process group and retains its session ID. Independently detached processes and remote work are outside that group. Already written files are not rolled back. If cancellation cannot be confirmed, say so; do not report it as stopped.

## Follow up and accept

```bash
claudectl resume --id RUN_ID --prompt-file follow-up.md --background
claudectl resume --id RUN_ID --prompt 'Narrow follow-up' --effort high --model opus --background
```

`resume` inherits explicit model, effort, limits, and run/review permissions unless overridden. Limits restart per attempt. Wait for completion or cancel the active attempt first; a session lock also prevents concurrent resumes. Cancellation preserves the ID, but if Claude never created a transcript, start a new run instead. There is no live prompt injection or pause command.

Read `result`, inspect the real diff, and verify claimed tests from logs. `DONE` alone does not prove the requested work was accomplished. Obtain review, commit, or push only when the user's instructions call for it.
