"""Loopback OpenAI-compatible shim in front of one admitted turn binding.

Byte-identical in cog-claude, cog-chatgpt and cog-turn-harness; engine.json and
the provider declaration supply the identity, the short name and the default
port. One chat completion becomes exactly one turn performed in this process:
no tools, no thread, no memory, one at a time. The gateway never fabricates a
completion — a failed turn is a 502 carrying the provider's own error code.

It is not a route to anybody else's subscription: it binds 127.0.0.1 only and
performs the turn with the vendor login of the user who started it.
"""
import argparse
import hmac
import json
import os
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import turn_runtime as rt

LOOPBACK = '127.0.0.1'
# Byte-identical file: the engine declaration picks the package's port.
DEFAULT_PORTS = {'claude': 8121, 'codex': 8122, 'openai-compatible': 8123}
SHORT_NAME = rt.IDENTITY['id'].rsplit('/', 1)[-1]
SHORT_NAME = SHORT_NAME[4:] if SHORT_NAME.startswith('cog-') else SHORT_NAME
CONSUMER = {'id': 'openteams/turn-gateway', 'version': '0.1.0'}
MAX_BODY = 2 * 1024 * 1024
# Accepted and ignored, as the docs say; a turn has no sampling controls.
IGNORED_KEYS = {'temperature', 'max_tokens', 'max_completion_tokens'}
REFUSED_KEYS = {'stream', 'n', 'tools', 'tool_choice', 'functions', 'function_call'}
SUPPORTED = ('A turn is one non-streaming request returning one JSON object: an '
             'optional system message followed by exactly one user message, '
             'response_format json_schema with an object schema, n = 1, no tools, '
             'no assistant history and no remembered thread.')


class GatewayError(Exception):
    """An OpenAI-shaped error with the HTTP status the caller should see."""

    def __init__(self, status, code, message, kind='invalid_request_error'):
        super().__init__(message)
        self.status, self.code, self.message, self.kind = status, code, message, kind

    def body(self):
        return {'error': {'message': self.message, 'type': self.kind,
                          'code': self.code, 'param': None}}


def refuse(condition, message):
    """A start-time refusal: named, fail-closed, never a warning."""
    if not condition:
        raise ValueError(message)


def bad_request(condition, message, code='unsupported_request'):
    if not condition:
        raise GatewayError(400, code, message + ' ' + SUPPORTED)


def admitted_binding(path, label='--binding'):
    """An ADMITTED binding document: a bare document, or a workbench suite
    entry carrying one under `binding`. Anything else is refused by name."""
    p = Path(path)
    refuse(p.is_file(), f'{label} {path}: no such file. Pass an admitted binding '
                        f'document or a workbench suite entry from cog-workbench/var/suite/.')
    refuse(p.stat().st_size <= rt.MAX_BYTES, f'{label} {path}: document exceeds the supported size.')
    refuse(not Path(str(p) + '.revoked').exists() and not p.with_suffix('.revoked').exists(),
           f'{label} {path}: the workbench marked this binding revoked.')
    try:
        document = json.loads(p.read_text())
    except ValueError:
        raise ValueError(f'{label} {path}: not JSON.') from None
    if isinstance(document, dict) and isinstance(document.get('binding'), dict):
        document = document['binding']  # workbench suite entry
    refuse(isinstance(document, dict) and document.get('document_kind') == 'binding',
           f'{label} {path}: not a binding document, and not a workbench suite '
           f'entry carrying one under "binding".')
    try:
        rt.validate(document, 'binding')
    except ValueError as exc:
        raise ValueError(f'{label} {path}: {exc}') from None
    refuse(document['state'] == 'admitted',
           f'{label} {path}: binding state is {document["state"]!r}. The gateway '
           f'serves an ADMITTED binding only — admit it in the workbench host first.')
    return document


def prepare(binding_path, model_binding_path=None):
    """Read the documents once, at start (working rule 6)."""
    binding = admitted_binding(binding_path)
    refuse(binding['provider'] == rt.IDENTITY,
           f'--binding {binding_path}: binding belongs to provider '
           f'{binding["provider"].get("id")!r}, not {rt.IDENTITY["id"]!r}.')
    reference = binding['model_binding']
    model_binding = None
    if reference is None:
        refuse(model_binding_path is None,
               '--model-binding was passed, but this binding is an inseparable '
               'Model+Harness binding that references no separate model.')
    else:
        refuse(model_binding_path is not None,
               f'--model-binding is required: this binding references model '
               f'{reference["binding_id"]} revision {reference["revision"]}.')
        model_binding = admitted_binding(model_binding_path, '--model-binding')
        refuse({'binding_id': model_binding['binding_id'],
                'revision': model_binding['revision']} == reference,
               f'--model-binding {model_binding_path}: this is '
               f'{model_binding["binding_id"]} revision {model_binding["revision"]}, '
               f'but the binding references {reference["binding_id"]} revision '
               f'{reference["revision"]}.')
    return binding, model_binding


def served_model(binding):
    return f'{SHORT_NAME}/{binding["binding_id"]}@{binding["revision"]}'


def rendered_input(system, user):
    parts = ([f'SYSTEM:\n{system}'] if system else []) + [f'USER:\n{user}']
    return '\n\n'.join(parts)


def translate(body, binding):
    """Chat-completions body -> harness_turn_request. Everything a turn cannot
    do is a 400 that names what it supports."""
    bad_request(isinstance(body, dict), 'Request body must be a JSON object.')
    unknown = set(body) - IGNORED_KEYS - REFUSED_KEYS - {'model', 'messages', 'response_format'}
    bad_request(not unknown, f'Unsupported request field(s): {", ".join(sorted(unknown))}.')
    bad_request(body.get('model', served_model(binding)) == served_model(binding),
                f'This gateway serves exactly one model id, '
                f'{served_model(binding)!r}; it cannot route to {body.get("model")!r}.',
                'model_not_found')
    bad_request(not body.get('stream'), 'Streaming is not supported.')
    bad_request(body.get('n', 1) == 1, 'Only one choice per request is supported.')
    for key in ('tools', 'tool_choice', 'functions', 'function_call'):
        bad_request(not body.get(key), f'{key} is not supported: this Cog accepts no tool grants.')

    messages = body.get('messages')
    bad_request(isinstance(messages, list) and messages, 'messages must be a non-empty array.')
    system, user = None, None
    for index, message in enumerate(messages):
        bad_request(isinstance(message, dict) and isinstance(message.get('content'), str)
                    and message['content'].strip(),
                    f'messages[{index}] must be an object with non-empty text content.')
        role = message.get('role')
        bad_request(role in ('system', 'user'),
                    f'messages[{index}] has role {role!r}; a turn carries no assistant history.')
        if role == 'system':
            bad_request(system is None and user is None,
                        f'messages[{index}]: one system message, first, at most.')
            system = message['content']
        else:
            bad_request(user is None, f'messages[{index}]: one user message at most.')
            user = message['content']
    bad_request(user is not None, 'A user message is required.')

    fmt = body.get('response_format')
    bad_request(isinstance(fmt, dict), 'response_format is required.')
    bad_request(fmt.get('type') == 'json_schema',
                f'response_format {fmt.get("type")!r} is not supported; a turn '
                f'needs the object schema its result is validated against.')
    schema = (fmt.get('json_schema') or {}).get('schema')
    bad_request(isinstance(schema, dict), 'response_format.json_schema.schema must be an object schema.')

    return {'document_kind': 'harness_turn_request', 'contract': rt.CONTRACT,
            'request_id': str(uuid.uuid4()),
            'binding': {'binding_id': binding['binding_id'], 'revision': binding['revision']},
            'model_binding': binding['model_binding'], 'consumer': dict(CONSUMER),
            'context': [], 'task': {'input': rendered_input(system, user), 'output_schema': schema},
            'tool_grant_refs': [], 'thread_ref': None}


def complete(body, binding, model_binding):
    """One chat completion = one turn. Request faults are 400; turn faults are
    502 with the provider's own error code, never a fabricated completion."""
    request = translate(body, binding)
    try:
        rt.check_turn(request, binding, model_binding)
    except (ValueError, TypeError, KeyError) as exc:
        raise GatewayError(400, 'unsupported_request', f'{exc} ' + SUPPORTED) from None
    try:
        response = rt.turn(request, binding, model_binding)
    except (ValueError, OSError, TypeError, KeyError) as exc:
        detail = str(exc) if isinstance(exc, ValueError) else 'Invalid or unavailable local document.'
        response = rt.envelope('turn', error=detail)
    if not response['ok']:
        error = response['error'] or {'code': 'turn-provider', 'detail': 'Turn failed.'}
        raise GatewayError(502, error['code'], error['detail'], 'upstream_error')
    payload = response['payload']
    return {'id': 'chatcmpl-' + uuid.uuid4().hex, 'object': 'chat.completion',
            'created': int(time.time()), 'model': served_model(binding),
            'choices': [{'index': 0, 'finish_reason': 'stop',
                         'message': {'role': 'assistant',
                                     'content': json.dumps(payload['result'])}}],
            'x_cog': {'provider': rt.IDENTITY, 'binding': request['binding'],
                      'model_binding': request['model_binding'],
                      'request_id': request['request_id'],
                      'provider_observations': response.get('provider_observations'),
                      'model_identity_verified': False,
                      'evidence_scope': 'composed-system'}}


def make_server(binding, model_binding=None, port=None, host=LOOPBACK):
    """A loopback server for one binding. The bearer token, like the binding,
    is read once here."""
    refuse(host == LOOPBACK, f'--host {host}: the turn gateway binds {LOOPBACK} only. '
                             f'It is the owner\'s own vendor login, never a route for '
                             f'anyone else\'s subscription.')
    model = served_model(binding)
    token = os.environ.get('COG_TURN_GATEWAY_TOKEN') or None
    one_turn = threading.Lock()

    class Requests(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # No paths, bodies or completion text in access logs.

        def respond(self, status, value):
            data = json.dumps(value, allow_nan=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def authorize(self):
            if token is not None and not hmac.compare_digest(
                    self.headers.get('Authorization', ''), 'Bearer ' + token):
                raise GatewayError(401, 'invalid_api_key',
                                   'COG_TURN_GATEWAY_TOKEN is set; a matching bearer token is required.',
                                   'authentication_error')

        def fail(self, exc):
            self.respond(exc.status, exc.body())

        def do_GET(self):
            try:
                self.authorize()
                if self.path.rstrip('/') == '/health':
                    return self.respond(200, {'status': 'ok', 'model': model,
                                              'note': 'Liveness only. A turn is attempted '
                                                      'when a completion is requested.'})
                if self.path.rstrip('/') == '/v1/models':
                    return self.respond(200, {'object': 'list', 'data': [
                        {'id': model, 'object': 'model', 'created': 0,
                         'owned_by': rt.IDENTITY['id']}]})
                raise GatewayError(404, 'not_found', f'Unknown endpoint {self.path!r}; this '
                                                     f'gateway serves /health, /v1/models and '
                                                     f'/v1/chat/completions.')
            except GatewayError as exc:
                self.fail(exc)
            except (OSError, ValueError, TypeError):
                self.fail(GatewayError(503, 'gateway_unavailable', 'Gateway unavailable.',
                                       'api_error'))

        def do_POST(self):
            try:
                self.authorize()
                if self.path.rstrip('/') != '/v1/chat/completions':
                    raise GatewayError(404, 'not_found', f'Unknown endpoint {self.path!r}; '
                                                         f'completions are at /v1/chat/completions.')
                if self.headers.get('Transfer-Encoding'):
                    raise GatewayError(400, 'unsupported_request', 'Chunked bodies are unsupported.')
                length = int(self.headers.get('Content-Length') or 0)
                if not 0 < length <= MAX_BODY:
                    raise GatewayError(413, 'unsupported_request', 'Invalid request size.')
                self.connection.settimeout(30)
                raw = self.rfile.read(length)
                self.connection.settimeout(None)  # A turn takes tens of seconds.
                try:
                    body = json.loads(raw)
                except ValueError:
                    raise GatewayError(400, 'unsupported_request', 'Request body is not JSON.') from None
                with one_turn:  # One turn at a time; a second request waits.
                    result = complete(body, binding, model_binding)
                self.respond(200, result)
            except GatewayError as exc:
                self.fail(exc)
            except (OSError, ValueError, TypeError):
                self.fail(GatewayError(503, 'gateway_unavailable', 'Gateway unavailable.',
                                       'api_error'))

    return ThreadingHTTPServer((host, port if port is not None else
                                DEFAULT_PORTS[rt.ENGINE['engine']]), Requests)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Serve one admitted turn binding '
                                                 'as a loopback OpenAI-compatible endpoint.')
    parser.add_argument('--binding', required=True, help='an ADMITTED binding document '
                                                         'or a workbench suite entry')
    parser.add_argument('--model-binding')
    parser.add_argument('--port', type=int, default=DEFAULT_PORTS[rt.ENGINE['engine']])
    parser.add_argument('--host', default=LOOPBACK)
    args = parser.parse_args(argv)
    try:
        binding, model_binding = prepare(args.binding, args.model_binding)
        server = make_server(binding, model_binding, args.port, args.host)
    except ValueError as exc:
        print('turn gateway refuses to start: ' + str(exc), file=sys.stderr)
        return 2
    print(f'turn gateway on http://{LOOPBACK}:{server.server_port}/v1 serving '
          f'{served_model(binding)} (one turn at a time; loopback only; '
          f'restart after any rebind)', file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
