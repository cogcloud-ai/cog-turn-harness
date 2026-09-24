"""Turn-gateway tests. No vendor CLI: `turn` is replaced everywhere.

Three layers: what the gateway refuses to start on, the transport boundary
(token, Host, Origin, media type, deadlines, connection limit, disconnects),
and the REAL caller — `cog_core.invoke`/`health` from the context-cog package
beside this one, driven against a live loopback server with the vendor replaced.
"""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import socket
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
TOKEN='test-token-0123456789abcdef0123456789abcdef'
# Optional real-caller check against a legacy context Cog (an internal package,
# not part of the suite); the tests skip when it is not checked out beside this one.
CALLER=ROOT.parent/'cog-issue-classifier'


def admitted():
    request=json.loads((ROOT/'examples/bind-request.json').read_text())
    binding=rt.candidate(request,lambda:{'version':'test-cli 1'})['binding']
    binding['state']='admitted'
    binding['admission']={'resolver_id':'test-host','checks':[{'check':'test','passed':True,'detail':'mock admission'}]}
    return binding


def fake_turn(result=None,observations=None,before=None):
    def turn(request,binding,model_binding=None,timeout=None,deadline=None):
        rt.check_turn(request,binding,model_binding)
        if before is not None: before(request)
        payload={'document_kind':'harness_turn_result','contract':rt.CONTRACT,'request_id':request['request_id'],
                 'binding':request['binding'],'model_binding':request['model_binding'],
                 'result':result if result is not None else {'answer':'ok'},'tool_uses':[]}
        response=rt.envelope('turn',payload,binding=binding)
        response['provider_observations']=observations if observations is not None else {'harness_version':'test-cli 1','identity_verified':False}
        return response
    return turn


def never_called(*args,**kwargs):
    raise AssertionError('the vendor turn was reached by a request the gateway must refuse')


def call(base,path,body=None,token=TOKEN,method=None,timeout=30,headers=None,
         content_type='application/json'):
    data=json.dumps(body).encode() if body is not None else None
    request=urllib.request.Request(base+path,data=data,method=method,
                                   headers={'Content-Type':content_type} if data else {})
    if token: request.add_header('Authorization','Bearer '+token)
    for key,value in (headers or {}).items(): request.add_header(key,value)
    try:
        with urllib.request.urlopen(request,timeout=timeout) as response:
            return response.status,json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code,json.loads(exc.read().decode())


def speak(port,payload,wait=5,hold=False):
    """Raw HTTP, so the transport boundary can be tested without a client
    library tidying it up. Returns (status, bytes), or (None, socket) when the
    caller wants to keep the connection and walk away from it."""
    sock=socket.create_connection(('127.0.0.1',port),timeout=wait)
    sock.sendall(payload)
    if hold: return None,sock
    sock.settimeout(wait)
    data=b''
    try:
        while True:
            chunk=sock.recv(65536)
            if not chunk: break
            data+=chunk
    except (TimeoutError,socket.timeout,ConnectionResetError):
        pass
    sock.close()
    return (int(data.split(b' ')[1]) if data.startswith(b'HTTP/') else None),data


class StartTests(unittest.TestCase):
    def setUp(self):
        self.binding=admitted()
        self.dir=tempfile.TemporaryDirectory();self.addCleanup(self.dir.cleanup)
        self.env=patch.dict(os.environ,{gw.TOKEN_VARIABLE:TOKEN});self.env.start()
        self.addCleanup(self.env.stop)
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
    def test_incompatible_separate_model_refuses_to_start(self):
        """A model binding that could never serve a turn is a start-time refusal,
        not a 400 on every completion for the life of the process."""
        if self.binding['model_binding'] is None:
            self.skipTest('this provider is an inseparable Model+Harness binding')
        model=json.loads((ROOT/'tests/model-binding.json').read_text())
        # This provider's own binding, renumbered: a real document of the wrong
        # composition, not a schema-invalid one.
        wrong=copy.deepcopy(self.binding)
        wrong['binding_id'],wrong['revision']=model['binding_id'],model['revision']
        for broken,expected in ((wrong,'model-only'),
                                (dict(model,capability='model-endpoint/other'),'capability is'),
                                (dict(model,features=['text-generation']),'json-output')):
            with self.assertRaisesRegex(ValueError,expected):
                gw.prepare(self.write(self.binding),self.write(broken,'broken-model.json'))
    def test_non_loopback_host_refused_by_name(self):
        with self.assertRaisesRegex(ValueError,r'binds 127\.0\.0\.1 only'):
            gw.make_server(self.binding,None,0,'0.0.0.0')
    def test_missing_or_short_token_refuses_to_start(self):
        for value in (None,'','short-secret'):
            with patch.dict(os.environ,{} if value is None else {gw.TOKEN_VARIABLE:value}):
                if value is None: os.environ.pop(gw.TOKEN_VARIABLE,None)
                with self.assertRaisesRegex(ValueError,'at least 32 characters'):
                    gw.make_server(self.binding,None,0)
    def test_model_id_shape(self):
        self.assertEqual(gw.served_model(self.binding),
                         f'{gw.SHORT_NAME}/{self.binding["binding_id"]}@{self.binding["revision"]}')
        self.assertFalse(gw.SHORT_NAME.startswith('cog-'))


class ServerTestCase(unittest.TestCase):
    """A live loopback server on port 0, in a thread, with `turn` replaced.

    Every deadline is a constructor parameter, so a test that must wait one out
    waits half a second rather than ten (or a hundred and seventy)."""
    queue_wait=None
    connections=None
    turn_timeout=None
    min_inference=None
    header_seconds=None
    body_seconds=None
    write_seconds=None
    def setUp(self):
        self.binding=admitted()
        model=json.loads((ROOT/'tests/model-binding.json').read_text())
        self.model=model if self.binding['composition']=='harness' else None
        with patch.dict(os.environ,{gw.TOKEN_VARIABLE:TOKEN}):
            self.server=gw.make_server(self.binding,self.model,0,queue_wait=self.queue_wait,
                                       connections=self.connections,
                                       turn_timeout=self.turn_timeout,
                                       min_inference=self.min_inference,
                                       header_seconds=self.header_seconds,
                                       body_seconds=self.body_seconds,
                                       write_seconds=self.write_seconds)
        self.port=self.server.server_port
        self.base=f'http://127.0.0.1:{self.port}'
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
    def post(self,body=None,content_type='application/json',length=None,host=None,
             token=TOKEN,path='/v1/chat/completions',version='HTTP/1.1'):
        raw=json.dumps(body).encode() if body is not None else b''
        head=f'POST {path} {version}\r\nHost: {host or "127.0.0.1:"+str(self.port)}\r\n'
        if token: head+=f'Authorization: Bearer {token}\r\n'
        head+=f'Content-Type: {content_type}\r\n'
        head+=f'Content-Length: {len(raw) if length is None else length}\r\n\r\n'
        return head.encode()+raw


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
    def test_completion_shape(self):
        with patch.object(rt,'turn',side_effect=fake_turn()):
            status,reply=call(self.base,'/v1/chat/completions',self.completion_body())
        self.assertEqual(status,200)
        self.assertEqual(reply['model'],self.model_id)
        self.assertEqual(json.loads(reply['choices'][0]['message']['content']),{'answer':'ok'})
        self.assertEqual(reply['object'],'chat.completion')
        x=reply['x_cog']
        self.assertEqual(x['provider'],rt.IDENTITY)
        self.assertEqual(x['binding'],{'binding_id':self.binding['binding_id'],'revision':self.binding['revision']})
        self.assertFalse(x['model_identity_verified']);self.assertEqual(x['evidence_scope'],'composed-system')
        self.assertTrue(x['request_id']);self.assertIsNotNone(x['provider_observations'])
    def test_system_becomes_context_and_the_user_message_is_the_task_input(self):
        seen={}
        def turn(request,binding,model_binding=None,timeout=None,deadline=None):
            seen.update(request);return fake_turn()(request,binding,model_binding)
        with patch.object(rt,'turn',side_effect=turn):
            call(self.base,'/v1/chat/completions',self.completion_body())
        self.assertEqual(seen['context'],[{'id':gw.CONTEXT_ID,'content':'Answer as JSON.'}])
        self.assertEqual(seen['task']['input'],'Reply with answer = ok')
        self.assertEqual(seen['task']['output_schema'],SCHEMA)
        self.assertEqual(seen['tool_grant_refs'],[]);self.assertIsNone(seen['thread_ref'])
    def test_rendered_prompt_is_the_workbench_host_shape(self):
        # infer_vendor renders context, then 'TASK DATA:', then the input —
        # exactly what the workbench host sends for the same messages.
        request=gw.translate(self.completion_body(),self.binding)
        rendered='\n\n'.join(x['content'] for x in request['context'])+'\n\nTASK DATA:\n'+request['task']['input']
        self.assertEqual(rendered,'Answer as JSON.\n\nTASK DATA:\nReply with answer = ok')
    def test_the_turn_gets_what_the_budget_leaves_and_no_more(self):
        """Not the flag value: what remains of this request's own budget once
        the transport and the queue have had their share."""
        seen={}
        def turn(request,binding,model_binding=None,timeout=None,deadline=None):
            # An absolute instant, not a duration: what is LEFT of it here is
            # what the turn may spend (§12).
            seen['left']=deadline-time.monotonic();seen['timeout']=timeout
            return fake_turn()(request,binding,model_binding)
        with patch.object(rt,'turn',side_effect=turn):
            call(self.base,'/v1/chat/completions',self.completion_body())
        self.assertIsNone(seen['timeout'],'the gateway passes a deadline, never a duration')
        self.assertLessEqual(seen['left'],gw.TURN_SECONDS)
        self.assertGreater(seen['left'],gw.TURN_SECONDS-10)
    def test_temperature_and_max_tokens_are_ignored(self):
        with patch.object(rt,'turn',side_effect=fake_turn()):
            status,_=call(self.base,'/v1/chat/completions',self.completion_body(temperature=0.7,max_tokens=32))
        self.assertEqual(status,200)
    def test_deep_health_probe_is_answered_without_a_turn(self):
        with patch.object(rt,'turn',side_effect=never_called):
            status,body=call(self.base,'/v1/chat/completions',
                             {'model':self.model_id,'max_tokens':1,
                              'messages':[{'role':'user','content':'ping'}]})
        self.assertEqual(status,200)
        self.assertEqual(body['model'],self.model_id)
        self.assertEqual(body['choices'][0]['message']['content'],'pong')
        self.assertTrue(body['x_cog']['liveness'])


class TransportTests(ServerTestCase):
    def test_every_route_requires_the_token(self):
        for path in ('/health','/v1/models'):
            self.assertEqual(call(self.base,path,token=None)[0],401)
            self.assertEqual(call(self.base,path,token='wrong')[0],401)
        with patch.object(rt,'turn',side_effect=never_called):
            self.assertEqual(call(self.base,'/v1/chat/completions',self.completion_body(),token=None)[0],401)
            self.assertEqual(call(self.base,'/v1/chat/completions',self.completion_body(),token='wrong')[0],401)
    def test_matching_token_admitted(self):
        self.assertEqual(call(self.base,'/health')[0],200)
        self.assertEqual(call(self.base,'/v1/models')[0],200)
        with patch.object(rt,'turn',side_effect=fake_turn()):
            self.assertEqual(call(self.base,'/v1/chat/completions',self.completion_body())[0],200)
    def test_foreign_host_header_is_403(self):
        # A DNS-rebound name still reaches loopback; the Host is the check.
        for host in ('rebound.example','127.0.0.1','evil.test:'+str(self.port)):
            status,_=speak(self.port,f'GET /health HTTP/1.1\r\nHost: {host}\r\n'
                                     f'Authorization: Bearer {TOKEN}\r\nConnection: close\r\n\r\n'.encode())
            self.assertEqual(status,403,host)
    def test_localhost_host_header_is_accepted(self):
        status,_=speak(self.port,f'GET /health HTTP/1.1\r\nHost: localhost:{self.port}\r\n'
                                 f'Authorization: Bearer {TOKEN}\r\nConnection: close\r\n\r\n'.encode())
        self.assertEqual(status,200)
    def test_any_origin_is_403(self):
        with patch.object(rt,'turn',side_effect=never_called):
            status,body=call(self.base,'/v1/chat/completions',self.completion_body(),
                             headers={'Origin':'https://page.example'})
        self.assertEqual(status,403);self.assertEqual(body['error']['code'],'forbidden_origin')
        self.assertEqual(call(self.base,'/health',headers={'Origin':'null'})[0],403)
    def test_non_json_content_type_is_refused(self):
        # A simple cross-origin POST needs no preflight; text/plain is its shape.
        with patch.object(rt,'turn',side_effect=never_called):
            status,_=call(self.base,'/v1/chat/completions',self.completion_body(),
                          content_type='text/plain')
        self.assertEqual(status,415)
    def test_bad_content_length_is_400(self):
        with patch.object(rt,'turn',side_effect=never_called):
            for header in ('abc','-1','12 34',''):
                status,_=speak(self.port,self.post(self.completion_body(),length=header))
                self.assertEqual(status,400,header)
    def test_oversized_declared_body_is_413(self):
        with patch.object(rt,'turn',side_effect=never_called):
            status,_=speak(self.port,self.post(self.completion_body(),length=gw.MAX_BODY+1))
        self.assertEqual(status,413)
    def test_duplicate_authorization_headers_are_400_in_either_order(self):
        """One good and one bad credential is ambiguous, and must fail the same
        way whichever came first — not be decided by header order."""
        good,bad=f'Bearer {TOKEN}','Bearer wrong-credential'
        for first,second in ((good,bad),(bad,good),(good,good)):
            raw=(f'GET /health HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n'
                 f'Authorization: {first}\r\nAuthorization: {second}\r\n'
                 f'Connection: close\r\n\r\n').encode()
            status,data=speak(self.port,raw)
            self.assertEqual(status,400,(first,second))
            self.assertIn(b'Exactly one Authorization header',data)
    def test_an_unsupported_method_is_a_json_error_object(self):
        raw=(f'PUT /health HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n'
             f'Authorization: Bearer {TOKEN}\r\nContent-Length: 0\r\n'
             f'Connection: close\r\n\r\n').encode()
        status,data=speak(self.port,raw)
        self.assertEqual(status,501)
        body=json.loads(data.split(b'\r\n\r\n',1)[1].decode())
        self.assertEqual(body['error']['code'],'unsupported_method')
        self.assertEqual(body['error']['type'],'api_error')
        self.assertNotIn(b'<html>',data.lower())


class Dripper:
    """A client that never stops sending and never finishes: one byte every
    `pause` seconds, for as long as the server will listen. No inactivity
    timeout ever fires for it — only a TOTAL deadline ends it."""
    def __init__(self,port,prefix,pause=0.2):
        self.sock=socket.socket();self.sock.settimeout(5)
        self.sock.connect(('127.0.0.1',port))
        self.sock.sendall(prefix)
        self.pause=pause
        self.stopped=threading.Event()
        self.thread=threading.Thread(target=self.run,daemon=True);self.thread.start()
    def run(self):
        while not self.stopped.wait(self.pause):
            try: self.sock.sendall(b'X')
            except OSError: return
    def collect(self,wait=10):
        self.sock.settimeout(wait);data=b''
        try:
            while True:
                chunk=self.sock.recv(65536)
                if not chunk: break
                data+=chunk
        except (TimeoutError,socket.timeout,ConnectionResetError,BrokenPipeError):
            pass
        return data
    def close(self):
        self.stopped.set()
        try: self.sock.close()
        except OSError: pass


class ReadDeadlineTests(ServerTestCase):
    """A trickle is not idleness. Both read deadlines are totals."""
    header_seconds=1.0
    body_seconds=1.0
    def test_a_dripping_header_block_is_cut_off_at_the_header_deadline(self):
        dripper=Dripper(self.port,f'GET /health HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n'.encode())
        self.addCleanup(dripper.close)
        started=time.monotonic()
        self.assertEqual(dripper.collect(wait=8),b'',
                         'a client that never finishes its headers is closed, silently')
        elapsed=time.monotonic()-started
        self.assertGreater(elapsed,self.header_seconds*0.5,'closed before its deadline')
        self.assertLess(elapsed,self.header_seconds+3,
                        'the header deadline is a total, not a gap between bytes')
    def test_a_dripping_body_is_408_at_the_body_deadline(self):
        with patch.object(rt,'turn',side_effect=never_called):
            dripper=Dripper(self.port,self.post(length=4096)+b'{"par')
            self.addCleanup(dripper.close)
            started=time.monotonic()
            data=dripper.collect(wait=8)
            elapsed=time.monotonic()-started
        self.assertEqual(int(data.split(b' ')[1]),408,data[:80])
        self.assertGreater(elapsed,self.body_seconds*0.5)
        self.assertLess(elapsed,self.body_seconds+3)
    def test_eight_drippers_do_not_starve_health_past_the_deadline(self):
        """Every slot held by a trickling, unauthenticated client. /health is
        refused while they hold them and answered once their deadline passes —
        it is never waiting on their rhythm."""
        drippers=[Dripper(self.port,f'GET /health HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n'.encode())
                  for _ in range(gw.MAX_CONNECTIONS)]
        for dripper in drippers: self.addCleanup(dripper.close)
        probe=(f'GET /health HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n'
               f'Authorization: Bearer {TOKEN}\r\nConnection: close\r\n\r\n').encode()
        self.assertEqual(speak(self.port,probe,wait=2)[0],503,
                         'while every slot is held, /health is refused at once, never queued')
        started=time.monotonic()
        deadline=started+self.header_seconds+4
        status=None
        while time.monotonic()<deadline:
            status,_=speak(self.port,probe,wait=2)
            if status==200: break
            time.sleep(0.05)
        self.assertEqual(status,200,'the drippers held their slots past the header deadline')
        self.assertLess(time.monotonic()-started,self.header_seconds+4)


class ConnectionLimitTests(ServerTestCase):
    connections=1
    def test_over_the_connection_limit_is_an_immediate_503(self):
        holder=socket.create_connection(('127.0.0.1',self.port),timeout=5)
        self.addCleanup(holder.close)
        probe=(f'GET /health HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n'
               f'Authorization: Bearer {TOKEN}\r\nConnection: close\r\n\r\n').encode()
        deadline=time.monotonic()+5
        status=None
        while time.monotonic()<deadline:
            status,_=speak(self.port,probe,wait=2)
            if status==503: break
            time.sleep(0.05)
        self.assertEqual(status,503,'the connection limit must refuse rather than queue')


class RefusalTests(ServerTestCase):
    def refused(self,**overrides):
        """Every refusal is an error object, and the vendor is never reached."""
        with patch.object(rt,'turn',side_effect=never_called):
            status,body=call(self.base,'/v1/chat/completions',self.completion_body(**overrides))
        self.assertEqual(status,400,body)
        self.assertIn('A turn is one non-streaming request',body['error']['message'])
        self.assertEqual(body['error']['type'],'invalid_request_error')
        return body
    def test_json_object_response_format(self):
        self.refused(response_format={'type':'json_object'})
    def test_missing_response_format(self):
        body=self.completion_body();body.pop('response_format')
        with patch.object(rt,'turn',side_effect=never_called):
            self.assertEqual(call(self.base,'/v1/chat/completions',body)[0],400)
    def test_nested_response_format_types(self):
        # `json_schema: "oops"` used to raise an uncaught AttributeError.
        self.refused(response_format={'type':'json_schema','json_schema':'oops'})
        self.refused(response_format={'type':'json_schema','json_schema':{'schema':'oops'}})
        self.refused(response_format={'type':'json_schema','json_schema':{'name':'x'}})
        self.refused(response_format=['json_schema'])
    def test_invalid_schema_is_400_not_a_traceback(self):
        # jsonschema raises SchemaError here, which is not a ValueError.
        for schema in ({'type':'object','required':42},
                       {'type':'object','properties':{'a':{'type':'nope'}},
                        'required':['a'],'additionalProperties':False}):
            self.refused(response_format={'type':'json_schema',
                                          'json_schema':{'name':'x','strict':True,'schema':schema}})
    def test_unresolved_local_reference_is_400(self):
        self.refused(response_format={'type':'json_schema','json_schema':{'name':'x','strict':True,
                     'schema':{'type':'object','properties':{'a':{'$ref':'#/$defs/missing'}},
                               'required':['a'],'additionalProperties':False}}})
    def test_a_pointer_the_precheck_used_to_disagree_about_is_400(self):
        """`#/$defs//T` names an empty-string key. A precheck that skipped empty
        pointer components read it as `#/$defs/T`, let it through, spent a turn,
        and only then met the resolver's own exception."""
        self.refused(response_format={'type':'json_schema','json_schema':{'name':'x','strict':True,
                     'schema':{'type':'object','$defs':{'T':{'type':'object'}},'$ref':'#/$defs//T'}}})
    def test_resolved_local_reference_is_accepted(self):
        schema={'type':'object','$defs':{'text':{'type':'string'}},
                'properties':{'a':{'$ref':'#/$defs/text'}},'required':['a'],'additionalProperties':False}
        with patch.object(rt,'turn',side_effect=fake_turn({'a':'ok'})):
            status,_=call(self.base,'/v1/chat/completions',self.completion_body(
                response_format={'type':'json_schema','json_schema':{'name':'x','strict':True,'schema':schema}}))
        self.assertEqual(status,200)
    def test_body_that_is_not_json(self):
        with patch.object(rt,'turn',side_effect=never_called):
            status,_=speak(self.port,self.post()+b'',wait=5)
        self.assertEqual(status,400)
        payload=b'not json'
        head=(f'POST /v1/chat/completions HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n'
              f'Authorization: Bearer {TOKEN}\r\nContent-Type: application/json\r\n'
              f'Content-Length: {len(payload)}\r\n\r\n').encode()
        with patch.object(rt,'turn',side_effect=never_called):
            status,_=speak(self.port,head+payload)
        self.assertEqual(status,400)
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
        with patch.object(rt,'turn',side_effect=never_called):
            status,body=call(self.base,'/v1/chat/completions',self.completion_body(model='gpt-4o'))
        self.assertEqual(status,400);self.assertEqual(body['error']['code'],'model_not_found')
    def test_non_object_output_schema(self):
        self.refused(response_format={'type':'json_schema','json_schema':{'name':'x','strict':True,
                     'schema':{'type':'array','items':{'type':'string'}}}})
    def test_remote_schema_reference(self):
        self.refused(response_format={'type':'json_schema','json_schema':{'name':'x','strict':True,
                     'schema':{'type':'object','$ref':'https://example.com/s.json'}}})
    def test_failed_turn_is_502_never_a_completion(self):
        def turn(request,binding,model_binding=None,timeout=None,deadline=None):
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


class LivenessTests(ServerTestCase):
    """The shortcut has one signature. Everything else without a
    `response_format` is a 400 — no request can collect a judgment-shaped
    `pong` by asking for a single token."""
    def probe(self,body):
        with patch.object(rt,'turn',side_effect=never_called):
            return call(self.base,'/v1/chat/completions',body)
    def test_the_exact_probe_is_answered_without_a_turn(self):
        status,body=self.probe({'model':self.model_id,'max_tokens':1,
                                'messages':[{'role':'user','content':'ping'}]})
        self.assertEqual(status,200)
        self.assertEqual(body['choices'][0]['message']['content'],'pong')
        self.assertTrue(body['x_cog']['liveness'])
    def test_every_near_miss_is_a_400(self):
        for body in ({'model':self.model_id,'max_tokens':1,
                      'messages':[{'role':'user','content':'Answer Y or N: prioritize this issue?'}]},
                     {'max_tokens':1},
                     {'model':self.model_id,'max_tokens':1},
                     {'model':self.model_id,'max_tokens':2,
                      'messages':[{'role':'user','content':'ping'}]},
                     {'model':self.model_id,'max_tokens':1,
                      'messages':[{'role':'user','content':'ping '}]},
                     {'model':self.model_id,'max_tokens':1,'temperature':0,
                      'messages':[{'role':'user','content':'ping'}]},
                     {'model':self.model_id,'max_tokens':1,
                      'messages':[{'role':'system','content':'Judge this.'},
                                  {'role':'user','content':'ping'}]},
                     {'model':self.model_id,'max_tokens':1,
                      'messages':[{'role':'user','content':'ping'},
                                  {'role':'user','content':'ping'}]}):
            status,reply=self.probe(body)
            self.assertEqual(status,400,body)
            self.assertNotIn('choices',reply)


class SerializationTests(ServerTestCase):
    def test_one_turn_at_a_time(self):
        live,overlap=[],[]
        lock=threading.Lock()
        def turn(request,binding,model_binding=None,timeout=None,deadline=None):
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


class QueueTestCase(ServerTestCase):
    def held_turn(self,release,started,calls):
        """A turn that blocks until the test lets it go, so the second request
        is genuinely waiting on the lock."""
        def turn(request,binding,model_binding=None,timeout=None,deadline=None):
            calls.append(request['request_id'])
            started.set()
            release.wait(20)
            return fake_turn()(request,binding,model_binding)
        return turn
    def occupy(self,release,started,calls):
        first=threading.Thread(target=lambda:call(self.base,'/v1/chat/completions',
                                                  self.completion_body()),daemon=True)
        first.start()
        self.assertTrue(started.wait(10),'the first turn never started')
        return first


class QueueTests(QueueTestCase):
    queue_wait=0.5
    def test_a_full_queue_answers_503_busy(self):
        release,started,calls=threading.Event(),threading.Event(),[]
        with patch.object(rt,'turn',side_effect=self.held_turn(release,started,calls)):
            first=self.occupy(release,started,calls)
            status,body=call(self.base,'/v1/chat/completions',self.completion_body())
            release.set();first.join(20)
        self.assertEqual(status,503);self.assertEqual(body['error']['code'],'busy')
        self.assertEqual(len(calls),1,'the refused request must not have started a turn')


class AbandonedQueueTests(QueueTestCase):
    queue_wait=30  # long: the caller leaving, not the deadline, must end this
    def test_an_abandoned_queue_entry_is_usually_dropped_before_a_turn(self):
        """Best effort, and named as such (§12): the disconnect check is a
        heuristic — two successful writes do not prove a reader — so this
        establishes that a caller which HAS left is detected here, not that one
        can never slip through."""
        release,started,calls=threading.Event(),threading.Event(),[]
        log=io.StringIO()
        with patch.object(rt,'turn',side_effect=self.held_turn(release,started,calls)),\
             contextlib.redirect_stderr(log):
            first=self.occupy(release,started,calls)
            # Queue a second request, then walk away before the lock frees.
            _,waiting=speak(self.port,self.post(self.completion_body()),hold=True)
            time.sleep(0.5)
            waiting.close()
            release.set();first.join(20)
            time.sleep(0.5)
        self.assertEqual(len(calls),1,'a caller that left while queued should not spend a turn')
        self.assertIn('left while queued',log.getvalue())


class BudgetTests(QueueTestCase):
    """One monotonic budget per request: the queue wait, the readiness commands
    and the vendor command all draw on it."""
    turn_timeout=2
    queue_wait=30
    min_inference=1
    def test_the_queue_wait_is_bounded_by_what_the_budget_leaves(self):
        release,started,calls,seen=threading.Event(),threading.Event(),[],{}
        def turn(request,binding,model_binding=None,timeout=None,deadline=None):
            # The gateway hands on the request's absolute deadline, not a
            # duration captured before the disconnect probe (§12).
            seen['left']=deadline-time.monotonic()
            return self.held_turn(release,started,calls)(request,binding,model_binding)
        with patch.object(rt,'turn',side_effect=turn):
            first=self.occupy(release,started,calls)
            began=time.monotonic()
            status,body=call(self.base,'/v1/chat/completions',self.completion_body())
            elapsed=time.monotonic()-began
            release.set();first.join(20)
        self.assertEqual(status,503);self.assertEqual(body['error']['code'],'busy')
        self.assertEqual(len(calls),1,'the refused request must not have started a turn')
        self.assertLess(elapsed,self.turn_timeout,
                        'the 30 s queue wait must be cut to what the 2 s budget leaves')
        self.assertGreater(elapsed,self.turn_timeout-self.min_inference-0.5)
        self.assertIn('budget',body['error']['message'])
        # And the running turn was given the budget, not the flag's own value.
        self.assertLessEqual(seen['left'],self.turn_timeout)
        self.assertGreater(seen['left'],0)


class ExhaustedBudgetTests(ServerTestCase):
    """The budget starts when the request is accepted, so a slow upload spends
    it like anything else. Too little left to finish a turn is 503 busy, not a
    turn nobody will be waiting for."""
    turn_timeout=1.0
    min_inference=0.5
    body_seconds=8
    def test_a_budget_spent_before_the_turn_is_busy_and_spends_nothing(self):
        head,_,raw=self.post(self.completion_body()).partition(b'\r\n\r\n')
        sock=socket.create_connection(('127.0.0.1',self.port),timeout=15)
        self.addCleanup(sock.close)
        with patch.object(rt,'turn',side_effect=never_called):
            sock.sendall(head+b'\r\n\r\n'+raw[:20])
            time.sleep(self.turn_timeout+0.3)
            sock.sendall(raw[20:])
            sock.settimeout(10);data=b''
            while b'\r\n\r\n' not in data or not data.endswith(b'}'):
                chunk=sock.recv(65536)
                if not chunk: break
                data+=chunk
        self.assertEqual(int(data.split(b' ')[1]),503,data[:120])
        body=json.loads(data.split(b'\r\n\r\n')[-1].decode())
        self.assertEqual(body['error']['code'],'busy')
        self.assertIn('no turn was started',body['error']['message'])


class DisconnectTests(ServerTestCase):
    def test_a_turn_whose_caller_left_finishes_and_is_discarded(self):
        started,finished=threading.Event(),threading.Event()
        def turn(request,binding,model_binding=None,timeout=None,deadline=None):
            started.set()
            time.sleep(0.6)
            response=fake_turn()(request,binding,model_binding)
            finished.set()
            return response
        log=io.StringIO()
        with patch.object(rt,'turn',side_effect=turn),contextlib.redirect_stderr(log):
            _,sock=speak(self.port,self.post(self.completion_body()),hold=True)
            self.assertTrue(started.wait(10))
            sock.close()
            self.assertTrue(finished.wait(10),'the paid-for turn must run to completion')
            time.sleep(0.5)
            # No second response is attempted, and the gateway still serves.
            with patch.object(rt,'turn',side_effect=fake_turn()):
                self.assertEqual(call(self.base,'/v1/chat/completions',self.completion_body())[0],200)
        self.assertIn('discarded',log.getvalue())


def final_response(data):
    """The last HTTP response in a byte stream that may open with interim ones.
    Every client must skip a 1xx; these tests read past them deliberately."""
    heads=[block for block in data.split(b'\r\n\r\n') if block.startswith(b'HTTP/')]
    return int(heads[-1].split(b' ')[1]),json.loads(data.split(b'\r\n\r\n')[-1].decode())


class HalfCloseTests(ServerTestCase):
    def test_a_half_closed_client_is_still_a_client(self):
        """It sent everything it had to send and closed that direction only. It
        is still reading, and the turn it paid for is delivered to it."""
        log=io.StringIO()
        with patch.object(rt,'turn',side_effect=fake_turn({'answer':'delivered'})),\
             contextlib.redirect_stderr(log):
            sock=socket.create_connection(('127.0.0.1',self.port),timeout=20)
            self.addCleanup(sock.close)
            sock.sendall(self.post(self.completion_body()))
            sock.shutdown(socket.SHUT_WR)
            data=b''
            sock.settimeout(20)
            while b'\r\n\r\n' not in data or not data.split(b'\r\n\r\n')[-1].endswith(b'}'):
                chunk=sock.recv(65536)
                if not chunk: break
                data+=chunk
        status,body=final_response(data)
        self.assertEqual(status,200,data[:200])
        self.assertEqual(json.loads(body['choices'][0]['message']['content']),{'answer':'delivered'})
        self.assertNotIn('discarded',log.getvalue())


    def drain(self,sock,seconds=20):
        data=b'';sock.settimeout(seconds)
        while b'\r\n\r\n' not in data or not data.split(b'\r\n\r\n')[-1].endswith(b'}'):
            chunk=sock.recv(65536)
            if not chunk: break
            data+=chunk
        return data
    def half_closed_request(self,version):
        """Send a complete request, close the sending direction only, read."""
        with patch.object(rt,'turn',side_effect=fake_turn({'answer':'delivered'})):
            sock=socket.create_connection(('127.0.0.1',self.port),timeout=20)
            self.addCleanup(sock.close)
            sock.sendall(self.post(self.completion_body(),version=version))
            sock.shutdown(socket.SHUT_WR)
            return self.drain(sock)
    def test_an_http_1_1_half_close_is_probed_with_an_interim_response(self):
        """The probe is legitimate for HTTP/1.1: such a client must parse and
        skip a 1xx, and the two writes are how the gateway asks whether anybody
        is still reading."""
        data=self.half_closed_request('HTTP/1.1')
        self.assertIn(b'HTTP/1.1 100 Continue',data,data[:200])
        status,body=final_response(data)
        self.assertEqual(status,200,data[:200])
        self.assertEqual(json.loads(body['choices'][0]['message']['content']),{'answer':'delivered'})
    def test_an_http_1_0_caller_is_never_sent_a_1xx(self):
        """HTTP forbids an informational response to an HTTP/1.0 client, which
        has no rule for reading one (§12). That caller is treated as present —
        the direction this check errs in anyway — and simply gets its result."""
        data=self.half_closed_request('HTTP/1.0')
        self.assertNotIn(b' 100 ',data,data[:200])
        self.assertNotIn(b'Continue',data,data[:200])
        status,body=final_response(data)
        self.assertEqual(status,200,data[:200])
        self.assertEqual(json.loads(body['choices'][0]['message']['content']),{'answer':'delivered'})


class WriteWindowTests(unittest.TestCase):
    """§12: headers, interim responses and the body share ONE write deadline.

    A `settimeout` per write bounds each write and no total: a reader that
    takes just under the limit for every piece holds its slot for as long as
    there are pieces. Driven here against a fake socket and an injected clock,
    so the arithmetic is exact and nothing waits."""
    class Clock:
        def __init__(self): self.now=1000.0
        def monotonic(self): return self.now
        def spend(self,seconds): self.now+=seconds
    class Peer:
        """A socket that takes one byte per send, and charges for it."""
        def __init__(self,clock,cost=0.4):
            self.clock,self.cost,self.sent,self.armed=clock,cost,b'',[]
        def settimeout(self,value): self.armed.append(value)
        def send(self,view):
            self.clock.spend(self.cost);self.sent+=bytes(view[:1]);return 1
    def writer(self,cost=0.4):
        clock=self.Clock();peer=self.Peer(clock,cost)
        return clock,peer,gw.Deadlined(peer,clock=clock.monotonic)
    def test_the_aggregate_is_bounded_not_each_write(self):
        _,peer,writer=self.writer()
        writer.arm(1.0)
        writer.write(b'ab')                      # 0.8 s of the one window
        with self.assertRaises(TimeoutError): writer.write(b'cd')
        self.assertEqual(peer.sent,b'abc',
                         'a fourth byte went out after the window had closed')
        for armed,expected in zip(peer.armed,[1.0,0.6,0.2]):
            self.assertAlmostEqual(armed,expected,places=6,
                                   msg='the socket timeout was reset to the full '
                                       'allowance instead of what remains')
        self.assertEqual(len(peer.armed),3)
    def test_a_second_arm_does_not_extend_the_window(self):
        """An interim probe arms it; the response that follows must not push
        the same window out again."""
        clock,peer,writer=self.writer(cost=0.0)
        writer.arm(1.0);opened=writer.deadline
        clock.spend(0.9)
        self.assertEqual(writer.arm(1.0),opened)
        writer.write(b'x')
        self.assertAlmostEqual(peer.armed[-1],0.1,places=6)
    def test_disarming_starts_the_next_response_fresh(self):
        clock,_,writer=self.writer(cost=0.0)
        writer.arm(1.0);clock.spend(5);writer.disarm()
        self.assertEqual(writer.arm(1.0),clock.monotonic()+1.0)


class HalfCloseLongTurnTests(ServerTestCase):
    """Internal review finding: the interim probe armed the write window, the turn ran
    inside it, and a half-closed caller's paid result was discarded when the
    turn outlasted the window."""
    write_seconds=0.5
    def test_a_turn_longer_than_the_write_window_is_still_delivered(self):
        log=io.StringIO()
        with patch.object(rt,'turn',side_effect=fake_turn(
                {'answer':'delivered late'},before=lambda request: time.sleep(1.5))),\
             contextlib.redirect_stderr(log):
            sock=socket.create_connection(('127.0.0.1',self.port),timeout=20)
            self.addCleanup(sock.close)
            sock.sendall(self.post(self.completion_body()))
            sock.shutdown(socket.SHUT_WR)
            data=b'';sock.settimeout(20)
            while b'\r\n\r\n' not in data or not data.split(b'\r\n\r\n')[-1].endswith(b'}'):
                chunk=sock.recv(65536)
                if not chunk: break
                data+=chunk
        self.assertIn(b'HTTP/1.1 100 Continue',data,data[:200])
        status,body=final_response(data)
        self.assertEqual(status,200,data[:300])
        self.assertEqual(json.loads(body['choices'][0]['message']['content']),{'answer':'delivered late'})
        self.assertNotIn('discarded',log.getvalue())


class StalledReaderTests(ServerTestCase):
    connections=1
    write_seconds=0.5
    def test_a_reader_that_stops_reading_is_dropped_at_the_write_deadline(self):
        """A connected client that never drains the socket would otherwise hold
        its slot for as long as it liked. Eight of those is the whole gateway."""
        reached=threading.Event()
        def turn(request,binding,model_binding=None,timeout=None,deadline=None):
            # One replacement for both requests: a result too big for any
            # socket buffer only for the one the stalled client sent.
            huge='STALL' in request['task']['input']
            if huge: reached.set()
            return fake_turn({'answer':'x'*(4*1024*1024) if huge else 'ok'})(
                request,binding,model_binding)
        log=io.StringIO()
        sock=socket.socket()
        sock.setsockopt(socket.SOL_SOCKET,socket.SO_RCVBUF,2048)  # before connect
        sock.settimeout(10);sock.connect(('127.0.0.1',self.port))
        self.addCleanup(sock.close)
        with patch.object(rt,'turn',side_effect=turn),contextlib.redirect_stderr(log):
            sock.sendall(self.post(self.completion_body(
                messages=[{'role':'user','content':'STALL: reply and I will not read it'}])))
            self.assertTrue(reached.wait(10),'the stalled request never reached its turn')
            started=time.monotonic()
            # Never read a byte of it. The slot must come back on its own.
            self.assertEqual(call(self.base,'/health',timeout=5)[0],503,
                             'the stalled reader should still hold the only slot')
            status=None
            deadline=time.monotonic()+10
            while time.monotonic()<deadline:
                status,_=call(self.base,'/v1/chat/completions',self.completion_body(),timeout=5)
                if status==200: break
                time.sleep(0.05)
        self.assertEqual(status,200,'the stalled reader never gave its slot back')
        elapsed=time.monotonic()-started
        self.assertGreater(elapsed,self.write_seconds*0.5)
        self.assertLess(elapsed,self.write_seconds+4,'dropped later than the write deadline')
        self.assertIn('stopped reading',log.getvalue())


class CallerTests(ServerTestCase):
    """The real context-cog caller, not a hand-copied request body."""
    def setUp(self):
        if not (CALLER/'src/cog_core.py').is_file():
            self.skipTest(f'{CALLER.name} (optional legacy package, not part of the suite) '
                          f'is not beside this package; the real-caller path is unverified '
                          f'in this checkout')
        sys.path.insert(0,str(CALLER/'src'))
        try:
            import cog_core
        except ImportError as exc:                       # pragma: no cover
            self.skipTest(f'{CALLER.name} could not be imported ({exc}); the real-caller '
                          f'path is unverified in this environment')
        self.core=cog_core
        super().setUp()
        # Its binding comes from model.json; point the module at this server.
        for patcher in (patch.object(self.core,'ENDPOINT',self.base+'/v1'),
                        patch.object(self.core,'MODEL',self.model_id),
                        patch.object(self.core,'API_KEY',TOKEN),
                        patch.object(self.core,'RESPONSE_FORMAT','json_schema'),
                        patch.dict(self.core.RECORD,{'locality':'local'})):
            patcher.start();self.addCleanup(patcher.stop)
    def bundle(self):
        return json.loads((CALLER/'examples/sample-bundle.json').read_text())
    def example_result(self):
        return json.loads((CALLER/'context/output-example.json').read_text())
    def test_invoke_reaches_the_gateway_and_returns_an_envelope(self):
        result=self.example_result()
        with patch.object(rt,'turn',side_effect=fake_turn(result)):
            envelope=self.core.invoke(self.bundle())
        self.assertTrue(envelope['ok'],envelope.get('error'))
        self.assertEqual(envelope['payload'],result)
        self.assertFalse([p for p in envelope['problems'] if p['check']=='schema'],envelope['problems'])
    def test_the_caller_sends_the_context_the_gateway_maps_to_the_turn(self):
        seen={}
        def turn(request,binding,model_binding=None,timeout=None,deadline=None):
            seen.update(copy.deepcopy(request))
            return fake_turn(self.example_result())(request,binding,model_binding)
        with patch.object(rt,'turn',side_effect=turn):
            self.core.invoke(self.bundle())
        self.assertEqual([x['id'] for x in seen['context']],[gw.CONTEXT_ID])
        self.assertEqual(seen['context'][0]['content'],self.core.load_context())
        self.assertEqual(seen['task']['input'],
                         self.core.task_logic.render_input(self.bundle()))
        self.assertEqual(seen['task']['output_schema'],self.core.OUTPUT_SCHEMA)
    def test_shallow_health_and_deep_health_both_answer(self):
        ok,detail=self.core.health()
        self.assertTrue(ok,detail)
        with patch.object(rt,'turn',side_effect=never_called):
            ok,detail=self.core.health(deep=True)
        self.assertTrue(ok,detail)
        self.assertIn('identity matches',detail)
    def test_a_caller_without_the_token_is_refused(self):
        """Two refusals, and today's caller names them differently. Its own
        readiness probe cannot reach an authenticated `/health` or `/v1/models`,
        so `invoke` stops before any completion (`model-unavailable`)."""
        with patch.object(self.core,'API_KEY',None):
            ok,detail=self.core.health()
            self.assertFalse(ok,detail)
            with patch.object(rt,'turn',side_effect=never_called):
                envelope=self.core.invoke(self.bundle())
        self.assertFalse(envelope['ok'])
        self.assertEqual(envelope['error']['code'],'model-unavailable')
    def test_the_gateways_401_on_a_completion_is_the_callers_call_failure(self):
        """With the probe out of the way, the completion itself meets the 401.
        Today's caller maps that HTTP error to `model-call-failed` — the code
        this test asserted before the caller changed was `model-unavailable`."""
        alive=(True,'probe bypassed: the transport refusal is what is under test')
        with patch.object(self.core,'API_KEY',None),\
             patch.object(self.core,'health',return_value=alive),\
             patch.object(rt,'turn',side_effect=never_called):
            envelope=self.core.invoke(self.bundle())
        self.assertFalse(envelope['ok'])
        self.assertEqual(envelope['error']['code'],'model-call-failed')
        self.assertIn('401',envelope['error']['detail'])


class NativeSchemaTests(unittest.TestCase):
    """The conservative detector: outside the named subset, the prompt path."""
    def test_the_closed_object_subset_is_native(self):
        self.assertTrue(rt.native_schema(SCHEMA))
        self.assertTrue(rt.native_schema({'$schema':'https://json-schema.org/draft/2020-12/schema',
                                          **SCHEMA}))
    def test_anything_outside_the_subset_is_not(self):
        nested=lambda inner:{'type':'object','properties':{'a':inner},'required':['a'],
                             'additionalProperties':False}
        for schema in ({'type':'object'},
                       {'type':['object','null']},
                       nested({'type':['object','null']}),
                       nested({'$schema':'https://json-schema.org/draft/2020-12/schema','type':'string'}),
                       nested({'type':'object','properties':{'b':{'type':'string'}},'required':['b']}),
                       nested({'anyOf':[{'type':'string'}]}),
                       nested({'$ref':'#/$defs/x'}),
                       nested({'type':'string','pattern':'^x'}),
                       {'type':'object','$defs':{'x':{'type':'string'}},
                        'properties':{'a':{'type':'string'}},'required':['a'],'additionalProperties':False}):
            self.assertFalse(rt.native_schema(schema),schema)
    def test_a_keyword_that_does_not_belong_to_the_type_is_not_native(self):
        """JSON Schema permits `items` beside `properties` and ignores it; the
        detector does not, because the vendor's strict parser may not."""
        nested=lambda inner:{'type':'object','properties':{'a':inner},'required':['a'],
                             'additionalProperties':False}
        for schema in (dict(SCHEMA,items={'type':['object','null'],'$id':'nested'}),
                       dict(SCHEMA,items={'type':'string'}),
                       nested({'type':'string','properties':{'b':{'type':'string'}}}),
                       nested({'type':'array','items':{'type':'string'},'required':['a']}),
                       nested({'type':'object','properties':{'b':{'type':'string'}},
                               'required':['b'],'additionalProperties':False,
                               'items':{'type':'string'}}),
                       nested({'type':'boolean','enum':[True]})):
            self.assertFalse(rt.native_schema(schema),schema)
    def test_the_subset_still_admits_what_it_always_did(self):
        self.assertTrue(rt.native_schema({'type':'object','additionalProperties':False,
            'required':['a','b'],'properties':{
                'a':{'type':'string','enum':['x','y'],'description':'d'},
                'b':{'type':'array','items':{'type':'object','additionalProperties':False,
                     'required':['c'],'properties':{'c':{'type':'integer'}}}}}}))


class CopyTests(unittest.TestCase):
    SIBLINGS=('cog-claude','cog-chatgpt','cog-turn-harness')
    SHARED=('src/turn_gateway.py','src/turn_runtime.py','tests/test_gateway.py','tests/test_provider.py')
    def test_shared_sources_are_byte_identical_across_providers(self):
        checked=0
        for name in self.SHARED:
            mine=ROOT/name
            for sibling in self.SIBLINGS:
                other=ROOT.parent/sibling/name
                if not other.exists() or other.resolve()==mine.resolve():
                    continue
                checked+=1
                self.assertEqual(mine.read_bytes(),other.read_bytes(),
                                 f'{other} differs; {name} is copied byte-for-byte')
        if not checked:
            self.skipTest('no sibling provider packages beside this one')


if __name__=='__main__':unittest.main()
