import copy
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
import turn_runtime as rt

class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.request=json.loads((ROOT/'examples/bind-request.json').read_text())
        self.binding=rt.candidate(self.request,lambda:{'version':'test-cli 1'})['binding']
        self.binding['state']='admitted'
        self.binding['admission']={'resolver_id':'test-host','checks':[{'check':'test','passed':True,'detail':'mock admission'}]}
        self.turn={'document_kind':'harness_turn_request','contract':rt.CONTRACT,'request_id':'turn-1',
            'binding':{'binding_id':self.binding['binding_id'],'revision':1},'model_binding':self.binding['model_binding'],
            'consumer':{'id':'test/context','version':'1'},'context':[{'id':'system','content':'Return JSON.'}],
            'task':{'input':'Test input','output_schema':{'type':'object','properties':{'ready':{'type':'boolean'}},'required':['ready'],'additionalProperties':False}},'tool_grant_refs':[],'thread_ref':None}
        model=json.loads((ROOT/'tests/model-binding.json').read_text())
        self.model=model if self.binding['composition']=='harness' else None
    def test_candidate_never_admits(self):
        value=rt.candidate(self.request,lambda:{'version':'test-cli 1'})
        self.assertEqual(value['binding']['state'],'candidate');self.assertIsNone(value['binding']['admission'])
        rt.validate(value,'bind_result')
    def test_composition_boundary(self):
        self.request['requirement']['accepted_compositions']=['model']
        with self.assertRaises(ValueError):rt.candidate(self.request,lambda:{'version':'test'})
    def test_reject_claims_and_probe(self):
        for key,value in [('identity_verified',True),('revision_pinned',True),('evidence_level','probe')]:
            request=copy.deepcopy(self.request);request['requirement'][key]=value
            with self.assertRaises(ValueError):rt.candidate(request,lambda:{'version':'test'})
    def test_config_credentials_closed(self):
        for key in ('configuration','credential_refs'):
            request=copy.deepcopy(self.request);request[key]['secret']='never-accepted'
            with self.assertRaises(ValueError):rt.candidate(request,lambda:{'version':'test'})
    def test_feature_and_locality_rejection(self):
        for key,value in [('features',['contract-checks/packaged']),('allowed_localities',['local'])]:
            request=copy.deepcopy(self.request);request['requirement'][key]=value
            with self.assertRaises(ValueError):rt.candidate(request,lambda:{'version':'test'})
    def test_turn_exact_refs(self):
        rt.check_turn(self.turn,self.binding,self.model)
        self.turn['binding']['revision']=2
        with self.assertRaises(ValueError):rt.check_turn(self.turn,self.binding,self.model)
    def test_reject_candidate_turn(self):
        self.binding['state']='candidate';self.binding['admission']=None
        with self.assertRaises(ValueError):rt.check_turn(self.turn,self.binding,self.model)
    def test_no_memory_or_tools(self):
        for key,value in [('thread_ref','remembered'),('tool_grant_refs',['grant'])]:
            request=copy.deepcopy(self.turn);request[key]=value
            with self.assertRaises(ValueError):rt.check_turn(request,self.binding,self.model)
    def test_remote_schema_ref_rejected(self):
        self.turn['task']['output_schema']['$ref']='https://example.com/schema'
        with self.assertRaises(ValueError):rt.check_turn(self.turn,self.binding,self.model)
    def test_model_dependency_mismatch(self):
        self.turn['model_binding']={'binding_id':'wrong','revision':1}
        with self.assertRaises(ValueError):rt.check_turn(self.turn,self.binding,self.model)
    def test_no_api_keys_in_subscription_environment(self):
        with patch.dict(os.environ,{'OPENAI_API_KEY':'secret','ANTHROPIC_API_KEY':'secret','OPENROUTER_API_KEY':'secret','OPENROUTER_COG_TOKEN':'secret','PIXI_PROJECT_NAME':'other','ANTHROPIC_BASE_URL':'https://bad'}):
            env=rt.clean_env()
        self.assertFalse(any('KEY' in k or 'TOKEN' in k or k.startswith('PIXI_') for k in env))
        self.assertNotIn('ANTHROPIC_BASE_URL',env)
    def test_turn_roundtrip_preserves_identity(self):
        with patch.object(rt,'infer_model',return_value=({'ready':True}, {'synthetic': True})),patch.object(rt,'infer_vendor',return_value=({'ready':True}, {'synthetic': True})):
            result=rt.turn(self.turn,self.binding,self.model)
        self.assertTrue(result['ok']);rt.validate(result['payload'],'harness_turn_result')
        self.assertEqual(result['binding'],self.binding);self.assertEqual(result['payload']['model_binding'],self.binding['model_binding'])
    def test_parser_error_does_not_echo_secret(self):
        result=rt.envelope('turn',error='Vendor failed')
        self.assertFalse(result['ok']);self.assertIsNone(result['payload'])
    def test_vendored_schema_matches_profile(self):
        profile=ROOT.parent/'cogspec/schemas/satisfier-binding.schema.json'
        if profile.exists():self.assertEqual((ROOT/'contracts/satisfier-binding.schema.json').read_bytes(),profile.read_bytes())


    def test_vendor_command_controls_and_schema_fallback(self):
        for engine in ('codex','claude'):
            for strict in (True,False):
                turn=copy.deepcopy(self.turn)
                if not strict:turn['task']['output_schema']={'type':'object'}
                binding=copy.deepcopy(self.binding);binding['model']={'id':'synthetic-model','revision':None,'digest':None}
                captured=[]
                def command(argv,prompt,cwd):
                    captured.append((argv,prompt))
                    if engine=='codex':
                        Path(argv[argv.index('--output-last-message')+1]).write_text('{"ready":true}')
                        return '{"type":"turn.completed"}\n'
                    return json.dumps({'type':'result','subtype':'success','is_error':False,'result':json.dumps({'ready':True})})
                with patch.dict(rt.ENGINE,{'engine':engine}),patch.object(rt,'doctor',return_value={'version':'test-cli 1'}),patch.object(rt,'vendor_executable',return_value='/fake/vendor'),patch.object(rt,'command',side_effect=command):
                    result,observations=rt.infer_vendor(turn,binding)
                self.assertEqual(result,{'ready':True});self.assertFalse(observations['identity_verified'])
                argv,prompt=captured[0]
                self.assertEqual(('--output-schema' if engine=='codex' else '--json-schema') in argv,strict)
                self.assertNotIn('--dangerously-bypass-approvals-and-sandbox',argv)
                self.assertNotIn('--dangerously-skip-permissions',argv)
                if engine=='codex':self.assertEqual(argv[argv.index('--sandbox')+1],'read-only')
                else:self.assertEqual(argv[argv.index('--tools')+1],'')
    def test_vendor_version_change_requires_rebinding(self):
        binding=copy.deepcopy(self.binding);binding['model']={'id':'test'}
        with patch.object(rt,'doctor',return_value={'version':'new-cli'}),self.assertRaises(ValueError):rt.infer_vendor(self.turn,binding)
    def test_native_schema_support_is_conservative(self):
        self.assertTrue(rt.native_schema(self.turn['task']['output_schema']))
        self.assertFalse(rt.native_schema({'type':'object'}))

if __name__=='__main__':unittest.main()
