---
name: claude-bridge
description: Delegating work to Claude Code through claudectl — the only sanctioned way to launch it. Use when a task should go to Claude, for Claude-run reviews, for checking on a run in progress ("how is it doing", "is it stuck"), for accepting the result, and for follow-ups.
---

# Claude Code through claudectl

This skill covers handing a task to Claude Code and taking the result back. It does not decide **which** work goes to Claude — that split belongs in the user's own instructions (`AGENTS.md`), and different people set it up differently. What is fixed here is the mechanics: Claude is launched **only** through `claudectl`.

## Hard rule: never invoke `claude` directly

Hand-assembled invocations fail in ways that are hard to read. Real traps already hit:

- `--add-dir` is variadic and swallows a trailing prompt argument, so the run dies with `Input must be provided either through stdin or as a prompt argument`. The prompt must go through stdin.
- `--permission-mode acceptEdits` allows file edits but still blocks commands, so a task that must run its own tests ends with `This command requires approval` and a half-finished job. Tasks need `auto`.
- `--output-format stream-json` needs `--verbose`, otherwise there is no event stream to watch.
- Claude Code refuses to work under `/tmp` — `Path is outside allowed working directories` — regardless of `--add-dir`.

All of that is handled inside `claudectl`. If a needed mode seems to be missing, extend `claudectl` rather than assembling a `claude` call by hand.

## Modes

```
claudectl run     --cwd DIR (--prompt TEXT | --prompt-file F) [--model M]
claudectl review  --cwd DIR [--base REF] [--prompt TEXT] [--model M]
claudectl resume  --id ID (--prompt TEXT | --prompt-file F)
claudectl status  [--id ID]
claudectl result  [--id ID]
claudectl cancel  [--id ID]
claudectl list
```

Without `--id`, commands act on the newest run started by this Codex thread, falling back to the newest run of the current project, and refuse outright when neither matches. Do not pass a working directory to those commands — there is no flag for it, and the binding is automatic on purpose.

## How to launch

`run`, `review` and `resume` block until the work is finished. Launch them in the background and keep working; never detach them with `nohup`, `&` or `disown`, or the run becomes invisible: no completion signal, no status, and both sides wait on something that finished long ago.

While a run is in flight, answer "how is it doing" with `claudectl status` — it reports the state, the time since the last event, and the latest steps, including any permission denials. Do not guess.

## States and what to do about them

| State | Meaning | Action |
|---|---|---|
| RUNNING | events are coming in | nothing; report the latest step |
| SILENT | no events for over three minutes | say so plainly, show the last step, offer `claudectl cancel` |
| DONE | finished normally | move to acceptance |
| FAILED | non-zero exit, or a result that is not `success` | show the tail of `err.log` |
| TRUNCATED | no result event at all | Claude died mid-run; accept what exists, then `claudectl resume` |

DONE does not mean "did the work": Claude can finish normally while reporting that it could not proceed — a denied command, a missing dependency. Always check its claims against the actual changes.

## Discipline around a run

1. **The spec.** If the user supplied a document, pass `--prompt-file` pointing at it and do not paraphrase. If the spec came out of the conversation, write it up, show it to the user and wait for confirmation, then launch.
2. **A clean tree.** If the project has uncommitted changes, say so before launching: otherwise acceptance cannot separate Claude's work from what was already there. The rollback tag is created by `claudectl` itself.
3. **Acceptance.** Run `claudectl result`, then `git diff` against the tag. If Claude claims the tests pass, find the corresponding command in the run log. Its word alone is not evidence.
4. **Cost is reported.** Every run prints what it cost in dollars; pass that on when the user asks about spend.
5. **Review is never automatic.** After acceptance, report the outcome and remind the user about review, asking who should do it — you or `claudectl review`. A "no" closes the topic immediately.

## Do not

- Invoke `claude` directly or improvise flags.
- Detach a run with `nohup` / `&` / `disown`.
- Commit or push Claude's work without being asked.
- Delegate on your own initiative when the user's instructions do not call for it.
- Report "done" without looking at the changes.
