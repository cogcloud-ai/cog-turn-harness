"""Report safety and opt-in behavior, fake vendor only."""
import copy
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
        with patch.object(rt,'doctor',return_value={'version':'fake-cli'}),patch.object(rt,'turn',side_effect=turn),patch.object(rt,'command',side_effect=ValueError('timed out; no result accepted')),patch.object(rt,'vendor_executable',return_value='fake-cli'):report=module.qualify(binding,binding['model']['id'],True)
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
            record.write_text(json.dumps({'binding':self.binding(),'package_sha256':module.package_digest(ROOT)}))
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
            record.write_text(json.dumps({'binding':self.binding(),'package_sha256':module.package_digest(ROOT)}))
            output=root/'report.json'
            argv=['--binding',str(record),'--model','test','--output',str(output),'--run-live']
            with patch.object(module,'qualify',return_value={'passed':True}),contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(module.main(argv),0)
            self.assertEqual(json.loads(output.read_text()),{'passed':True})
            output.unlink()
            with patch.object(module,'qualify',side_effect=ValueError('PRIVATE_TOKEN')),contextlib.redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(module.main(argv),1)
            self.assertNotIn('PRIVATE_TOKEN',stdout.getvalue());self.assertFalse(output.exists())
