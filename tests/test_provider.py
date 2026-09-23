import copy
import json
import os
from pathlib import Path
import sys
import time
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
    def test_timeout_rejects_partial_output(self):
        with self.assertRaisesRegex(ValueError, 'timed out; no result accepted'):
            rt.command([sys.executable, '-c', 'import time; print("partial", flush=True); time.sleep(10)'], timeout=0.05)
    def test_result_text_tolerates_fences_and_prose(self):
        self.assertEqual(rt.parse_result_text('{"ready": true}'),{'ready':True})
        self.assertEqual(rt.parse_result_text('```json\n{"ready": true}\n```'),{'ready':True})
        self.assertEqual(rt.parse_result_text('Here is the design:\n{"ready": true}\nLet me know.'),{'ready':True})
        with self.assertRaisesRegex(ValueError,'not a JSON object; it begins: I could not'):
            rt.parse_result_text('I could not produce a design because the goal is unclear.')
        with self.assertRaisesRegex(ValueError,'empty result'):
            rt.parse_result_text('')
    def test_empty_vendor_output_reports_stderr(self):
        with self.assertRaisesRegex(ValueError,'returned no output; stderr: boom'):
            rt.command([sys.executable,'-c','import sys; sys.stderr.write("boom")'])
    def test_candidate_never_admits(self):
        value=rt.candidate(self.request,lambda:{'version':'test-cli 1'})
        self.assertEqual(value['binding']['state'],'candidate');self.assertIsNone(value['binding']['admission'])
        rt.validate(value,'bind_result')
    def test_readiness_can_accept_stderr_only_status(self):
        value=rt.command([sys.executable,'-c','import sys; sys.stderr.write("Logged in using ChatGPT")'],include_stderr=True)
        self.assertIn('ChatGPT',value)
    def test_combined_readiness_output_is_bounded(self):
        with patch.object(rt,'MAX_BYTES',8), self.assertRaisesRegex(ValueError,'exceeded'):
            rt.command([sys.executable,'-c','import sys; sys.stdout.write("1234"); sys.stderr.write("56789")'],include_stderr=True)
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
        profile=ROOT.parent/'cog-manifest-openteams/schemas/satisfier-binding.schema.json'
        if profile.exists():self.assertEqual((ROOT/'contracts/satisfier-binding.schema.json').read_bytes(),profile.read_bytes())


    def test_vendor_command_controls_and_schema_fallback(self):
        for engine in ('codex','claude'):
            for strict in (True,False):
                turn=copy.deepcopy(self.turn)
                if not strict:turn['task']['output_schema']={'type':'object'}
                binding=copy.deepcopy(self.binding);binding['model']={'id':'synthetic-model','revision':None,'digest':None}
                captured=[]
                def command(argv,prompt,cwd,timeout=None):
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
    def test_vendor_schema_drops_meta_keys(self):
        # Authored Cogs carry '$schema'; the Claude CLI rejects it in --json-schema.
        for engine in ('codex','claude'):
            turn=copy.deepcopy(self.turn)
            turn['task']['output_schema']={'$schema':'https://json-schema.org/draft/2020-12/schema','$id':'x',**turn['task']['output_schema']}
            binding=copy.deepcopy(self.binding);binding['model']={'id':'synthetic-model','revision':None,'digest':None}
            captured=[]
            def command(argv,prompt,cwd,timeout=None):
                if engine=='codex':
                    captured.append(json.loads(Path(argv[argv.index('--output-schema')+1]).read_text()))
                    Path(argv[argv.index('--output-last-message')+1]).write_text('{"ready":true}')
                    return '{"type":"turn.completed"}\n'
                captured.append(json.loads(argv[argv.index('--json-schema')+1]))
                return json.dumps({'type':'result','subtype':'success','is_error':False,'result':json.dumps({'ready':True})})
            with patch.dict(rt.ENGINE,{'engine':engine}),patch.object(rt,'doctor',return_value={'version':'test-cli 1'}),patch.object(rt,'vendor_executable',return_value='/fake/vendor'),patch.object(rt,'command',side_effect=command):
                result,_=rt.infer_vendor(turn,binding)
            self.assertEqual(result,{'ready':True})
            handed=captured[0]
            self.assertNotIn('$schema',handed);self.assertNotIn('$id',handed);self.assertEqual(handed['type'],'object')
    def test_vendor_version_change_requires_rebinding(self):
        binding=copy.deepcopy(self.binding);binding['model']={'id':'test'}
        with patch.object(rt,'doctor',return_value={'version':'new-cli'}),self.assertRaises(ValueError):rt.infer_vendor(self.turn,binding)
    def test_native_schema_support_is_conservative(self):
        self.assertTrue(rt.native_schema(self.turn['task']['output_schema']))
        self.assertFalse(rt.native_schema({'type':'object'}))
    def test_readiness_commands_draw_on_the_turn_budget(self):
        """Three readiness commands at a flat fifteen seconds each used to be
        spent BEFORE the turn's own deadline began. They are inside it now."""
        for engine in ('codex','claude'):
            for budget,expected in ((100,rt.READY_SECONDS),(5,5)):
                seen=[]
                def command(argv,prompt=None,cwd=None,timeout=None,include_stderr=False):
                    seen.append(timeout)
                    if '--help' in argv:
                        return ('--ignore-user-config --ephemeral --output-schema' if engine=='codex'
                                else '--safe-mode --tools --no-session-persistence --json-schema')
                    if 'status' in argv:
                        return 'ChatGPT' if engine=='codex' else json.dumps({'loggedIn':True,'authMethod':'claude.ai'})
                    return 'test-cli 1'
                with patch.dict(rt.ENGINE,{'engine':engine}),\
                     patch.object(rt,'vendor_executable',return_value='/fake/vendor'),\
                     patch.object(rt,'command',side_effect=command):
                    rt.doctor(time.monotonic()+budget)
                self.assertEqual(len(seen),3)
                for value in seen:
                    self.assertLessEqual(value,expected)
                    self.assertGreater(value,expected-1)
    def test_an_exhausted_budget_starts_nothing(self):
        with patch.dict(rt.ENGINE,{'engine':'claude'}),\
             patch.object(rt,'vendor_executable',return_value='/fake/vendor'),\
             patch.object(rt,'command',side_effect=AssertionError('no command may start')):
            with self.assertRaisesRegex(ValueError,'budget was spent before'):
                rt.doctor(time.monotonic()-1)
    def test_the_vendor_command_gets_what_readiness_left(self):
        binding=copy.deepcopy(self.binding);binding['model']={'id':'synthetic-model','revision':None,'digest':None}
        seen={}
        def command(argv,prompt,cwd,timeout=None):
            seen['timeout']=timeout
            return json.dumps({'type':'result','subtype':'success','is_error':False,
                               'result':json.dumps({'ready':True})})
        def slow_doctor(deadline=None):
            time.sleep(0.2)
            return {'version':'test-cli 1'}
        with patch.dict(rt.ENGINE,{'engine':'claude'}),patch.object(rt,'doctor',side_effect=slow_doctor),\
             patch.object(rt,'vendor_executable',return_value='/fake/vendor'),\
             patch.object(rt,'command',side_effect=command):
            rt.infer_vendor(self.turn,binding,timeout=5)
        self.assertLess(seen['timeout'],4.9,'readiness time must come out of the same budget')
        self.assertGreater(seen['timeout'],4.0)
    def test_one_deadline_travels_through_revalidation_and_readiness(self):
        """§12: the gateway hands `turn()` an absolute instant, and every stage
        after it charges the SAME clock. `infer_vendor` used to be handed a
        duration measured before the disconnect probe and to start a fresh
        deadline from it AFTER `turn()` had revalidated, so probing and
        checking were free. An injected clock makes the arithmetic exact and
        makes this test sleep for nothing."""
        class Clock:
            def __init__(self): self.now=1000.0
            def monotonic(self): return self.now
            def spend(self,seconds): self.now+=seconds
        clock=Clock()
        binding=copy.deepcopy(self.binding);binding['model']={'id':'synthetic-model','revision':None,'digest':None}
        seen={}
        def check_turn(request,bound,model_binding=None): clock.spend(3)   # revalidation
        def doctor(deadline=None): clock.spend(1);return {'version':'test-cli 1'}   # readiness
        def command(argv,prompt,cwd,timeout=None):
            seen['timeout']=timeout
            return json.dumps({'type':'result','subtype':'success','is_error':False,
                               'result':json.dumps({'ready':True})})
        with patch.dict(rt.ENGINE,{'engine':'claude'}),patch.object(rt,'time',clock),\
             patch.object(rt,'check_turn',side_effect=check_turn),\
             patch.object(rt,'doctor',side_effect=doctor),\
             patch.object(rt,'vendor_executable',return_value='/fake/vendor'),\
             patch.object(rt,'command',side_effect=command):
            # A caller that has already started its clock hands the instant on.
            rt.turn(self.turn,binding,None,deadline=clock.monotonic()+10)
        self.assertEqual(seen['timeout'],6.0,
                         'the vendor command must get the budget MINUS the 3 s '
                         'revalidation and the 1 s readiness, not a fresh clock')
    def test_a_reference_is_checked_with_the_resolver_that_will_resolve_it(self):
        # An empty pointer component names an empty-string key: the real
        # resolver says no, and so must the precheck.
        self.turn['task']['output_schema']={'type':'object','$defs':{'T':{'type':'object'}},
                                            '$ref':'#/$defs//T'}
        with self.assertRaisesRegex(ValueError,'does not resolve'):
            rt.check_turn(self.turn,self.binding,self.model)
        self.turn['task']['output_schema']={'type':'object','$defs':{'':{'T':{'type':'object'}}},
                                            '$ref':'#/$defs//T'}
        rt.references_resolve(self.turn['task']['output_schema'])

if __name__=='__main__':unittest.main()
