#!/usr/bin/env python3
"""Claude Code bridge. Standard library only; macOS/Linux, Python 3.9+."""
import argparse
import collections
import datetime
import fcntl
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import uuid

TERMINAL = {'done', 'failed', 'incomplete', 'cancelled', 'timed_out'}
OPTIONS = ('model', 'effort', 'timeout', 'idle_timeout', 'max_budget_usd', 'max_turns')


def runs_dir():
    return Path(os.environ.get('CLAUDE_RUNS_DIR', str(Path.home() / '.codex/claude-runs'))).expanduser().resolve()


def read_json(path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {} if default is None else default


def atomic_json(path, data):
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    tmp.replace(path)


def git(cwd, *args):
    result = subprocess.run(['git', '-C', str(cwd), *args], capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else ''


def current_project():
    return git(Path.cwd(), 'rev-parse', '--show-toplevel') or str(Path.cwd())


def resolve_run(run_id=None):
    root = runs_dir()
    if run_id:
        if Path(run_id).name != run_id or run_id in ('.', '..'):
            raise ValueError('invalid run id')
        run = root / run_id
        if not (run / 'meta.json').is_file():
            raise ValueError('no such run: ' + run_id)
        cwd = read_json(run / 'meta.json').get('cwd')
        if cwd and cwd != current_project():
            print('WARNING: run belongs to ' + cwd, file=sys.stderr)
        return run
    candidates = [(p, read_json(p / 'meta.json')) for p in root.glob('*/')]
    for field, value in [('thread_id', os.environ.get('CODEX_THREAD_ID')), ('cwd', current_project())]:
        matches = [(p, m) for p, m in candidates if value and (
            str(Path(m.get(field, '')).resolve()) == str(Path(value).resolve())
            if field == 'cwd' and m.get(field) else m.get(field) == value)]
        if matches:
            return max(matches, key=lambda pair: float(pair[1].get('started_at') or 0))[0]
    raise ValueError('no run belongs to this thread or to ' + current_project() + ' — choose claudectl list, then --id ID')


def positive(value):
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError('must be a positive number')
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError('must be a positive number')
    return number


def positive_int(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError('must be a positive integer')
    return number


def parser():
    p = argparse.ArgumentParser(description='Run Claude Code with explicit model/effort, progress and cancellation.')
    sub = p.add_subparsers(dest='command')
    for name in ('run', 'review', 'resume'):
        s = sub.add_parser(name)
        if name == 'resume':
            s.add_argument('--id')
        else:
            s.add_argument('--cwd', required=True)
            s.add_argument('--base')
        prompt = s.add_mutually_exclusive_group(required=name != 'review')
        prompt.add_argument('--prompt')
        prompt.add_argument('--prompt-file')
        s.add_argument('--model')
        s.add_argument('--effort', choices=['low', 'medium', 'high', 'xhigh', 'max', 'ultracode'])
        s.add_argument('--timeout', type=positive, help='wall-clock limit in seconds')
        s.add_argument('--idle-timeout', type=positive, help='stop after this many seconds without stream events')
        s.add_argument('--max-budget-usd', type=positive)
        s.add_argument('--max-turns', type=positive_int)
        s.add_argument('--background', action='store_true')
    for name in ('status', 'watch', 'result', 'cancel', 'list'):
        s = sub.add_parser(name)
        if name != 'list':
            s.add_argument('--id')
        if name in ('status', 'list'):
            s.add_argument('--json', action='store_true')
        if name == 'watch':
            s.add_argument('--interval', type=positive, default=2)
            s.add_argument('--seconds', type=positive, default=60, help='observation window; does not cancel the run (default: 60)')
    return p


class EventLog:
    """Consume complete JSONL lines once, including lines split across writes."""
    def __init__(self, run):
        self.run = run
        self.offset = 0
        self.pending = b''
        self.recent = collections.deque(maxlen=12)
        self.tasks = {}
        self.tool_ids = set()
        self.workflow_ids = set()
        self.result = None
        self.progress = {'event_count': 0, 'tool_calls': 0, 'workflow_calls': 0}

    def note(self, text):
        self.recent.append(text[:400])
        print('[' + time.strftime('%H:%M:%S') + '] ' + text[:400], flush=True)

    def ingest(self, e):
        self.progress['event_count'] += 1
        self.progress['last_event_at'] = time.time()
        kind, subtype = e.get('type'), e.get('subtype')
        if kind == 'system' and subtype == 'init' and not e.get('parent_tool_use_id'):
            self.progress['actual_model'] = e.get('model')
            self.progress['session_id'] = e.get('session_id')
            self.note('INIT model=' + str(e.get('model', 'unknown')))
        elif kind == 'assistant':
            who = 'agent ' + str(e['parent_tool_use_id'])[:16] if e.get('parent_tool_use_id') else 'main'
            for block in (e.get('message') or {}).get('content') or []:
                if block.get('type') == 'tool_use':
                    tool_id = block.get('id')
                    if tool_id and tool_id in self.tool_ids:
                        continue
                    if tool_id:
                        self.tool_ids.add(tool_id)
                    self.progress['tool_calls'] += 1
                    name, inp = block.get('name', '?'), block.get('input') or {}
                    if name == 'Workflow':
                        self.workflow_ids.add(tool_id or str(self.progress['tool_calls']))
                        self.progress['workflow_calls'] = len(self.workflow_ids)
                    detail = inp.get('description') or inp.get('command') or inp.get('file_path') or inp.get('pattern') or ''
                    self.note(who + ' ' + name + ' ' + str(detail).replace('\n', ' '))
                elif block.get('type') == 'text' and block.get('text'):
                    self.note(who + ' says ' + block['text'].replace('\n', ' '))
        elif kind == 'system' and subtype in ('task_started', 'task_progress', 'task_notification'):
            task_id = str(e.get('task_id') or e.get('tool_use_id') or 'unknown')
            task = self.tasks.setdefault(task_id, {'id': task_id})
            for field in ('description', 'summary', 'usage', 'task_type', 'status', 'last_tool_name'):
                if e.get(field) is not None:
                    task[field] = e[field]
            task.setdefault('status', 'running')
            self.note(subtype + ' ' + task_id + ' ' + str(e.get('summary') or e.get('description') or e.get('usage') or ''))
        elif kind == 'tool_progress':
            self.note('tool_progress ' + str(e.get('tool_name', '')) + ' elapsed=' + str(e.get('elapsed_time_seconds', '?')) + 's')
        elif kind == 'system' and subtype == 'permission_denied':
            self.note('DENIED ' + str(e.get('tool_name', '?')) + ': ' + str(e.get('decision_reason', '')))
        elif kind == 'result' and not e.get('parent_tool_use_id'):
            self.result = e
            atomic_json(self.run / 'result.json', e)
            if e.get('permission_denials'):
                self.note('DENIED: ' + str(len(e['permission_denials'])) + ' tool request(s); inspect result.json')

    def poll(self):
        path = self.run / 'run.jsonl'
        if not path.exists():
            return
        with path.open('rb') as stream:
            stream.seek(self.offset)
            chunk = stream.read()
            self.offset = stream.tell()
        if not chunk:
            return
        parts = (self.pending + chunk).split(b'\n')
        self.pending = parts.pop()
        for line in parts:
            try:
                e = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(e, dict):
                self.ingest(e)
        self.progress['recent'] = list(self.recent)
        self.progress['tasks'] = list(self.tasks.values())
        atomic_json(self.run / 'progress.json', self.progress)


def alive(pid):
    try:
        if not pid:
            return False
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


def snapshot(run):
    m = read_json(run / 'meta.json')
    progress = read_json(run / 'progress.json')
    now = time.time()
    state = m.get('state', 'starting')
    started = float(m.get('started_at') or now)
    log = run / 'run.jsonl'
    last = progress.get('last_event_at') or (log.stat().st_mtime if log.exists() else started)
    if state not in TERMINAL:
        pid = m.get('worker_pid') or m.get('pid')
        if pid and not alive(pid):
            state = 'incomplete'
        elif not pid and now - started > 10:
            state = 'incomplete'
        elif (run / 'cancel.request').exists():
            state = 'cancelling'
        elif now - last > float(os.environ.get('CLAUDE_STALL_SECONDS', '180')):
            state = 'silent'
    label = {'incomplete': 'TRUNCATED', 'timed_out': 'TIMED_OUT'}.get(state, state.upper())
    ended = float(m.get('finished_at') or now)
    return {**m, 'state': state, 'status': label, 'elapsed_seconds': round(ended - started, 1),
            'last_event_age_seconds': round(max(0, now - last), 1), 'progress': progress, 'log': str(log)}


def show_status(s):
    print('run:      ' + s.get('id', '?'))
    print('state:    ' + s['status'])
    print('elapsed:  ' + str(s['elapsed_seconds']) + 's | last event ' + str(s['last_event_age_seconds']) + 's ago')
    print('project:  ' + s.get('cwd', '?'))
    p = s['progress']
    print('model:    requested=' + str(s.get('model') or '<default>') + ' actual=' + str(p.get('actual_model') or '<not reported>'))
    print('effort:   requested=' + str(s.get('effort') or s.get('effort_env') or '<default>'))
    print('limits:   ' + ', '.join(k + '=' + str(s[k]) for k in OPTIONS[2:] if s.get(k)) if any(s.get(k) for k in OPTIONS[2:]) else 'limits:   none')
    print('activity: ' + str(p.get('tool_calls', 0)) + ' tool calls, ' + str(p.get('workflow_calls', 0)) + ' Workflow calls observed')
    for task in p.get('tasks', []):
        print('task:     ' + task['id'] + ' ' + task.get('status', '?') + ' ' + str(task.get('description') or task.get('summary') or '')[:140])
        if task.get('usage'):
            print('usage:    ' + json.dumps(task['usage']))
    for line in p.get('recent', [])[-5:]:
        print('  ' + line)
    if s.get('stop_reason'):
        print('reason:   ' + s['stop_reason'])
    print('log:      ' + s['log'])


def claude_args(m):
    args = ['claude', '-p', '--output-format', 'stream-json', '--verbose', '--add-dir', m['cwd']]
    if m['mode'] == 'review':
        args += ['--restricted', '--permission-mode', 'acceptEdits']
    else:
        args += ['--permission-mode', 'auto']
    for field in ('model', 'effort', 'max_budget_usd', 'max_turns'):
        if m.get(field) is not None:
            args += ['--' + field.replace('_', '-'), str(m[field])]
    if m.get('forward_subagent_text'):
        args += ['--forward-subagent-text']
    args += ['--resume' if m.get('parent') else '--session-id', m['session_id']]
    return args


def stop_group(proc):
    # Claude owns a new process group; the supervising worker is outside it.
    # Also signal remaining children if the group leader has just exited.
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            break
        except PermissionError:
            # macOS can return EPERM for a group containing only an exited,
            # unreaped leader. Verify that no live group member remains; never
            # turn a real signalling failure into a successful cancellation.
            if proc.poll() is None:
                raise
            groups = subprocess.run(['ps', '-axo', 'pgid=,stat='], capture_output=True, text=True)
            if groups.returncode or any(
                fields[0] == str(proc.pid) and not fields[1].startswith('Z')
                for line in groups.stdout.splitlines()
                if len(fields := line.split()) >= 2
            ):
                raise
            break
        if sig == signal.SIGTERM:
            time.sleep(1)
    proc.wait()


def notify(run, m):
    if not m.get('thread_id') or not shutil.which('codex'):
        return
    message = 'claudectl: run ' + m['id'] + ' finished — ' + m['state']
    if m.get('cost_usd') is not None:
        message += ', cost $' + str(m['cost_usd'])
    message += '. See: claudectl result --id ' + m['id']
    try:
        r = subprocess.run(['codex', 'queue', '--thread', m['thread_id'], '--message', message],
                           capture_output=True, timeout=15)
        m['notification'] = 'sent' if r.returncode == 0 else 'failed'
    except (OSError, subprocess.TimeoutExpired):
        m['notification'] = 'failed'
    atomic_json(run / 'meta.json', m)
    if m['notification'] == 'failed':
        print('note: completion notification failed; use claudectl status/result', flush=True)


def run_worker(run, background=False):
    m = read_json(run / 'meta.json')
    proc = None
    interrupted = []
    previous = {}
    events = EventLog(run)
    lock_dir = run.parent / '.session-locks'
    lock_dir.mkdir(exist_ok=True)
    lock = (lock_dir / (m['session_id'] + '.lock')).open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        m.update(state='failed', stop_reason='another run owns this Claude session', finished_at=time.time())
        atomic_json(run / 'meta.json', m)
        print('OUTCOME: FAILED — ' + m['stop_reason'], flush=True)
        if background:
            notify(run, m)
        return 1
    try:
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous[sig] = signal.signal(sig, lambda signum, frame: interrupted.append(signum))
        m.update(worker_pid=os.getpid(), state='starting')
        atomic_json(run / 'meta.json', m)
        env = dict(os.environ)
        if m.get('effort'):
            # Claude's environment override has higher precedence than --effort.
            env.pop('CLAUDE_CODE_EFFORT_LEVEL', None)
        args = claude_args(m)
        (run / 'cmd.txt').write_text(shlex.join(args) + '\n')
        with (run / 'prompt.md').open('rb') as prompt, (run / 'run.jsonl').open('wb') as log, (run / 'err.log').open('wb') as err:
            if not (run / 'cancel.request').exists():
                proc = subprocess.Popen(args, cwd=m['cwd'], env=env, stdin=prompt, stdout=log,
                                        stderr=err, start_new_session=True)
                m.update(pid=proc.pid, state='running')
                atomic_json(run / 'meta.json', m)
            started = time.monotonic()
            reason, state = None, None
            while proc is not None:
                events.poll()
                if (run / 'cancel.request').exists() or interrupted:
                    state, reason = 'cancelled', 'cancel requested'
                    break
                if proc.poll() is not None:
                    break
                elapsed = time.monotonic() - started
                last = events.progress.get('last_event_at', m['started_at'])
                if m.get('timeout') and elapsed >= m['timeout']:
                    state, reason = 'timed_out', 'wall-clock timeout'
                    break
                if m.get('idle_timeout') and time.time() - last >= m['idle_timeout']:
                    state, reason = 'timed_out', 'idle timeout (no stream events)'
                    break
                time.sleep(0.2)
            if proc is None:
                state, reason = 'cancelled', 'cancelled before launch'
            elif state:
                stop_group(proc)
            else:
                proc.wait()
            events.poll()
        result = events.result or {}
        if not state:
            state = 'done' if proc.returncode == 0 and result.get('subtype') == 'success' and not result.get('is_error') else 'failed'
            if proc.returncode == 0 and not result:
                state = 'incomplete'
        m.update(state=state, exit_code=proc.returncode if proc else None, finished_at=time.time())
        if reason:
            m['stop_reason'] = reason
        # Preserve the original session ID when cancellation yields no result.
        m['session_id'] = result.get('session_id') or events.progress.get('session_id') or m['session_id']
        m['cost_usd'] = result.get('total_cost_usd')
        m['turns'] = result.get('num_turns')
        atomic_json(run / 'meta.json', m)
    except Exception as exc:
        if proc is not None:
            try:
                stop_group(proc)
            except OSError as cleanup_error:
                print('ERROR: process cleanup failed: ' + str(cleanup_error), file=sys.stderr)
        m.update(state='failed', stop_reason=str(exc), finished_at=time.time())
        atomic_json(run / 'meta.json', m)
        print('ERROR: ' + str(exc), file=sys.stderr)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        lock.close()
    print('OUTCOME: ' + m['state'].upper() + ' | run id: ' + m['id'], flush=True)
    if m.get('cost_usd') is not None:
        print('cost: $' + str(m['cost_usd']), flush=True)
    if background:
        notify(run, m)
    return 0 if m['state'] == 'done' else 1


def prepare(args):
    old, parent = {}, None
    if args.command == 'resume':
        parent = resolve_run(args.id)
        old = read_json(parent / 'meta.json')
        if snapshot(parent)['state'] not in TERMINAL:
            raise ValueError('run is still active; cancel it or wait before resuming')
        if not old.get('session_id'):
            raise ValueError('run has no session id to resume')
        cwd = Path(old['cwd']).resolve()
        mode = old.get('mode', 'run')
        if mode == 'resume':  # legacy metadata
            mode = 'run'
    else:
        cwd, mode = Path(args.cwd).expanduser().resolve(), args.command
    if not cwd.is_dir():
        raise ValueError('no such directory: ' + str(cwd))
    if cwd == Path('/tmp').resolve() or Path('/tmp').resolve() in cwd.parents:
        raise ValueError('Claude Code refuses to work under /tmp — use a directory under your home')
    if not shutil.which('claude'):
        raise ValueError('claude is not installed')
    if args.prompt_file:
        prompt = Path(args.prompt_file).expanduser().read_text()
    else:
        prompt = args.prompt or ''
    options = {k: getattr(args, k) if getattr(args, k) is not None else old.get(k) for k in OPTIONS}
    if options['effort'] == 'ultracode' and mode == 'review':
        raise ValueError('ultracode needs command/workflow tools; use run for a scoped analysis task, or review with another effort')
    forward = False
    if options['effort'] == 'ultracode':
        version = subprocess.run(['claude', '--version'], capture_output=True, text=True, timeout=10)
        match = re.search(r'(\d+)\.(\d+)\.(\d+)', version.stdout)
        if not match or tuple(map(int, match.groups())) < (2, 1, 211):
            raise ValueError('ultracode with subagent progress requires Claude Code >= 2.1.211')
        if os.environ.get('CLAUDE_CODE_DISABLE_WORKFLOWS', '').lower() in ('1', 'true'):
            raise ValueError('workflows are disabled by CLAUDE_CODE_DISABLE_WORKFLOWS')
        forward = True
    if mode == 'review' and not parent:
        target = 'changes relative to ' + args.base if args.base else 'uncommitted changes'
        prompt += '\nReview ' + target + '. Do not fix anything. List defects with file, line and reason. If none, say so.\n'
    if not prompt.strip():
        raise ValueError('prompt must not be empty')
    root = runs_dir()
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    run_id = re.sub(r'[^A-Za-z0-9_.-]', '-', cwd.name) + '-' + stamp + '-' + uuid.uuid4().hex[:8]
    run = root / run_id
    run.mkdir(mode=0o700)
    (run / 'prompt.md').write_text(prompt)
    head = git(cwd, 'rev-parse', 'HEAD')
    tag = old.get('baseline_tag', '')
    if mode == 'run' and head and not parent:
        tag = 'claude-baseline-' + stamp + '-' + uuid.uuid4().hex[:8]
        tagged = subprocess.run(['git', '-C', str(cwd), 'tag', tag], capture_output=True)
        if tagged.returncode:
            print('WARNING: baseline tag could not be created', file=sys.stderr)
            tag = ''
    m = {**options, 'id': run_id, 'cwd': str(cwd), 'mode': mode,
         'parent': parent.name if parent else None, 'baseline_sha': old.get('baseline_sha') or head,
         'baseline_tag': tag, 'started_at': time.time(), 'state': 'starting',
         'session_id': old.get('session_id') or str(uuid.uuid4()),
         'thread_id': os.environ.get('CODEX_THREAD_ID', ''), 'forward_subagent_text': forward,
         'effort_env': os.environ.get('CLAUDE_CODE_EFFORT_LEVEL') if not options['effort'] else None}
    atomic_json(run / 'meta.json', m)
    print('run ' + run_id + ' | mode ' + mode + ' | model ' + str(options['model'] or '<default>') + ' | effort ' + str(options['effort'] or m['effort_env'] or '<default>'), flush=True)
    if tag:
        print('rollback tag: ' + tag, flush=True)
    if args.background:
        with (run / 'console.log').open('ab') as log:
            subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '__background', str(run)],
                             stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True, close_fds=True)
        print('started in the background: ' + run_id)
        print('watch:  claudectl watch --id ' + run_id)
        print('result: claudectl result --id ' + run_id)
        print('a message will be queued to this Codex session when it ends' if m['thread_id'] else 'note: CODEX_THREAD_ID is not set; no completion message will be sent')
        return 0
    return run_worker(run)


def cancel(run):
    s = snapshot(run)
    if s['state'] in TERMINAL:
        print('run ' + run.name + ' is not active (' + s['state'] + ')')
        return 0
    if not s.get('worker_pid') and s['state'] != 'starting':
        raise ValueError('legacy run has no supervising worker; inspect its process before stopping it')
    # The owner handles signals and writes the final verdict, never this client.
    (run / 'cancel.request').touch()
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        final = snapshot(run)
        if final['state'] in TERMINAL:
            print('run ' + run.name + ': ' + final['status'])
            return 0 if final['state'] in ('cancelled', 'done', 'timed_out') else 1
        time.sleep(0.2)
    print('cancellation requested; worker has not confirmed yet — inspect claudectl status', file=sys.stderr)
    return 1


def show_result(run):
    m = read_json(run / 'meta.json')
    r = read_json(run / 'result.json')
    if not r and (run / 'run.jsonl').exists():
        for line in (run / 'run.jsonl').open(errors='replace'):
            try:
                e = json.loads(line)
                if e.get('type') == 'result' and not e.get('parent_tool_use_id'):
                    r = e
            except ValueError:
                pass
    print('run: ' + run.name + ' | state: ' + m.get('state', '?') + ' | cost: ' + str(m.get('cost_usd') if m.get('cost_usd') is not None else 'not reported'))
    print(r.get('result') or '(no final answer; inspect run.jsonl and any partial changes)')
    if m.get('stop_reason'):
        print('reason: ' + m['stop_reason'])
    if r.get('permission_denials'):
        print('permission denials: ' + json.dumps(r['permission_denials'], ensure_ascii=False))
    if m.get('state') != 'done' and (run / 'err.log').exists():
        print('\n'.join((run / 'err.log').read_text(errors='replace').splitlines()[-8:]))
    print('--- current changed files (includes pre-existing work) ---')
    print(git(m['cwd'], 'status', '--short'))
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == '__background':
        return run_worker(Path(argv[1]), background=True)
    p = parser()
    if not argv or argv == ['help']:
        p.print_help()
        return 0
    args = p.parse_args(argv)
    if args.command in ('run', 'review', 'resume'):
        return prepare(args)
    if args.command == 'list':
        rows = [snapshot(d) for d in runs_dir().glob('*/') if (d / 'meta.json').is_file()]
        rows.sort(key=lambda s: float(s.get('started_at') or 0), reverse=True)
        if args.json:
            print(json.dumps(rows[:10], ensure_ascii=False))
        else:
            print('  * this thread   . this project')
            for s in rows[:10]:
                mark = '*' if s.get('thread_id') and s['thread_id'] == os.environ.get('CODEX_THREAD_ID') else '.' if s.get('cwd') == current_project() else ' '
                print(mark + ' ' + s['id'] + ' ' + s['status'] + ' ' + s.get('cwd', ''))
        return 0
    run = resolve_run(args.id)
    if args.command == 'cancel':
        return cancel(run)
    if args.command == 'result':
        return show_result(run)
    if args.command == 'status':
        s = snapshot(run)
        print(json.dumps(s, ensure_ascii=False)) if args.json else show_status(s)
        return 0
    if args.command == 'watch':
        deadline = time.monotonic() + args.seconds
        while True:
            s = snapshot(run)
            show_status(s)
            if s['state'] in TERMINAL:
                return 0 if s['state'] == 'done' else 1
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                print('observation window ended; the run continues')
                return 0
            time.sleep(min(args.interval, remaining))


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        print('ERROR: ' + str(exc), file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        sys.exit(130)
