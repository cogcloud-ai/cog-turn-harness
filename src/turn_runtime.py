"""Reference turn provider. Host admission and context checks are separate tasks.

Vendored unchanged by the subscription adapters; engine.json owns their identity.
No automatic login, credential copying, generated commands, or model substitution.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

ROOT = Path(__file__).resolve().parents[1]
ENGINE = json.loads((ROOT / 'engine.json').read_text())
CARD = json.loads((ROOT / 'binding/provider.json').read_text())
SCHEMA = json.loads((ROOT / 'contracts/satisfier-binding.schema.json').read_text())
CONTRACT = CARD['contract']
IDENTITY = CARD['provider']
MAX_BYTES = 8 * 1024 * 1024
VENDOR_SECONDS = 600
MODEL_SECONDS = 180
READY_SECONDS = 15       # one readiness command, or what the budget leaves


def _reference_errors():
    """The exception family the resolver that jsonschema ACTUALLY uses raises
    for a reference it cannot resolve. Caught in the precheck and again after a
    turn, so a schema that defeats the precheck is an error object, never a
    traceback."""
    found = []
    try:  # jsonschema >= 4.18 resolves through its own `referencing` dependency
        from referencing.exceptions import Unresolvable
        found.append(Unresolvable)
    except ImportError:  # pragma: no cover - older jsonschema
        pass
    from jsonschema import exceptions as _exceptions
    legacy = getattr(_exceptions, '_RefResolutionError', None)
    if isinstance(legacy, type):
        found.append(legacy)
    return tuple(found)


REFERENCE_ERRORS = _reference_errors()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def validate(value, definition=None, schema=None):
    if schema is None:
        schema = dict(SCHEMA, **{'$ref': '#/$defs/' + definition})
        schema.pop('oneOf', None)
    try:
        errors = list(Draft202012Validator(schema).iter_errors(value))
    except SchemaError as exc:
        raise ValueError('Schema is not a valid JSON Schema: ' + str(exc).split('\n')[0]) from None
    except REFERENCE_ERRORS as exc:
        raise ValueError('Schema reference does not resolve: ' + str(exc).split('\n')[0]) from None
    require(not errors, 'Contract validation failed: ' + (str(errors[0].json_path) if errors else ''))


def envelope(task, payload=None, error=None, binding=None):
    return {'envelope': 1, 'cog': IDENTITY, 'task': task, 'ok': error is None,
            'payload': payload, 'error': {'code': 'turn-provider', 'detail': error} if error else None,
            'raw': None, 'problems': [{'check': 'turn-provider', 'detail': error, 'severity': 'error'}] if error else [],
            'binding': binding, 'timing': {'latency_s': None}}


def clean_env():
    # Vendor login stores stay vendor-owned. API keys, gateway credentials and
    # endpoint overrides never flow into a subscription subprocess.
    keep = {'PATH', 'HOME', 'USER', 'LOGNAME', 'TMPDIR', 'LANG', 'LC_ALL', 'SYSTEMROOT',
            'CODEX_HOME', 'CLAUDE_CONFIG_DIR'}
    return {k: v for k, v in os.environ.items() if k in keep}


def command(argv, prompt=None, cwd=None, timeout=VENDOR_SECONDS, include_stderr=False):
    # File-backed capture bounds memory. Kill the whole process group on timeout.
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        try:
            proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=output, stderr=errors,
                                    cwd=cwd, env=clean_env(), start_new_session=True)
        except OSError:
            raise ValueError('Vendor CLI is unavailable; install it and log in using its own login command.') from None
        try:
            proc.communicate((prompt or '').encode(), timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            raise ValueError('Vendor CLI timed out; no result accepted.') from None
        require(proc.returncode == 0, 'Vendor CLI failed; check its installation and subscription login directly.')
        output.seek(0)
        data = output.read(MAX_BYTES + 1)
        require(len(data) <= MAX_BYTES, 'Vendor response exceeded the supported size.')
        if not data.strip():
            errors.seek(0)
            tail = errors.read(MAX_BYTES).decode('utf-8', 'replace')[-800:].strip()
            raise ValueError('Vendor CLI returned no output' + ('; stderr: ' + tail if tail else '.'))
        if include_stderr:
            errors.seek(0)
            data += errors.read(MAX_BYTES)
        return data.decode('utf-8')


def vendor_executable():
    name = 'codex' if ENGINE['engine'] == 'codex' else 'claude'
    executable = shutil.which(name)
    if not executable:
        local = Path.home() / '.local' / 'bin' / name
        if local.is_file():
            executable = str(local)
    require(executable is not None, 'Install the vendor CLI and log in with your subscription first.')
    return executable


def remaining_budget(deadline, what):
    """What is left of ONE monotonic end-to-end budget. Nothing starts on an
    exhausted budget: a command begun with no time left can only be killed."""
    if deadline is None:
        return None
    left = deadline - time.monotonic()
    require(left > 0, f'The turn budget was spent before {what} could start; no turn was performed.')
    return left


def readiness_timeout(deadline):
    left = remaining_budget(deadline, 'a readiness command')
    return READY_SECONDS if left is None else min(READY_SECONDS, left)


def doctor(deadline=None):
    """`deadline` is a monotonic instant: the readiness commands draw on the
    same end-to-end budget as the turn they precede, each getting
    min(READY_SECONDS, what remains)."""
    engine = ENGINE['engine']
    if engine == 'openai-compatible':
        return {'available': True, 'engine': engine, 'version': IDENTITY['version']}
    executable = vendor_executable()
    require(executable is not None, 'Install the vendor CLI and log in with your subscription first.')
    version = command([executable, '--version'], timeout=readiness_timeout(deadline)).strip()
    help_text = command([executable, 'exec', '--help'] if engine == 'codex' else [executable, '--help'], timeout=readiness_timeout(deadline))
    flags = ['--ignore-user-config', '--ephemeral', '--output-schema'] if engine == 'codex' else ['--safe-mode', '--tools', '--no-session-persistence', '--json-schema']
    require(all(x in help_text for x in flags), 'This vendor CLI is too old for the adapter safety and output controls.')
    status = command([executable, 'login', 'status'] if engine == 'codex' else [executable, 'auth', 'status'], timeout=readiness_timeout(deadline), include_stderr=engine == 'codex')
    if engine == 'codex':
        require('ChatGPT' in status, 'A ChatGPT subscription login is required; API-key login is not accepted.')
    else:
        auth = json.loads(status)
        require(auth.get('loggedIn') is True and auth.get('authMethod') == 'claude.ai',
                'A Claude subscription login is required; API-key login is not accepted.')
    return {'available': True, 'engine': engine, 'version': version}


def candidate(request, inspect=doctor):
    validate(request, 'bind_request')
    require(request['provider'] == IDENTITY, 'Provider identity mismatch.')
    validate(request['configuration'], schema=CARD['configuration_schema'])
    validate(request['credential_refs'], schema=CARD['credential_schema'])
    req, config = request['requirement'], request['configuration']
    composition = CARD['compositions'][0]
    require(req['capability'] == CARD['capability'] and composition in req['accepted_compositions'], 'Incompatible capability or composition.')
    require(set(req['features']) <= set(ENGINE['features']), 'Unsupported interaction features.')
    require('cloud' in req['allowed_localities'], 'This reference requires cloud processing permission.')
    require(not req['identity_verified'] and not req['revision_pinned'], 'Immutable model identity cannot be attested by this provider.')
    require(req['evidence_level'] == 'declaration', 'This reference advertises declaration evidence only; host conformance tests are separate.')
    if composition == 'model+harness':
        require(req['model_id'] in (None, config['model_id']), 'Configured model differs from requirement.')
    else:
        require(req['model_id'] is None, 'Put model identity constraints on the separate model requirement.')
    status = inspect()
    binding = {'document_kind': 'binding', 'contract': CONTRACT, 'binding_id': request['binding_id'],
        'revision': request['revision'], 'state': 'candidate', 'provider': IDENTITY,
        'requirement_id': req['id'], 'configuration': config, 'credential_refs': request['credential_refs'],
        'capability': CARD['capability'], 'features': req['features'], 'composition': composition,
        'model': {'id': config['model_id'], 'revision': None, 'digest': None} if composition == 'model+harness' else None,
        'harness': {'id': ENGINE['harness_id'], 'version': status['version'], 'configuration_digest': 'sha256:' + digest(config)},
        'model_binding': config.get('model_binding'), 'locality': 'cloud',
        'invocation': {'protocol': 'cog-harness-turn-command-v1', 'address': 'cog-command:turn'},
        'qualification': {'level': 'declaration', 'identity_verified': False, 'revision_pinned': False,
            'evidence': [{'check': 'adapter-controls', 'observation': 'CLI/version and subscription login inspected; no model-quality or immutable-weights claim.' if composition == 'model+harness' else 'Single-turn JSON interaction with no tools or memory; external model admission required.'}]},
        'admission': None}
    validate(binding, 'binding')
    return {'document_kind': 'bind_result', 'contract': CONTRACT, 'request_id': request['request_id'],
            'status': 'candidate', 'binding': binding, 'problems': []}


def reference_lookup(schema):
    """The resolver the validator will actually use for this schema. A second
    implementation of JSON Pointer is a second opinion: one that accepts what
    the real resolver rejects spends a turn and fails afterwards, and one that
    rejects what it accepts refuses a legal schema."""
    validator = Draft202012Validator(schema)
    resolver = getattr(validator, '_resolver', None)      # jsonschema >= 4.18
    if resolver is not None and hasattr(resolver, 'lookup'):
        return resolver.lookup
    legacy = getattr(validator, 'resolver', None)         # pragma: no cover
    return legacy.resolve if legacy is not None and hasattr(legacy, 'resolve') else None


def references_resolve(schema):
    """Every `$ref` in the output schema must be a pointer that resolves inside
    this document, CHECKED WITH THAT RESOLVER. A remote reference is
    unsupported; an unresolved local one would otherwise survive schema
    checking, spend a turn, and only fail when the result is validated."""
    try:
        lookup = reference_lookup(schema)
    except (ValueError, TypeError, AttributeError, KeyError) + REFERENCE_ERRORS:
        lookup = None

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ('$ref', '$dynamicRef'):
                    require(isinstance(value, str) and value.startswith('#'), 'Remote schema references are unsupported.')
                    if lookup is None:  # pragma: no cover - no resolver to ask
                        require(False, 'Output schema references cannot be checked; send a schema without $ref.')
                    try:
                        lookup(value)
                    except REFERENCE_ERRORS:
                        raise ValueError(f'Output schema reference {value} does not resolve inside the schema.') from None
                    except (ValueError, TypeError, AttributeError, KeyError):
                        raise ValueError(f'Output schema reference {value} could not be resolved.') from None
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
    walk(schema)


def check_turn(request, binding, model_binding=None):
    validate(binding, 'binding')
    require(binding['state'] == 'admitted' and binding['provider'] == IDENTITY, 'An admitted binding for this provider is required.')
    # Null model reference is used only for an inseparable Model+Harness turn.
    validate(request, 'harness_turn_request')
    ref = {'binding_id': binding['binding_id'], 'revision': binding['revision']}
    require(request['binding'] == ref and request['model_binding'] == binding['model_binding'], 'Turn binding references do not match.')
    require(not request['tool_grant_refs'] and request['thread_ref'] is None, 'This adapter accepts no external tools or remembered thread.')
    require(set(request['task']) == {'input', 'output_schema'}, 'Turn task requires input and output_schema only.')
    require(isinstance(request['task']['input'], str), 'Rendered task input must be text.')
    output_schema = request['task']['output_schema']
    require(isinstance(output_schema, dict), 'Object output schema required.')
    references_resolve(output_schema)
    try:
        Draft202012Validator.check_schema(output_schema)
    except SchemaError as exc:
        raise ValueError('Output schema is not a valid JSON Schema: ' + str(exc).split('\n')[0]) from None
    require(output_schema.get('type') == 'object', 'Object output schema required.')
    if binding['composition'] == 'harness':
        validate(model_binding, 'binding')
        require(model_binding['state'] == 'admitted' and model_binding['composition'] == 'model', 'Separate model must be admitted and model-only.')
        require({'binding_id': model_binding['binding_id'], 'revision': model_binding['revision']} == binding['model_binding'], 'Separate model reference mismatch.')
        require(model_binding['capability'] == 'model-endpoint/openai-compatible' and
                {'text-generation', 'json-output'} <= set(model_binding['features']), 'Incompatible model capabilities.')
    return output_schema


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def infer_model(request, model_binding, timeout=None):
    from urllib.parse import urlsplit
    address = model_binding['invocation']['address']
    parsed = urlsplit(address)
    require(parsed.scheme == 'http' and parsed.hostname == '127.0.0.1' and parsed.port and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment,
            'Reference harness accepts only a host-admitted loopback model gateway.')
    require(model_binding['invocation']['protocol'] == 'openai-chat-completions-v1', 'Unsupported model protocol.')
    reference = model_binding['credential_refs'].get('gateway_token')
    require(reference == 'env:OPENROUTER_COG_TOKEN', 'Reference harness requires the OpenRouter gateway credential reference.')
    token = os.environ.get('OPENROUTER_COG_TOKEN')
    require(bool(token) and '\n' not in token and '\r' not in token, 'Model gateway credential is unavailable.')
    schema = request['task']['output_schema']
    messages = [{'role': 'system', 'content': '\n\n'.join(x['content'] for x in request['context'])},
                {'role': 'user', 'content': request['task']['input']}]
    body = {'model': model_binding['model']['id'], 'messages': messages, 'response_format': {'type': 'json_object'}}
    req = urllib.request.Request(address.rstrip('/') + '/chat/completions', json.dumps(body).encode(),
                                 {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token})
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=MODEL_SECONDS if timeout is None else timeout) as response:
            raw = response.read(MAX_BYTES + 1)
        require(len(raw) <= MAX_BYTES, 'Model response exceeded limit.')
        data = json.loads(raw)
        require(data['model'] == model_binding['model']['id'], 'Model response identity mismatch.')
        message = data['choices'][0]['message']
        require(not message.get('tool_calls'), 'Unexpected model tool call.')
        result = json.loads(message['content'])
    except (OSError, KeyError, IndexError, TypeError, json.JSONDecodeError):
        raise ValueError('Model gateway failed or returned malformed output.') from None
    validate(result, schema=schema)
    facts = data.get('cog_binding')
    require(isinstance(facts, dict) and facts.get('binding_id') == model_binding['binding_id'] and facts.get('revision') == model_binding['revision'] and facts.get('outcome') == 'completed' and not facts.get('deviations'), 'Missing or mismatched model gateway provenance.')
    return result, {'gateway': facts}


def infer_vendor(request, binding, timeout=None):
    engine = ENGINE['engine']
    # `timeout` is the WHOLE remaining budget for this turn, readiness included.
    timeout = VENDOR_SECONDS if timeout is None else timeout
    deadline = time.monotonic() + timeout
    status = doctor(deadline)
    require(status['version'] == binding['harness']['version'], 'Vendor harness version changed; rebind before invoking.')
    prompt = '\n\n'.join(x['content'] for x in request['context']) + '\n\nTASK DATA:\n' + request['task']['input']
    schema = request['task']['output_schema']
    observations = {'requested_model': binding['model']['id'], 'harness_version': status['version'], 'identity_verified': False, 'observed_model': None}
    # Open-ended JSON Schema documents (as produced by author/designer Cogs)
    # cannot be represented by vendor strict structured-output subsets.
    # Request JSON in the prompt for these, then enforce the full local schema.
    strict = native_schema(schema)
    if not strict:
        prompt += '\n\nReturn only a JSON object conforming to this output schema:\n' + json.dumps(schema)
    # Vendor strict-output parsers reject JSON Schema meta keys ('$schema', '$id')
    # that authored Cogs legitimately carry; hand the vendor a bare copy. The
    # full schema, meta keys included, still validates the result below.
    vendor_schema = {k: v for k, v in schema.items() if k not in STRIPPED_KEYS}
    with tempfile.TemporaryDirectory(prefix='cog-turn-') as directory:
        timeout = remaining_budget(deadline, 'the vendor command')
        directory = Path(directory)
        schema_file = directory / 'output-schema.json'
        schema_file.write_text(json.dumps(vendor_schema))
        if engine == 'codex':
            result_file = directory / 'result.json'
            argv = [vendor_executable(), 'exec', '--ignore-user-config', '--ignore-rules', '--ephemeral', '--skip-git-repo-check',
                    '--sandbox', 'read-only', '-c', 'approval_policy="never"', '-c', 'web_search="disabled"',
                    '-c', 'forced_login_method="chatgpt"', '--model', binding['model']['id'],
                    '--output-last-message', str(result_file), '--json']
            if strict:
                argv += ['--output-schema', str(schema_file)]
            for feature in ('shell_tool', 'apps', 'plugins', 'hooks', 'browser_use', 'computer_use', 'image_generation', 'multi_agent', 'memories', 'skill_search'):
                argv += ['--disable', feature]
            argv += ['-']
            events = command(argv, prompt, str(directory), timeout=timeout)
            for line in events.splitlines():
                event = json.loads(line)
                require(event.get('type') != 'turn.failed', 'Vendor turn failed.')
                item = event.get('item', {})
                require(item.get('type') not in ('command_execution', 'mcp_tool_call', 'web_search', 'file_change'), 'Unexpected vendor tool activity; turn rejected.')
            require(result_file.is_file() and result_file.stat().st_size <= MAX_BYTES, 'Vendor final result is missing or too large.')
            result = json.loads(result_file.read_text())
        else:
            argv = [vendor_executable(), '-p', '--safe-mode', '--setting-sources', '', '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
                    '--tools', '', '--disallowedTools', 'mcp__*', '--permission-mode', 'dontAsk',
                    '--no-session-persistence', '--output-format', 'json',
                    '--model', binding['model']['id']]
            if strict:
                argv += ['--json-schema', json.dumps(vendor_schema)]
            data = json.loads(command(argv, prompt, str(directory), timeout=timeout))
            require(not data.get('is_error') and data.get('subtype') == 'success', 'Claude did not finish successfully.')
            require(not data.get('permission_denials'), 'Claude requested unsupported permissions.')
            observations['reported_model_usage'] = data.get('modelUsage', {})
            result = data.get('structured_output')
            if result is None:
                result = parse_result_text(data.get('result'))
        validate(result, schema=schema)
        return result, observations


def parse_result_text(text):
    """Model reply text -> JSON object. Tolerates a code fence or prose around
    one object; anything else fails with an excerpt so the cause is visible."""
    require(isinstance(text, str) and text.strip(), 'Vendor returned an empty result.')
    body = text.strip()
    if body.startswith('```'):
        body = body.split('\n', 1)[1] if '\n' in body else ''
        body = body.rsplit('```', 1)[0]
    try:
        return json.loads(body)
    except ValueError:
        pass
    start, end = body.find('{'), body.rfind('}')
    if start != -1 and end > start:
        try:
            return json.loads(body[start:end + 1])
        except ValueError:
            pass
    raise ValueError('Vendor result was not a JSON object; it begins: ' + body[:200].replace('\n', ' '))


NATIVE_TYPES = {'object', 'array', 'string', 'integer', 'number', 'boolean'}
NATIVE_COMMON = {'type', 'description', 'title'}
# Keys that belong to each declared type, and to no other. `items` on an object
# or `properties` on a string is legal JSON Schema and inert, but a node
# carrying it is not the shape this subset describes.
NATIVE_FOR_TYPE = {'object': {'properties', 'required', 'additionalProperties'},
                   'array': {'items'},
                   'string': {'enum'}, 'integer': {'enum'},
                   'number': {'enum'}, 'boolean': set()}
NATIVE_KEYS = NATIVE_COMMON | set().union(*NATIVE_FOR_TYPE.values())
STRIPPED_KEYS = ('$schema', '$id')


def native_schema(schema, top=True):
    """The vendor strict structured-output subset, named exhaustively: a closed
    tree of single-typed nodes built from the keys above and nothing else.
    A `type` LIST, a nested meta key, a `$ref`, a combinator, any keyword not
    enumerated here, and any keyword that does not belong to THIS node's
    declared type select the prompt-based path instead — an honest prompt beats
    a schema the vendor may reject or silently narrow. Every subschema-bearing
    keyword the subset allows is visited; nothing rides along unexamined. Local
    validation against the full schema stays normative on both paths."""
    if not isinstance(schema, dict):
        return False
    kind = schema.get('type')
    if not isinstance(kind, str) or kind not in NATIVE_TYPES:  # a type LIST included
        return False
    allowed = NATIVE_COMMON | NATIVE_FOR_TYPE[kind] | (set(STRIPPED_KEYS) if top else set())
    if set(schema) - allowed:
        return False
    enum = schema.get('enum')
    if 'enum' in schema and not (isinstance(enum, list) and enum
                                 and all(isinstance(x, (str, int, float, bool)) for x in enum)):
        return False
    if kind == 'object':
        properties = schema.get('properties')
        return (isinstance(properties, dict) and bool(properties)
                and schema.get('additionalProperties') is False
                and set(schema.get('required') or []) == set(properties)
                and all(native_schema(value, False) for value in properties.values()))
    if kind == 'array':
        return 'items' in schema and native_schema(schema['items'], False)
    return True


def turn(request, binding, model_binding=None, timeout=None):
    """`timeout` is what remains of ONE end-to-end budget for this request:
    readiness commands and the vendor command both draw on it, and neither
    starts once it is gone."""
    check_turn(request, binding, model_binding)
    result, observations = (infer_model(request, model_binding, timeout) if ENGINE['engine'] == 'openai-compatible'
                            else infer_vendor(request, binding, timeout))
    payload = {'document_kind': 'harness_turn_result', 'contract': CONTRACT, 'request_id': request['request_id'],
               'binding': request['binding'], 'model_binding': request['model_binding'], 'result': result, 'tool_uses': []}
    validate(payload, 'harness_turn_result')
    response = envelope('turn', payload, binding=binding)
    response['provider_observations'] = observations
    return response


def read(path):
    p = Path(path)
    require(p.stat().st_size <= MAX_BYTES, 'Input document exceeds limit.')
    return json.loads(p.read_text())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['card', 'check', 'bind', 'turn', 'validate'])
    parser.add_argument('--request')
    parser.add_argument('--binding')
    parser.add_argument('--model-binding')
    parser.add_argument('--definition')
    args = parser.parse_args()
    try:
        if args.operation == 'card':
            result = envelope('card', CARD)
        elif args.operation == 'check':
            result = envelope('check', doctor())
        elif args.operation == 'validate':
            validate(read(args.request), args.definition)
            result = envelope('validate', {'valid': True})
        elif args.operation == 'bind':
            result = envelope('bind', candidate(read(args.request)))
        else:
            result = turn(read(args.request), read(args.binding), read(args.model_binding) if args.model_binding else None)
    except (ValueError, OSError, TypeError, KeyError) as exc:
        result = envelope(args.operation, error=str(exc) if isinstance(exc, ValueError) else 'Invalid or unavailable local document.')
    print(json.dumps(result))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
