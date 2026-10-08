"""Opt-in, sanitized CLI qualification. Never admits a provider or copies login data."""
import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import sys
import time
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
import turn_runtime as rt


def package_digest(root):
    paths = [root/name for name in ('cog.yaml','pixi.toml','engine.json','model-artifact.json') if (root/name).is_file()]
    for name in ('src','scripts','context','binding','contracts'):
        if (root/name).is_dir():
            paths += [p for p in (root/name).rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc']
    values = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths)}
    return hashlib.sha256(json.dumps(values,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()

def qualify(binding, model, run_live=False):
    rt.require(run_live, 'Live qualification requires --run-live; it may spend two subscription turns.')
    rt.require(rt.ENGINE['engine'] in ('codex','claude'), 'Qualification is for subscription adapters only.')
    rt.validate(binding,'binding')
    rt.require(binding['state']=='admitted' and binding['provider']==rt.IDENTITY, 'Supply an independently admitted binding for this provider.')
    rt.require(isinstance(model,str) and model and model==binding['model']['id'], 'Explicit model must match the admitted binding.')
    report={'schema':'openteams/provider-qualification [0.1]', 'provider':rt.IDENTITY,
            'date':datetime.now(timezone.utc).isoformat(), 'platform':{'system':platform.system(),'machine':platform.machine()},
            'python':platform.python_version(), 'requested_model':model,'model_identity_verified':False,
            'binding':{'binding_id':binding['binding_id'],'revision':binding['revision']},
            'adapter_sha256':package_digest(ROOT),
            'checks':[], 'scope':'Installed CLI and composed-system observations; no model-quality, weights, vendor-tool or OS-isolation attestation.'}
    def observe(name, function):
        started=time.monotonic()
        try: passed=function() is True
        except Exception: passed=False
        # Exception text, stdout/stderr, model output and login responses can contain
        # sensitive account/transport details. Reports retain only named outcomes.
        report['checks'].append({'check':name,'passed':passed,'elapsed_seconds':round(time.monotonic()-started,3)})
        return passed
    def readiness():
        status=rt.doctor()
        report['cli_version']=status['version']
        return status['version']==binding['harness']['version']
    if not observe('authentication-controls-and-exact-version',readiness):
        report['passed']=False;return report
    schema={'type':'object','properties':{'ready':{'type':'boolean'}},'required':['ready'],'additionalProperties':False}
    request={'document_kind':'harness_turn_request','contract':rt.CONTRACT,'request_id':'qualification-synthetic',
             'binding':report['binding'],'model_binding':None,'consumer':{'id':'test/qualification','version':'1'},
             'context':[{'id':'qualification','content':'This is a synthetic adapter check. Return only the requested JSON. Do not use tools or recall a prior conversation.'}],
             'task':{'input':'Return ready=true.','output_schema':schema},'tool_grant_refs':[],'thread_ref':None}
    def structured(open_ended=False):
        value=copy.deepcopy(request)
        if open_ended:value['task']['output_schema']['additionalProperties']=True
        result=rt.turn(value,binding,timeout=120)
        return bool(result['ok'] and result['payload']['result'].get('ready') is True and result['payload']['tool_uses']==[])
    observe('native-structured-output',structured)
    observe('open-schema-local-validation',lambda:structured(True))
    def timeout():
        # Deterministic local supervisor check; no live inference is interrupted.
        terminated = False
        try:rt.command([sys.executable, '-c', 'import time;time.sleep(30)'], timeout=.05)
        except ValueError as exc:terminated = 'timed out' in str(exc)
        try:rt.turn(request,binding,deadline=time.monotonic()-1)
        except ValueError as exc:return terminated and ('timed out' in str(exc) or 'budget' in str(exc))
        return False
    observe('local-process-timeout-and-expired-turn-deadline',timeout)
    def refused():
        denied=copy.deepcopy(request);denied['tool_grant_refs']=['qualification-unsupported-grant']
        try:rt.check_turn(denied,binding)
        except ValueError:return True
        return False
    observe('unsupported-tool-grant-refused-before-inference',refused)
    report['passed']=all(row['passed'] for row in report['checks'])
    return report


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binding',required=True);parser.add_argument('--model',required=True)
    parser.add_argument('--output',required=True);parser.add_argument('--run-live',action='store_true')
    args=parser.parse_args(argv)
    output = Path(args.output)
    claimed = False
    started = False
    try:
        rt.require(args.run_live, 'Live qualification requires --run-live.')
        value=rt.read(args.binding);binding=value.get('binding',value)
        rt.require(isinstance(value.get('package_sha256'), str) and value['package_sha256']==package_digest(ROOT), 'Supply a current Workbench admission record for this package.')
        with output.open('x',encoding='utf-8') as stream:
            claimed = True
            started = True
            report=qualify(binding,args.model,args.run_live)
            json.dump(report,stream,indent=2);stream.write('\n')
        print(json.dumps({'passed':report['passed'],'report':str(output)}))
        return 0 if report['passed'] else 1
    except Exception:
        if claimed:
            output.unlink(missing_ok=True)
        stage = 'Qualification failed after starting checks.' if started else 'Qualification preflight failed.'
        print(json.dumps({'passed':False,'error':stage+' Supply a current Workbench admission record, matching requested model, writable new output path and --run-live. Inspect login/CLI directly; no raw diagnostics are exported.'}))
        return 1

if __name__=='__main__':raise SystemExit(main())
