"""Report safety and opt-in behavior, fake vendor only."""
import copy
import hashlib
import os
import subprocess
import signal
import time
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import io
import contextlib
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
import turn_runtime as rt
spec=importlib.util.spec_from_file_location('qualification',ROOT/'scripts/qualify_provider.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)

class QualificationTests(unittest.TestCase):
    def binding(self):
        value=rt.candidate(json.loads((ROOT/'examples/bind-request.json').read_text()),lambda:{'version':'fake-cli'})['binding']
        value['state']='admitted';value['admission']={'resolver_id':'fake-host','checks':[{'check':'fixture','passed':True,'detail':'synthetic'}]}
        if value['model'] is None:value['model']={'id':'test-model','revision':None,'digest':None}
        return value
    def write_record(self, path):
        value={'binding':self.binding(),'package_sha256':module.package_digest(ROOT)}
        value['sha256']=hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
        path.write_text(json.dumps(value))

    def test_opt_in_precedes_vendor_calls(self):
        binding=self.binding()
        with patch.object(rt,'doctor') as doctor,patch.object(rt,'turn') as turn,patch.object(rt,'command') as command,self.assertRaisesRegex(ValueError, '--run-live'):
            module.qualify(binding,binding['model']['id'])
        doctor.assert_not_called();turn.assert_not_called();command.assert_not_called()
    @unittest.skipIf(rt.ENGINE['engine']=='openai-compatible','Subscription qualification only')
    def test_stale_version_stops_without_inference(self):
        binding=self.binding()
        with patch.object(rt,'doctor',return_value={'version':'new-cli'}),patch.object(rt,'turn') as turn:
            report=module.qualify(binding,binding['model']['id'],True)
        self.assertFalse(report['passed']);turn.assert_not_called()
    @unittest.skipIf(rt.ENGINE['engine']=='openai-compatible','Subscription qualification only')
    def test_results_export_no_vendor_output_or_exception(self):
        binding=self.binding()
        with patch.object(rt,'doctor',return_value={'version':'fake-cli'}),patch.object(rt,'turn',side_effect=ValueError('PRIVATE_TOKEN and account@example.com')),patch.object(rt,'command',side_effect=ValueError('timed out; no result accepted')):
            report=module.qualify(binding,binding['model']['id'],True)
        text=json.dumps(report);self.assertNotIn('PRIVATE_TOKEN',text);self.assertNotIn('account@example.com',text);self.assertFalse(report['passed'])
    @unittest.skipIf(rt.ENGINE['engine']=='openai-compatible','Subscription qualification only')
    def test_clean_output_timeout_and_refusal_have_explicit_results(self):
        binding=self.binding()
        def turn(request,binding,timeout=None,deadline=None):
            if deadline is not None:raise ValueError('timed out; no result accepted')
            return {'ok':True,'payload':{'result':{'ready':True},'tool_uses':[]}}
        with patch.object(rt,'doctor',return_value={'version':'fake-cli'}),patch.object(rt,'turn',side_effect=turn) as turns,patch.object(rt,'command',side_effect=ValueError('timed out; no result accepted')) as commands,patch.object(rt,'vendor_executable',return_value='fake-cli'):report=module.qualify(binding,binding['model']['id'],True)
        commands.assert_called_once_with([sys.executable, '-c', 'import time;time.sleep(30)'], timeout=.05)
        self.assertEqual(len(turns.call_args_list),3)
        self.assertLess(turns.call_args_list[-1].kwargs['deadline'],time.monotonic())
        self.assertTrue(report['passed']);self.assertEqual(len(report['checks']),5)

    def test_real_supervisor_terminates_sleeping_process(self):
        import time
        started = time.monotonic()
        with self.assertRaisesRegex(ValueError, 'timed out'):
            rt.command([sys.executable, '-c', 'import time;time.sleep(30)'], timeout=.05)
        self.assertLess(time.monotonic()-started, 3)

    def test_main_refuses_opt_in_collision_and_missing_parent_before_checks(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); record=root/'binding.json'
            self.write_record(record)
            output=root/'report.json'; output.write_text('existing')
            cases=[([],root/'new.json'),(['--run-live'],output),(['--run-live'],root/'missing/report.json')]
            for flags,target in cases:
                with patch.object(module,'qualify') as qualify,contextlib.redirect_stdout(io.StringIO()) as stdout:
                    self.assertEqual(module.main(['--binding',str(record),'--model','test','--output',str(target),*flags]),1)
                qualify.assert_not_called();self.assertIn('preflight failed',stdout.getvalue())
            self.assertEqual(output.read_text(),'existing')

    def test_main_exclusively_writes_sanitized_report_and_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); record=root/'binding.json'
            self.write_record(record)
            output=root/'report.json'
            argv=['--binding',str(record),'--model','test','--output',str(output),'--run-live']
            with patch.object(module,'qualify',return_value={'passed':True}),contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(module.main(argv),0)
            self.assertEqual(json.loads(output.read_text()),{'passed':True})
            output.unlink()
            with patch.object(module,'qualify',side_effect=ValueError('PRIVATE_TOKEN')),contextlib.redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(module.main(argv),1)
            self.assertNotIn('PRIVATE_TOKEN',stdout.getvalue());self.assertFalse(output.exists())

    def test_stale_missing_tampered_and_revoked_records_leave_no_report_or_checks(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); record=root/'binding.json'; output=root/'report.json'
            for kind in ('missing','wrong','tampered','revoked'):
                self.write_record(record)
                value=json.loads(record.read_text()); value.pop('sha256')
                if kind=='missing': value.pop('package_sha256')
                if kind=='wrong': value['package_sha256']='wrong'
                value['sha256']=hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
                if kind=='tampered': value['binding']['revision']=99
                record.write_text(json.dumps(value))
                if kind=='revoked': record.with_suffix('.revoked').touch()
                with patch.object(module,'qualify') as checks,contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(module.main(['--binding',str(record),'--model','test','--output',str(output),'--run-live']),1)
                checks.assert_not_called(); self.assertFalse(output.exists())

    def test_interrupt_removes_claimed_output(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); record=root/'binding.json'; output=root/'report.json'; self.write_record(record)
            with patch.object(module,'qualify',side_effect=KeyboardInterrupt),self.assertRaises(KeyboardInterrupt):
                module.main(['--binding',str(record),'--model','test','--output',str(output),'--run-live'])
            self.assertFalse(output.exists())

    def test_package_digest_matches_workbench_behavior_file_set(self):
        spec=importlib.util.spec_from_file_location('public_workbench',ROOT.parent/'cog-workbench/src/workbench_suite.py')
        wb=importlib.util.module_from_spec(spec)
        sys.path.insert(0,str(ROOT.parent/'cog-workbench/src'))
        try: spec.loader.exec_module(wb)
        finally: sys.path.pop(0)
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            for name in ('cog.yaml','pixi.toml','engine.json','model-artifact.json','src/run.py','scripts/check.py','context/prompt.md','binding/contract.json','contracts/output.json','README.md','src/__pycache__/cached.pyc'):
                path=root/name;path.parent.mkdir(exist_ok=True,parents=True);path.write_text(name)
            self.assertEqual(module.package_digest(root),wb.package_digest(root))
            before=module.package_digest(root);(root/'README.md').write_text('prose changed')
            self.assertEqual(module.package_digest(root),before)
            (root/'contracts/output.json').write_text('behavior changed')
            self.assertNotEqual(module.package_digest(root),before)

    def test_supervisor_terminates_grandchild_and_handles_permission_race(self):
        with tempfile.TemporaryDirectory() as folder:
            pidfile=Path(folder)/'pid'
            code="import subprocess,sys,time;from pathlib import Path;p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);Path(sys.argv[1]).write_text(str(p.pid));time.sleep(30)"
            with self.assertRaisesRegex(ValueError,'timed out'):
                rt.command([sys.executable,'-c',code,str(pidfile)],timeout=.5)
            pid=int(pidfile.read_text())
            status=subprocess.run(['ps','-o','stat=','-p',str(pid)],capture_output=True,text=True).stdout.strip()
            try: self.assertTrue(not status or status.startswith('Z'),status)
            finally:
                if status and not status.startswith('Z'): os.kill(pid,9)
        with patch.object(rt.os,'killpg',side_effect=PermissionError),self.assertRaisesRegex(ValueError,'timed out'):
            rt.command([sys.executable,'-c','import time;time.sleep(30)'],timeout=.05)

    @unittest.skipIf(rt.ENGINE['engine']=='openai-compatible','Subscription qualification only')
    def test_timeout_checks_fail_if_command_or_expired_turn_returns(self):
        binding=self.binding()
        def success(*args,**kwargs):return {'ok':True,'payload':{'result':{'ready':True},'tool_uses':[]}}
        for command_returns in (True,False):
            def turn(*args,**kwargs):
                if command_returns and 'deadline' in kwargs: raise ValueError('timed out')
                return success()
            command={'return_value':{}} if command_returns else {'side_effect':ValueError('timed out')}
            with patch.object(rt,'doctor',return_value={'version':'fake-cli'}),patch.object(rt,'turn',side_effect=turn),patch.object(rt,'command',**command):
                report=module.qualify(binding,binding['model']['id'],True)
            row=next(row for row in report['checks'] if row['check']=='local-process-timeout-and-expired-turn-deadline')
            self.assertFalse(row['passed']);self.assertFalse(report['passed'])

    @unittest.skipIf(rt.ENGINE['engine']=='openai-compatible','Subscription qualification only')
    def test_wrong_model_is_preflight_and_failure_after_first_check_is_started(self):
        with tempfile.TemporaryDirectory() as folder:
            record=Path(folder)/'binding.json';output=Path(folder)/'report.json';self.write_record(record)
            argv=['--binding',str(record),'--model','wrong-model','--output',str(output),'--run-live']
            with patch.object(rt,'doctor') as doctor,patch.object(rt,'command') as command,patch.object(rt,'turn') as turn,contextlib.redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(module.main(argv),1)
            self.assertIn('preflight failed',stdout.getvalue());doctor.assert_not_called();command.assert_not_called();turn.assert_not_called()
            self.assertFalse(output.exists())
            argv[argv.index('--model')+1]=self.binding()['model']['id']
            with patch.object(rt,'doctor',return_value={'version':'stale'}) as doctor,patch.object(rt,'turn') as turn,patch.object(module.json,'dump',side_effect=ValueError('Synthetic report write failure')),contextlib.redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(module.main(argv),1)
            doctor.assert_called_once();turn.assert_not_called()
            self.assertIn('after starting checks',stdout.getvalue());self.assertFalse(output.exists())

    def test_sigint_and_sigterm_remove_report_and_stop_turn_process_group(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);record=root/'binding.json';self.write_record(record)
            # Use the real command supervisor inside an independently started qualifier.
            driver=root/'driver.py'
            driver.write_text("import importlib.util,sys\nfrom pathlib import Path\nspec=importlib.util.spec_from_file_location('qualifier',sys.argv[1]);q=importlib.util.module_from_spec(spec);spec.loader.exec_module(q)\ndef slow(*args,**kwargs):\n    q.rt.command([sys.executable,'-c',sys.argv[4],sys.argv[3]],timeout=30)\nq.qualify=slow\nq.main(['--binding',sys.argv[2],'--model','test','--output',sys.argv[3]+'.report','--run-live'])\n")
            child="import os,subprocess,sys,time;from pathlib import Path;p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);Path(sys.argv[1]).write_text(str(os.getpid())+' '+str(p.pid));time.sleep(30)"
            for sig in (signal.SIGINT,signal.SIGTERM):
                marker=root/('pids-'+str(sig));report=Path(str(marker)+'.report')
                proc=subprocess.Popen([sys.executable,str(driver),str(ROOT/'scripts/qualify_provider.py'),str(record),str(marker),child],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                pids=[]
                try:
                    deadline=time.monotonic()+10
                    while not marker.exists() and proc.poll() is None and time.monotonic()<deadline:time.sleep(.02)
                    self.assertTrue(marker.exists(),'Synthetic turn did not start')
                    pids=list(map(int,marker.read_text().split()))
                    self.assertTrue(report.exists())
                    proc.send_signal(sig);proc.wait(timeout=5)
                    self.assertFalse(report.exists())
                    for pid in pids:
                        status=subprocess.run(['ps','-o','stat=','-p',str(pid)],capture_output=True,text=True).stdout.strip()
                        self.assertTrue(not status or status.startswith('Z'),status)
                finally:
                    if proc.poll() is None:proc.kill();proc.wait()
                    for pid in pids:
                        try:os.kill(pid,9)
                        except ProcessLookupError:pass
