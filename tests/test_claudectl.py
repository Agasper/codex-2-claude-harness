#!/usr/bin/env python3
"""Offline integration tests: a fake Claude exercises the actual process runner."""
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
CTL = ROOT / 'plugins/claude-bridge/scripts/claudectl.sh'
RUNTIME = CTL.with_name('claudectl.py')
spec = importlib.util.spec_from_file_location('claudectl', RUNTIME)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

FAKE = r'''#!/usr/bin/env python3
import json,os,pathlib,signal,subprocess,sys,time
if '--version' in sys.argv:
    print(os.environ.get('FAKE_VERSION','2.1.278 (Claude Code)')); sys.exit()
root=pathlib.Path(os.environ['FAKE_ROOT'])
(root/'argv.json').write_text(json.dumps(sys.argv[1:]))
(root/'effort-env.txt').write_text(os.environ.get('CLAUDE_CODE_EFFORT_LEVEL','<unset>'))
(root/'prompt.txt').write_text(sys.stdin.read())
mode=os.environ.get('FAKE_MODE','success')
def emit(e): print(json.dumps(e),flush=True)
sid=sys.argv[sys.argv.index('--resume')+1] if '--resume' in sys.argv else sys.argv[sys.argv.index('--session-id')+1]
if mode=='fail':
    print('deliberate launch failure',file=sys.stderr); sys.exit(7)
emit({'type':'system','subtype':'init','model':'fake-opus','session_id':sid})
if mode=='hang':
    code='import os,pathlib,signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); pathlib.Path(os.environ["FAKE_ROOT"],"child.pid").write_text(str(os.getpid())); time.sleep(60)'
    child=subprocess.Popen([sys.executable,'-c',code])
    signal.signal(signal.SIGTERM,signal.SIG_IGN)
    while True: time.sleep(.1)
if mode=='active':
    for i in range(100):
        emit({'type':'tool_progress','tool_name':'Workflow','elapsed_time_seconds':i}); time.sleep(.1)
if mode=='truncated': sys.exit()
if mode=='slow': time.sleep(.5)
emit({'type':'assistant','message':{'content':[{'type':'tool_use','id':'wf1','name':'Workflow','input':{'description':'two checks'}}]}})
emit({'type':'system','subtype':'task_started','task_id':'task1','description':'verify fixture','task_type':'workflow'})
emit({'type':'assistant','parent_tool_use_id':'agent1','message':{'content':[{'type':'tool_use','id':'read1','name':'Read','input':{'file_path':'fixture.txt'}}]}})
emit({'type':'system','subtype':'task_progress','task_id':'task1','usage':{'total_tokens':42,'tool_uses':2,'duration_ms':123}})
emit({'type':'system','subtype':'task_notification','task_id':'task1','status':'completed','summary':'fixture verified'})
emit({'type':'result','parent_tool_use_id':'agent1','subtype':'success','result':'nested result must not win','session_id':'wrong'})
r={'type':'result','subtype':'success','result':'FAKE_OK','session_id':sid,'total_cost_usd':.01,'num_turns':2}
if mode=='denied': r.update(is_error=True,permission_denials=[{'tool_name':'Workflow'}])
emit(r)
'''


class RunnerTests(unittest.TestCase):
    def setUp(self):
        base = Path(tempfile.gettempdir()).resolve()
        directory = '/var/tmp' if base == Path('/tmp').resolve() else None
        self.temp = tempfile.TemporaryDirectory(prefix='claudectl-test-', dir=directory)
        self.root = Path(self.temp.name).resolve()
        self.project = self.root / 'project with spaces'
        self.project.mkdir()
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        fake = self.bin / 'claude'
        fake.write_text(FAKE)
        fake.chmod(0o755)
        self.runs = self.root / 'runs'
        self.env = {**os.environ, 'PATH': str(self.bin)+os.pathsep+os.environ['PATH'],
                    'CLAUDE_RUNS_DIR': str(self.runs), 'FAKE_ROOT': str(self.root),
                    'CODEX_THREAD_ID': '', 'CLAUDE_CODE_DISABLE_WORKFLOWS': '',
                    'CLAUDE_CODE_EFFORT_LEVEL': ''}

    def tearDown(self):
        for p in self.runs.glob('*/meta.json'):
            m=json.loads(p.read_text())
            if m.get('state') in module.TERMINAL:
                continue
            for pid in [m.get('pid'), m.get('worker_pid')]:
                if pid:
                    try: os.killpg(int(pid),signal.SIGKILL)
                    except OSError: pass
        self.temp.cleanup()

    def cli(self,*args,code=0,env=None):
        r=subprocess.run(['bash',str(CTL),*args],cwd=self.project,env={**self.env,**(env or {})},capture_output=True,text=True,timeout=15)
        self.assertEqual(r.returncode,code,r.stdout+r.stderr)
        return r

    def run_cli(self,*args,**kwargs):
        return self.cli('run','--cwd',str(self.project),'--prompt','test task',*args,**kwargs)

    def newest(self):
        return max(self.runs.glob('*/meta.json'),key=lambda p:p.stat().st_mtime).parent

    def wait_for(self,predicate):
        end=time.monotonic()+8
        while time.monotonic()<end:
            if predicate(): return
            time.sleep(.03)
        self.fail('condition did not become true')

    def meta(self,run):
        return json.loads((run/'meta.json').read_text())

    def test_ultracode_flags_and_progress(self):
        self.run_cli('--model','opus','--effort','ultracode','--max-budget-usd','2','--max-turns','5',env={'CLAUDE_CODE_EFFORT_LEVEL':'low'})
        argv=json.loads((self.root/'argv.json').read_text())
        for flag,value in [('--model','opus'),('--effort','ultracode'),('--max-budget-usd','2.0'),('--max-turns','5')]:
            self.assertEqual(argv[argv.index(flag)+1],value)
        self.assertIn('--forward-subagent-text',argv)
        self.assertEqual((self.root/'effort-env.txt').read_text(),'<unset>')
        s=json.loads(self.cli('status','--id',self.newest().name,'--json').stdout)
        self.assertEqual(s['progress']['actual_model'],'fake-opus')
        self.assertEqual(s['progress']['workflow_calls'],1)
        self.assertEqual(s['progress']['tool_calls'],2)
        self.assertEqual(s['progress']['tasks'][0]['status'],'completed')
        self.assertEqual(s['progress']['tasks'][0]['usage']['total_tokens'],42)
        self.assertEqual(s['session_id'],self.meta(self.newest())['session_id'])
        self.assertNotEqual(s['session_id'],'wrong')
        self.assertIn('FAKE_OK',self.cli('result','--id',self.newest().name).stdout)

    def test_resume_inherits_and_overrides_options(self):
        self.run_cli('--model','opus','--effort','ultracode','--timeout','30','--idle-timeout','20','--max-turns','6')
        old=self.newest()
        self.cli('resume','--id',old.name,'--prompt','follow up')
        new=self.meta(self.newest())
        self.assertEqual(new['session_id'],self.meta(old)['session_id'])
        self.assertEqual([new[k] for k in module.OPTIONS[:4]],['opus','ultracode',30,20])
        self.cli('resume','--id',self.newest().name,'--prompt','again','--effort','high','--model','sonnet')
        self.assertEqual(self.meta(self.newest())['effort'],'high')
        self.assertEqual(self.meta(self.newest())['model'],'sonnet')

    def test_review_resume_preserves_restriction(self):
        self.cli('review','--cwd',str(self.project))
        self.cli('resume','--id',self.newest().name,'--prompt','review more')
        argv=json.loads((self.root/'argv.json').read_text())
        self.assertIn('--restricted',argv)
        self.assertEqual(argv[argv.index('--permission-mode')+1],'acceptEdits')

    def test_invalid_options_create_no_run(self):
        for args in [('--effort','ultra'),('--effort',),('--timeout','0'),('--timeout','nan'),('--max-turns','-1'),('--max-budget-usd','inf'),('--prompt-file','/no/such/file')]:
            self.cli('run','--cwd',str(self.project),'--prompt','x',*args,code=2)
        self.assertFalse(self.runs.exists())

    def test_missing_prompt_file_is_rejected(self):
        self.cli('run','--cwd',str(self.project),'--prompt-file','/no/such/file',code=2)
        self.assertFalse(self.runs.exists())

    def test_ultracode_version_and_disabled_workflows(self):
        self.run_cli('--effort','ultracode',code=2,env={'FAKE_VERSION':'2.1.202'})
        self.run_cli('--effort','ultracode',code=2,env={'CLAUDE_CODE_DISABLE_WORKFLOWS':'1'})
        self.cli('review','--cwd',str(self.project),'--effort','ultracode',code=2)
        self.assertFalse(self.runs.exists())

    def test_failure_and_truncation_keep_session_id(self):
        for mode,state in [('fail','failed'),('truncated','incomplete'),('denied','failed')]:
            self.run_cli(code=1,env={'FAKE_MODE':mode})
            m=self.meta(self.newest())
            self.assertEqual(m['state'],state)
            self.assertTrue(m['session_id'])

    def test_background_cancel_group_and_resume(self):
        self.run_cli('--effort','ultracode','--background',env={'FAKE_MODE':'hang'})
        run=self.newest()
        self.wait_for(lambda:(self.root/'child.pid').exists())
        child=int((self.root/'child.pid').read_text())
        self.cli('resume','--id',run.name,'--prompt','not yet',code=2)
        self.cli('cancel','--id',run.name)
        self.assertEqual(self.meta(run)['state'],'cancelled')
        self.assertTrue(self.meta(run)['session_id'])
        # A killed child can briefly be a zombie, but must not be running.
        ps=subprocess.run(['ps','-o','stat=','-p',str(child)],capture_output=True,text=True)
        self.assertTrue(not ps.stdout.strip() or ps.stdout.strip().startswith('Z'),ps.stdout)
        time.sleep(.3)
        self.assertEqual(self.meta(run)['state'],'cancelled')
        self.cli('resume','--id',run.name,'--prompt','continue')
        self.assertEqual(self.meta(self.newest())['session_id'],self.meta(run)['session_id'])

    def test_cancel_immediately_after_background_launch(self):
        self.run_cli('--background',env={'FAKE_MODE':'hang'})
        run=self.newest()
        self.cli('cancel','--id',run.name)
        self.assertEqual(self.meta(run)['state'],'cancelled')

    def test_wall_clock_and_idle_timeout(self):
        for flag,mode,reason in [('--timeout','active','wall-clock timeout'),('--idle-timeout','hang','idle timeout (no stream events)')]:
            self.run_cli(flag,'.5',code=1,env={'FAKE_MODE':mode})
            m=self.meta(self.newest())
            self.assertEqual(m['state'],'timed_out')
            self.assertEqual(m['stop_reason'],reason)
            self.assertTrue(m['session_id'])

    def test_watch_observation_does_not_cancel(self):
        self.run_cli('--background',env={'FAKE_MODE':'hang'})
        run=self.newest()
        out=self.cli('watch','--id',run.name,'--seconds','.3','--interval','.1').stdout
        self.assertIn('observation window ended',out)
        self.assertIn(self.meta(run)['state'],('running','starting'))
        self.cli('cancel','--id',run.name)

    def test_background_completion_notification(self):
        codex=self.bin/'codex'
        codex.write_text('#!/usr/bin/env python3\nimport os,pathlib,sys\npathlib.Path(os.environ["FAKE_ROOT"],"notification.txt").write_text(repr(sys.argv))\n')
        codex.chmod(0o755)
        self.run_cli('--background',env={'CODEX_THREAD_ID':'test-thread'})
        run=self.newest()
        self.wait_for(lambda:self.meta(run).get('notification')=='sent')
        self.assertIn('test-thread',(self.root/'notification.txt').read_text())
        self.assertEqual(self.meta(run)['state'],'done')

    def test_notification_failure_does_not_change_success(self):
        codex=self.bin/'codex'
        codex.write_text('#!/bin/sh\nexit 1\n'); codex.chmod(0o755)
        self.run_cli('--background',env={'CODEX_THREAD_ID':'test-thread'})
        run=self.newest()
        self.wait_for(lambda:self.meta(run).get('notification')=='failed')
        self.assertEqual(self.meta(run)['state'],'done')

    def test_cancel_does_not_report_success_when_worker_cleanup_fails(self):
        run=self.root/'cancel-failure'; run.mkdir()
        snapshots=[{'state':'running','worker_pid':123}, {'state':'failed','status':'FAILED'}]
        with patch.object(module,'snapshot',side_effect=snapshots):
            self.assertEqual(module.cancel(run),1)
        self.assertTrue((run/'cancel.request').exists())

    def test_concurrent_resume_is_rejected_by_session_lock(self):
        self.run_cli()
        original=self.newest()
        self.cli('resume','--id',original.name,'--prompt','first follow up','--background',env={'FAKE_MODE':'hang'})
        running=self.newest()
        self.wait_for(lambda:self.meta(running).get('state')=='running')
        self.cli('resume','--id',original.name,'--prompt','second follow up',code=1)
        self.assertEqual(self.meta(self.newest())['stop_reason'],'another run owns this Claude session')
        self.cli('cancel','--id',running.name)

    def test_log_partial_lines_and_nested_result(self):
        run=self.root/'event-test'; run.mkdir()
        log=run/'run.jsonl'
        log.write_bytes(b'{"type":"system","subtype":"task_')
        events=module.EventLog(run); events.poll()
        self.assertEqual(events.progress['event_count'],0)
        with log.open('ab') as f: f.write(b'started","task_id":"a"}\ninvalid\n')
        events.poll(); events.poll()
        self.assertEqual(events.progress['event_count'],1)
        self.assertEqual(len(events.tasks),1)

    def test_symlink_entry_point_and_prompt_file(self):
        link=self.root/'claudectl'; link.symlink_to(CTL)
        prompt=self.root/'prompt file.md'; prompt.write_text('literal $HOME `do not execute`\nsecond line\n')
        r=subprocess.run([str(link),'run','--cwd',str(self.project),'--prompt-file',str(prompt)],env=self.env,capture_output=True,text=True)
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertEqual((self.root/'prompt.txt').read_text(),prompt.read_text())

    def test_legacy_metadata_status_result(self):
        run=self.runs/'legacy'; run.mkdir(parents=True)
        (run/'meta.json').write_text(json.dumps({'id':'legacy','cwd':str(self.project),'state':'done','started_at':'100'}))
        (run/'run.jsonl').write_text('{"type":"result","result":"old answer"}\n')
        self.assertIn('DONE',self.cli('status','--id','legacy').stdout)
        self.assertIn('old answer',self.cli('result','--id','legacy').stdout)


if __name__=='__main__': unittest.main()
