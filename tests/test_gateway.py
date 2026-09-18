"""Turn-gateway tests. No vendor CLI: `turn` is replaced everywhere."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
import turn_runtime as rt
import turn_gateway as gw

SCHEMA={'type':'object','properties':{'answer':{'type':'string'}},'required':['answer'],'additionalProperties':False}


def admitted():
    request=json.loads((ROOT/'examples/bind-request.json').read_text())
    binding=rt.candidate(request,lambda:{'version':'test-cli 1'})['binding']
    binding['state']='admitted'
    binding['admission']={'resolver_id':'test-host','checks':[{'check':'test','passed':True,'detail':'mock admission'}]}
    return binding


def fake_turn(result=None,observations=None):
    def turn(request,binding,model_binding=None):
        rt.check_turn(request,binding,model_binding)
        payload={'document_kind':'harness_turn_result','contract':rt.CONTRACT,'request_id':request['request_id'],
                 'binding':request['binding'],'model_binding':request['model_binding'],
                 'result':result if result is not None else {'answer':'ok'},'tool_uses':[]}
        response=rt.envelope('turn',payload,binding=binding)
        response['provider_observations']=observations if observations is not None else {'harness_version':'test-cli 1','identity_verified':False}
        return response
    return turn


def call(base,path,body=None,token=None,method=None,timeout=30):
    data=json.dumps(body).encode() if body is not None else None
    request=urllib.request.Request(base+path,data=data,method=method,
                                   headers={'Content-Type':'application/json'} if data else {})
    if token: request.add_header('Authorization','Bearer '+token)
    try:
        with urllib.request.urlopen(request,timeout=timeout) as response:
            return response.status,json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code,json.loads(exc.read().decode())


class StartTests(unittest.TestCase):
    def setUp(self):
        self.binding=admitted()
        self.dir=tempfile.TemporaryDirectory();self.addCleanup(self.dir.cleanup)
    def write(self,value,name='binding.json'):
        path=Path(self.dir.name)/name;path.write_text(json.dumps(value));return str(path)
    def dependency(self):
        """Harness-only providers name a separate admitted model binding."""
        if self.binding['model_binding'] is None: return None
        return self.write(json.loads((ROOT/'tests/model-binding.json').read_text()),'model.json')
    def test_bare_admitted_document_is_accepted(self):
        binding,model=gw.prepare(self.write(self.binding),self.dependency())
        self.assertEqual(binding['binding_id'],self.binding['binding_id'])
        self.assertEqual(model is None,self.binding['model_binding'] is None)
    def test_workbench_suite_entry_is_accepted(self):
        entry={'request':{},'binding':self.binding,'path':str(ROOT),'package_sha256':'x','sha256':'y'}
        binding,_=gw.prepare(self.write(entry,'abc-4.json'),self.dependency())
        self.assertEqual(binding,self.binding)
    def test_candidate_binding_refused(self):
        candidate=copy.deepcopy(self.binding);candidate['state']='candidate';candidate['admission']=None
        with self.assertRaisesRegex(ValueError,'ADMITTED binding only'):gw.prepare(self.write(candidate))
    def test_revoked_entry_refused(self):
        path=self.write(self.binding,'abc-4.json');Path(path).with_suffix('.revoked').touch()
        with self.assertRaisesRegex(ValueError,'revoked'):gw.prepare(path)
    def test_foreign_provider_refused(self):
        other=copy.deepcopy(self.binding);other['provider']={'id':'openteams/cog-somewhere','version':'0.1.0'}
        with self.assertRaisesRegex(ValueError,'belongs to provider'):gw.prepare(self.write(other))
    def test_not_a_binding_document_refused(self):
        with self.assertRaisesRegex(ValueError,'not a binding document'):gw.prepare(self.write({'hello':'world'}))
        bad=Path(self.dir.name)/'bad.json';bad.write_text('not json')
        with self.assertRaisesRegex(ValueError,'not JSON'):gw.prepare(str(bad))
        with self.assertRaisesRegex(ValueError,'no such file'):gw.prepare(str(Path(self.dir.name)/'missing.json'))
    def test_model_binding_arity(self):
        if self.binding['model_binding'] is None:
            with self.assertRaisesRegex(ValueError,'references no separate model'):
                gw.prepare(self.write(self.binding),self.write(self.binding,'m.json'))
        else:
            with self.assertRaisesRegex(ValueError,'--model-binding is required'):gw.prepare(self.write(self.binding))
    def test_non_loopback_host_refused_by_name(self):
        with self.assertRaisesRegex(ValueError,r'binds 127\.0\.0\.1 only'):
            gw.make_server(self.binding,None,0,'0.0.0.0')
    def test_model_id_shape(self):
        self.assertEqual(gw.served_model(self.binding),
                         f'{gw.SHORT_NAME}/{self.binding["binding_id"]}@{self.binding["revision"]}')
        self.assertFalse(gw.SHORT_NAME.startswith('cog-'))


class ServerTestCase(unittest.TestCase):
    """A live loopback server on port 0, in a thread, with `turn` replaced."""
    token=None
    def setUp(self):
        self.binding=admitted()
        model=json.loads((ROOT/'tests/model-binding.json').read_text())
        self.model=model if self.binding['composition']=='harness' else None
        environment={'COG_TURN_GATEWAY_TOKEN':self.token} if self.token else {}
        with patch.dict(os.environ,environment,clear=False):
            if not self.token: os.environ.pop('COG_TURN_GATEWAY_TOKEN',None)
            self.server=gw.make_server(self.binding,self.model,0)
        self.base=f'http://127.0.0.1:{self.server.server_port}'
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.stop)
        self.model_id=gw.served_model(self.binding)
    def stop(self):
        self.server.shutdown();self.thread.join(5);self.server.server_close()
    def completion_body(self,**overrides):
        body={'model':self.model_id,'temperature':0,
              'messages':[{'role':'system','content':'Answer as JSON.'},
                          {'role':'user','content':'Reply with answer = ok'}],
              'response_format':{'type':'json_schema','json_schema':{'name':'cog_output','strict':True,'schema':SCHEMA}}}
        body.update(overrides);return body


class SurfaceTests(ServerTestCase):
    def test_health(self):
        status,body=call(self.base,'/health')
        self.assertEqual(status,200);self.assertEqual(body['model'],self.model_id)
    def test_models(self):
        status,body=call(self.base,'/v1/models')
        self.assertEqual(status,200);self.assertEqual([x['id'] for x in body['data']],[self.model_id])
    def test_unknown_endpoint(self):
        self.assertEqual(call(self.base,'/v1/embeddings')[0],404)
        self.assertEqual(call(self.base,'/v1/embeddings',{})[0],404)
    def test_cog_core_health_probes(self):
        # cog_core.health() probes <base without /v1>/health, then <base>/models.
        endpoint=self.base+'/v1'
        probes=[endpoint.rstrip('/').rsplit('/v1',1)[0]+'/health',endpoint.rstrip('/')+'/models']
        for probe in probes:
            with urllib.request.urlopen(urllib.request.Request(probe),timeout=5) as response:
                self.assertEqual(response.status,200)
    def test_exact_cog_core_invoke_body(self):
        # The literal body cog_core.invoke builds for a json_schema binding.
        body={'model':self.model_id,'temperature':0,
              'messages':[{'role':'system','content':'You classify issues.'},
                          {'role':'user','content':'ISSUE: the build is broken'}],
              'response_format':{'type':'json_schema','json_schema':{'name':'cog_output','strict':True,'schema':SCHEMA}}}
        with patch.object(rt,'turn',side_effect=fake_turn()):
            status,reply=call(self.base,'/v1/chat/completions',body)
        self.assertEqual(status,200)
        self.assertEqual(reply['model'],self.model_id)
        self.assertEqual(json.loads(reply['choices'][0]['message']['content']),{'answer':'ok'})
        self.assertEqual(reply['object'],'chat.completion')
        x=reply['x_cog']
        self.assertEqual(x['provider'],rt.IDENTITY)
        self.assertEqual(x['binding'],{'binding_id':self.binding['binding_id'],'revision':self.binding['revision']})
        self.assertFalse(x['model_identity_verified']);self.assertEqual(x['evidence_scope'],'composed-system')
        self.assertTrue(x['request_id']);self.assertIsNotNone(x['provider_observations'])
    def test_system_and_user_become_the_task_input(self):
        seen={}
        def turn(request,binding,model_binding=None):
            seen.update(request);return fake_turn()(request,binding,model_binding)
        with patch.object(rt,'turn',side_effect=turn):
            call(self.base,'/v1/chat/completions',self.completion_body())
        self.assertIn('Answer as JSON.',seen['task']['input'])
        self.assertIn('Reply with answer = ok',seen['task']['input'])
        self.assertLess(seen['task']['input'].index('Answer as JSON.'),
                        seen['task']['input'].index('Reply with answer = ok'))
        self.assertEqual(seen['task']['output_schema'],SCHEMA)
        self.assertEqual(seen['tool_grant_refs'],[]);self.assertIsNone(seen['thread_ref'])
    def test_temperature_and_max_tokens_are_ignored(self):
        with patch.object(rt,'turn',side_effect=fake_turn()):
            status,_=call(self.base,'/v1/chat/completions',self.completion_body(temperature=0.7,max_tokens=32))
        self.assertEqual(status,200)


class RefusalTests(ServerTestCase):
    def refused(self,**overrides):
        with patch.object(rt,'turn',side_effect=fake_turn()):
            status,body=call(self.base,'/v1/chat/completions',self.completion_body(**overrides))
        self.assertEqual(status,400,body)
        self.assertIn('A turn is one non-streaming request',body['error']['message'])
        return body
    def test_json_object_response_format(self):
        self.refused(response_format={'type':'json_object'})
    def test_missing_response_format(self):
        body=self.completion_body();body.pop('response_format')
        with patch.object(rt,'turn',side_effect=fake_turn()):
            self.assertEqual(call(self.base,'/v1/chat/completions',body)[0],400)
    def test_stream_tools_and_n(self):
        self.refused(stream=True);self.refused(n=2)
        self.refused(tools=[{'type':'function','function':{'name':'x'}}])
    def test_assistant_history(self):
        self.refused(messages=[{'role':'user','content':'hi'},{'role':'assistant','content':'hello'}])
    def test_two_user_messages(self):
        self.refused(messages=[{'role':'user','content':'a'},{'role':'user','content':'b'}])
    def test_unknown_field(self):
        body=self.refused(seed=7)
        self.assertIn('seed',body['error']['message'])
    def test_another_model_id(self):
        with patch.object(rt,'turn',side_effect=fake_turn()):
            status,body=call(self.base,'/v1/chat/completions',self.completion_body(model='gpt-4o'))
        self.assertEqual(status,400);self.assertEqual(body['error']['code'],'model_not_found')
    def test_non_object_output_schema(self):
        self.refused(response_format={'type':'json_schema','json_schema':{'name':'x','strict':True,
                     'schema':{'type':'array','items':{'type':'string'}}}})
    def test_remote_schema_reference(self):
        self.refused(response_format={'type':'json_schema','json_schema':{'name':'x','strict':True,
                     'schema':{'type':'object','$ref':'https://example.com/s.json'}}})
    def test_failed_turn_is_502_never_a_completion(self):
        def turn(request,binding,model_binding=None):
            raise ValueError('Vendor harness version changed; rebind before invoking.')
        with patch.object(rt,'turn',side_effect=turn):
            status,body=call(self.base,'/v1/chat/completions',self.completion_body())
        self.assertEqual(status,502)
        self.assertEqual(body['error']['code'],'turn-provider')
        self.assertIn('rebind before invoking',body['error']['message'])
        self.assertNotIn('choices',body)
    def test_failed_envelope_is_502(self):
        with patch.object(rt,'turn',side_effect=lambda *a,**k:rt.envelope('turn',error='Vendor CLI failed.')):
            status,body=call(self.base,'/v1/chat/completions',self.completion_body())
        self.assertEqual(status,502);self.assertIn('Vendor CLI failed.',body['error']['message'])


class SerializationTests(ServerTestCase):
    def test_one_turn_at_a_time(self):
        live,overlap=[],[]
        lock=threading.Lock()
        def turn(request,binding,model_binding=None):
            with lock:
                live.append(1)
                if len(live)>1: overlap.append(1)
            time.sleep(0.3)
            with lock: live.pop()
            return fake_turn()(request,binding,model_binding)
        results=[]
        with patch.object(rt,'turn',side_effect=turn):
            threads=[threading.Thread(target=lambda:results.append(
                call(self.base,'/v1/chat/completions',self.completion_body()))) for _ in range(3)]
            for t in threads: t.start()
            for t in threads: t.join(30)
        self.assertEqual([status for status,_ in results],[200,200,200])
        self.assertFalse(overlap,'turns overlapped; the gateway must run one at a time')


class TokenTests(ServerTestCase):
    token='shared-secret'
    def test_token_required_on_every_endpoint(self):
        for path in ('/health','/v1/models'):
            self.assertEqual(call(self.base,path)[0],401)
        with patch.object(rt,'turn',side_effect=fake_turn()):
            self.assertEqual(call(self.base,'/v1/chat/completions',self.completion_body())[0],401)
            self.assertEqual(call(self.base,'/v1/chat/completions',self.completion_body(),token='wrong')[0],401)
    def test_matching_token_admitted(self):
        self.assertEqual(call(self.base,'/health',token=self.token)[0],200)
        self.assertEqual(call(self.base,'/v1/models',token=self.token)[0],200)
        with patch.object(rt,'turn',side_effect=fake_turn()):
            self.assertEqual(call(self.base,'/v1/chat/completions',self.completion_body(),token=self.token)[0],200)


class CopyTests(unittest.TestCase):
    SIBLINGS=('cog-claude','cog-chatgpt','cog-turn-harness')
    def test_gateway_is_byte_identical_across_providers(self):
        mine=ROOT/'src/turn_gateway.py'
        others=[ROOT.parent/name/'src/turn_gateway.py' for name in self.SIBLINGS]
        others=[p for p in others if p.exists() and p.resolve()!=mine.resolve()]
        if not others:
            self.skipTest('no sibling provider packages beside this one')
        for other in others:
            self.assertEqual(mine.read_bytes(),other.read_bytes(),
                             f'{other} differs; turn_gateway.py is copied byte-for-byte')


if __name__=='__main__':unittest.main()
