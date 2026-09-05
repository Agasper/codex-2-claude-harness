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

Requires Claude Code (`claude`) already installed and signed in, plus Python 3, Git and Bash.

## claudectl

```
claudectl run     --cwd DIR (--prompt TEXT | --prompt-file F) [--model M]
claudectl review  --cwd DIR [--base REF] [--prompt TEXT] [--model M]
claudectl resume  --id ID  (--prompt TEXT | --prompt-file F)
claudectl status  [--id ID]
claudectl result  [--id ID]
claudectl cancel  [--id ID]
claudectl list
```

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

Claude Code has no OS-level sandbox — permissions are enforced inside its own harness, so the model of safety differs from Codex's:

- **`run`** uses `--permission-mode auto`: Claude edits files and runs commands, judging each one. Access is limited to the project directory via `--add-dir`.
- **`review`** adds `--restricted`, which removes Bash and the other command-running tools outright. A reviewer has no business executing anything.
- Before every `run`, `claudectl` creates a git tag `claude-baseline-<timestamp>` as a rollback point and as the base for the acceptance diff.
- Claude can delete anything inside the project, exactly as any agent with edit access can. The tag is what makes that recoverable.

## Development

```bash
tests/smoke.sh   # checks that need no Claude call and cost nothing
```

## License

MIT
