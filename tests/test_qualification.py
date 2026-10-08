"""Report safety and opt-in behavior, fake vendor only."""
import copy
import importlib.util
import json
from pathlib import Path
import sys
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
        with patch.object(rt,'doctor') as doctor,self.assertRaises(ValueError):module.qualify({},'model')
        doctor.assert_not_called()
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
            if deadline is not None or timeout<1:raise ValueError('timed out; no result accepted')
            return {'ok':True,'payload':{'result':{'ready':True},'tool_uses':[]}}
        with patch.object(rt,'doctor',return_value={'version':'fake-cli'}),patch.object(rt,'turn',side_effect=turn),patch.object(rt,'command',side_effect=ValueError('timed out; no result accepted')),patch.object(rt,'vendor_executable',return_value='fake-cli'):report=module.qualify(binding,binding['model']['id'],True)
        self.assertTrue(report['passed']);self.assertEqual(len(report['checks']),5)
