"""Loopback OpenAI-compatible shim in front of one admitted turn binding.

Byte-identical in cog-claude, cog-chatgpt and cog-turn-harness; engine.json and
the provider declaration supply the identity, the short name and the default
port. One chat completion becomes exactly one turn performed in this process:
no tools, no thread, no memory, one at a time. The gateway never fabricates a
completion — a failed turn is a 502 carrying the provider's own error code.

It is not a route to anybody else's subscription. It binds 127.0.0.1, it
performs the turn with the vendor login of the user who started it, and — since
a loopback port that spends a subscription is reachable from every page in this
machine's browser — it refuses to start without a bearer token and refuses
browser-shaped requests (a foreign Host, any Origin, a non-JSON body) before it
looks at anything else.
"""
import argparse
import hmac
import io
import json
import os
import select
import socket
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
CONSUMER = {'id': 'openteams/turn-gateway', 'version': '0.2.0'}
MAX_BODY = 2 * 1024 * 1024
MAX_CONNECTIONS = 8
HEADER_SECONDS = 10      # a TOTAL header deadline, not a per-packet one
BODY_SECONDS = 10        # a TOTAL upload deadline, not a per-packet one
WRITE_SECONDS = 30       # a TOTAL deadline for writing one response
TURN_SECONDS = 170       # the END-TO-END budget, under the caller's 180 s
QUEUE_SECONDS = 60       # the longest a second request waits for the turn lock
MIN_INFERENCE_SECONDS = 30   # below this, 503 busy rather than a doomed turn
TOKEN_VARIABLE = 'COG_TURN_GATEWAY_TOKEN'
TOKEN_LENGTH = 32
CONTEXT_ID = 'system'
# The exact body `cog_core.health(deep=True)` sends, and nothing else.
LIVENESS_MESSAGES = [{'role': 'user', 'content': 'ping'}]
LIVENESS_KEYS = {'model', 'messages', 'max_tokens'}
# An interim response an HTTP/1.1 client must skip: the gateway's way of asking
# a socket whether anybody is still there (see Requests.client_gone). HTTP
# forbids sending 1xx to an HTTP/1.0 client, so that caller is never probed.
INTERIM = b'HTTP/1.1 100 Continue\r\n\r\n'
PROBE_VERSION = 'HTTP/1.1'
PROBE_PAUSE = 0.05       # long enough on loopback for a reset to USUALLY come back
# Inherited HTTP failures, answered as JSON error objects like every other one.
INHERITED_CODES = {400: 'unsupported_request', 404: 'not_found', 408: 'request_timeout',
                   411: 'unsupported_request', 414: 'request_too_large',
                   431: 'request_too_large', 501: 'unsupported_method',
                   505: 'unsupported_request'}
# Accepted and ignored, as the docs say; a turn has no sampling controls.
IGNORED_KEYS = {'temperature', 'max_tokens', 'max_completion_tokens'}
REFUSED_KEYS = {'stream', 'n', 'tools', 'tool_choice', 'functions', 'function_call'}
SUPPORTED = ('A turn is one non-streaming request returning one JSON object: an '
             'optional system message followed by exactly one user message, '
             'response_format json_schema with an object schema, n = 1, no tools, '
             'no assistant history and no remembered thread.')


def note(message):
    """Operator log. No paths, bodies or completion text — request ids only."""
    print('turn gateway: ' + message, file=sys.stderr, flush=True)


class Clocked(io.RawIOBase):
    """A socket reader with TOTAL deadlines. The socket timeout is reset to
    whatever remains of the current deadline before EVERY receive, so a client
    that drips one byte every few seconds is cut off on time whatever its
    rhythm — an inactivity timeout never expires for such a client, and a
    buffered `read()` can perform many receives without the caller looking at a
    clock in between. Wrapped in a BufferedReader so the request line, the
    header block and the body are all read through it."""

    def __init__(self, connection, seconds):
        self.connection = connection
        self.start(seconds)

    def start(self, seconds):
        self.deadline = time.monotonic() + seconds

    def readable(self):
        return True

    def readinto(self, buffer):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('read deadline elapsed')
        self.connection.settimeout(remaining)
        return self.connection.recv_into(buffer)


class Deadlined(io.RawIOBase):
    """A response writer under ONE deadline for the WHOLE response.

    Headers, any interim responses and the body draw on the same window: the
    remainder is re-armed on the socket before every send, and a long body is
    sent in pieces so that the aggregate — not each write separately — is what
    the deadline bounds. Giving each write its own full allowance (which is
    what a plain `settimeout` per write does) bounds no total at all: a reader
    that takes just under the limit for every piece holds the slot for as long
    as there are pieces.
    """

    def __init__(self, connection, clock=time.monotonic):
        self.connection, self.clock, self.deadline = connection, clock, None

    def writable(self):
        return True

    def disarm(self):
        self.deadline = None

    def arm(self, seconds):
        """Start the window if it is not already running. The first write of a
        response — an interim probe included — is what starts it."""
        if self.deadline is None:
            self.deadline = self.clock() + seconds
        return self.deadline

    def write(self, data):
        view = memoryview(data)
        written = len(view)
        while view:
            remaining = None if self.deadline is None else self.deadline - self.clock()
            if remaining is not None and remaining <= 0:
                raise TimeoutError('write deadline elapsed')
            self.connection.settimeout(remaining)
            view = view[self.connection.send(view):]
        return written


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
    """Read the documents once, at start (working rule 6), and prove here that
    the pair can actually serve a turn: a gateway that would answer 400 to every
    completion because of its own configuration must refuse to start instead."""
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
        refuse(model_binding['composition'] == 'model',
               f'--model-binding {model_binding_path}: composition is '
               f'{model_binding["composition"]!r}; a separately bound model must be '
               f'model-only, or no turn this gateway accepts can be performed.')
        refuse(model_binding['capability'] == 'model-endpoint/openai-compatible',
               f'--model-binding {model_binding_path}: capability is '
               f'{model_binding["capability"]!r}, not '
               f'model-endpoint/openai-compatible.')
        missing = {'text-generation', 'json-output'} - set(model_binding['features'])
        refuse(not missing,
               f'--model-binding {model_binding_path}: the admitted model does not '
               f'declare {", ".join(sorted(missing))}; a turn needs both.')
    return binding, model_binding


def served_model(binding):
    return f'{SHORT_NAME}/{binding["binding_id"]}@{binding["revision"]}'


def translate(body, binding):
    """Chat-completions body -> harness_turn_request. Everything a turn cannot
    do is a 400 that names what it supports. System message contents become the
    turn's `context` and the user message alone is `task.input`, so the provider
    renders the workbench host's prompt shape: the instructions, then
    `TASK DATA:`, then the data. Nothing reaches a vendor until this returns."""
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
    declaration = fmt.get('json_schema')
    bad_request(isinstance(declaration, dict),
                'response_format.json_schema must be an object carrying the schema.')
    schema = declaration.get('schema')
    bad_request(isinstance(schema, dict), 'response_format.json_schema.schema must be an object schema.')

    return {'document_kind': 'harness_turn_request', 'contract': rt.CONTRACT,
            'request_id': str(uuid.uuid4()),
            'binding': {'binding_id': binding['binding_id'], 'revision': binding['revision']},
            'model_binding': binding['model_binding'], 'consumer': dict(CONSUMER),
            'context': [{'id': CONTEXT_ID, 'content': system}] if system else [],
            'task': {'input': user, 'output_schema': schema},
            'tool_grant_refs': [], 'thread_ref': None}


def validated(body, binding, model_binding):
    """Translate and run every contract check BEFORE a turn lock is taken, so
    nothing reaches the vendor — or waits in a queue — that cannot be served."""
    request = translate(body, binding)
    try:
        rt.check_turn(request, binding, model_binding)
    except (ValueError, TypeError, KeyError, AttributeError, IndexError) + rt.REFERENCE_ERRORS as exc:
        detail = str(exc) if isinstance(exc, ValueError) else 'Request could not be checked against the turn contract.'
        raise GatewayError(400, 'unsupported_request', f'{detail} ' + SUPPORTED) from None
    return request


def is_liveness_probe(body, binding):
    """ONE request body is answered without a turn: the exact one
    `cog_core.health(deep=True)` sends — no `response_format`, `max_tokens` 1,
    and `messages` exactly one user message reading `ping`. That is a liveness
    question, not a turn. Anything else without a `response_format` is a 400:
    a completion request must never be able to collect a fabricated
    judgment-shaped `pong` by asking for one token."""
    return (isinstance(body, dict) and 'response_format' not in body
            and not set(body) - LIVENESS_KEYS
            and body.get('messages') == LIVENESS_MESSAGES
            and isinstance(body.get('max_tokens'), int) and not isinstance(body.get('max_tokens'), bool)
            and body['max_tokens'] == 1
            and body.get('model', served_model(binding)) == served_model(binding))


def pong(binding):
    return {'id': 'chatcmpl-' + uuid.uuid4().hex, 'object': 'chat.completion',
            'created': int(time.time()), 'model': served_model(binding),
            'choices': [{'index': 0, 'finish_reason': 'stop',
                         'message': {'role': 'assistant', 'content': 'pong'}}],
            'x_cog': {'provider': rt.IDENTITY, 'liveness': True,
                      'note': 'max_tokens 1 with no response_format is answered as '
                              'liveness: no turn was performed and nothing was spent.',
                      'model_identity_verified': False, 'evidence_scope': 'composed-system'}}


def perform(request, binding, model_binding, deadline):
    """One validated request = one turn. Turn faults are 502 with the provider's
    own error code, never a fabricated completion.

    `deadline` is the request's ONE absolute monotonic instant, not a duration:
    it travels into the turn so that the revalidation and the readiness
    commands are charged to the same budget as the vendor command, and no stage
    restarts the clock from a duration captured earlier."""
    try:
        response = rt.turn(request, binding, model_binding, deadline=deadline)
    except (ValueError, OSError, TypeError, KeyError) + rt.REFERENCE_ERRORS as exc:
        if isinstance(exc, ValueError):
            detail = str(exc)
        elif rt.REFERENCE_ERRORS and isinstance(exc, rt.REFERENCE_ERRORS):
            detail = 'A schema reference in this request does not resolve; no result was accepted.'
        else:
            detail = 'Invalid or unavailable local document.'
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


class Gateway(ThreadingHTTPServer):
    """Bounded: a flood of half-open connections must not become a thread
    apiece. Over the limit is an immediate 503, not a queued socket."""
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, connections=None):
        self.slots = threading.Semaphore(connections or MAX_CONNECTIONS)
        super().__init__(address, handler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            body = json.dumps(GatewayError(503, 'busy', 'The gateway is at its connection '
                                           'limit; it serves one turn at a time.',
                                           'api_error').body()).encode()
            try:
                request.settimeout(0.5)
                request.recv(65536)  # drain, so closing is not a bare reset
            except OSError:
                pass
            try:
                request.sendall(b'HTTP/1.1 503 Service Unavailable\r\n'
                                b'Content-Type: application/json\r\nConnection: close\r\n'
                                b'Content-Length: ' + str(len(body)).encode() + b'\r\n\r\n' + body)
            except OSError:
                pass
            return self.shutdown_request(request)
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


def make_server(binding, model_binding=None, port=None, host=LOOPBACK,
                turn_timeout=None, queue_wait=None, connections=None,
                min_inference=None, header_seconds=None, body_seconds=None,
                write_seconds=None):
    """A loopback server for one binding. The bearer token, like the binding, is
    read once here — and it is mandatory: without it any page in a local browser
    could spend this subscription. Every deadline is a parameter so tests can
    use small ones instead of waiting out the real ones."""
    refuse(host == LOOPBACK, f'--host {host}: the turn gateway binds {LOOPBACK} only. '
                             f'It is the owner\'s own vendor login, never a route for '
                             f'anyone else\'s subscription.')
    token = (os.environ.get(TOKEN_VARIABLE) or '').strip()
    refuse(len(token) >= TOKEN_LENGTH,
           f'{TOKEN_VARIABLE} must be set to at least {TOKEN_LENGTH} characters before '
           f'this gateway will start. A loopback port that spends a subscription is '
           f'reachable from every page in this machine\'s browser, so every route — '
           f'/health included — requires the bearer token. Generate one with '
           f'`python -c "import secrets; print(secrets.token_urlsafe(32))"` and name it '
           f'to callers with api_key_env.')
    expected = ('Bearer ' + token).encode()
    model = served_model(binding)
    turn_timeout = TURN_SECONDS if turn_timeout is None else turn_timeout
    queue_wait = QUEUE_SECONDS if queue_wait is None else queue_wait
    min_inference = MIN_INFERENCE_SECONDS if min_inference is None else min_inference
    header_seconds = HEADER_SECONDS if header_seconds is None else header_seconds
    body_seconds = BODY_SECONDS if body_seconds is None else body_seconds
    write_seconds = WRITE_SECONDS if write_seconds is None else write_seconds
    one_turn = threading.Lock()

    class Requests(BaseHTTPRequestHandler):
        timeout = header_seconds   # the socket timeout Clocked then keeps arming
        answered = False
        budget = None

        def log_message(self, *args):
            pass  # No paths, bodies or completion text in access logs.

        def setup(self):
            super().setup()
            # Every read on this connection now runs against a total deadline,
            # and every write of one response against a single other one.
            self.clock = Clocked(self.connection, header_seconds)
            self.rfile = io.BufferedReader(self.clock)
            self.writes = Deadlined(self.connection)
            self.wfile = self.writes

        def handle_one_request(self):
            self.answered = False
            self.budget = None
            self.clock.start(header_seconds)
            self.writes.disarm()
            super().handle_one_request()

        def respond(self, status, value):
            """Writing is deadlined too: a connected client that stops reading
            would otherwise hold a slot for as long as it liked. The window is
            ONE per response — headers, interim responses and body together."""
            data = json.dumps(value, allow_nan=False).encode()
            self.answered = True  # set first: never a second response attempt
            self.writes.arm(write_seconds)
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def fail(self, exc):
            if self.answered:
                return
            try:
                self.respond(exc.status, exc.body())
            except OSError:
                self.close_connection = True

        def send_error(self, code, message=None, explain=None):
            """Inherited HTTP failures — an unsupported method, an over-long
            request line, a header block the parser rejects — answer with the
            same JSON error object as every other refusal. The one failure with
            no response at all is a client that never finishes its headers:
            there is no request to answer."""
            if self.answered:
                return
            self.close_connection = True
            detail = explain or message or self.responses.get(code, ('Request refused.',))[0]
            kind = 'api_error' if int(code) >= 500 else 'invalid_request_error'
            error = GatewayError(code, INHERITED_CODES.get(int(code), 'unsupported_request'),
                                 str(detail), kind)
            try:
                self.respond(code, error.body())
            except OSError:
                pass

        def guard(self):
            """Browser-shaped requests are refused before anything else. A
            simple cross-origin POST needs no preflight, and a rebound hostname
            still reaches loopback, so neither CORS nor the bind address is the
            authorization boundary — these three checks and the token are."""
            allowed = (f'{LOOPBACK}:{self.server.server_port}',
                       f'localhost:{self.server.server_port}')
            hosts = self.headers.get_all('Host') or []
            if len(hosts) != 1 or hosts[0].strip() not in allowed:
                raise GatewayError(403, 'forbidden_host',
                                   'Host must be exactly ' + ' or '.join(allowed) +
                                   '. This gateway answers local programs on loopback only.',
                                   'permission_error')
            if self.headers.get('Origin') is not None:
                raise GatewayError(403, 'forbidden_origin',
                                   'A browser origin is never a caller here: this gateway '
                                   'spends the owner\'s own subscription.', 'permission_error')
            credentials = self.headers.get_all('Authorization') or ['']
            if len(credentials) != 1:
                # Whichever one a proxy or a client meant, the pair is ambiguous
                # and must fail the same way in either order.
                raise GatewayError(400, 'unsupported_request',
                                   'Exactly one Authorization header is accepted; this request '
                                   'carried ' + str(len(credentials)) + '.')
            offered = credentials[0].encode('utf-8', 'replace')
            if not hmac.compare_digest(offered, expected):
                raise GatewayError(401, 'invalid_api_key',
                                   f'Every route requires the bearer token the gateway was '
                                   f'started with ({TOKEN_VARIABLE}).', 'authentication_error')

        def read_body(self):
            media = (self.headers.get('Content-Type') or '').split(';')[0].strip().lower()
            if media != 'application/json':
                raise GatewayError(415, 'unsupported_media_type',
                                   'POST requires Content-Type: application/json. A body a '
                                   'browser may send without a CORS preflight is refused.')
            if self.headers.get('Transfer-Encoding'):
                raise GatewayError(400, 'unsupported_request',
                                   'Chunked bodies are unsupported; send a Content-Length.')
            declared = [x.strip() for x in (self.headers.get_all('Content-Length') or [])]
            if len(declared) != 1 or not declared[0].isdigit():
                raise GatewayError(400, 'unsupported_request',
                                   'Exactly one numeric Content-Length header is required.')
            length = int(declared[0])
            if length == 0:
                raise GatewayError(400, 'unsupported_request', 'A request body is required.')
            if length > MAX_BODY:
                raise GatewayError(413, 'request_too_large',
                                   f'A request body of at most {MAX_BODY} bytes is supported.')
            raw = self.read_exactly(length)
            self.connection.settimeout(None)  # a turn takes tens of seconds
            try:
                return json.loads(raw)
            except ValueError:
                raise GatewayError(400, 'unsupported_request', 'Request body is not JSON.') from None

        def read_exactly(self, length):
            """A TOTAL upload deadline, not a per-packet one: the clock is armed
            before every receive, so a drip feed that never idles longer than a
            socket timeout is still a 408 at the deadline."""
            late = GatewayError(408, 'request_timeout',
                                f'The request body did not arrive within {body_seconds:g} seconds.',
                                'api_error')
            self.clock.start(body_seconds)
            chunks, read = [], 0
            while read < length:
                try:
                    chunk = self.rfile.read(min(length - read, 65536))
                except (TimeoutError, socket.timeout):
                    raise late from None
                if not chunk:
                    raise GatewayError(400, 'unsupported_request',
                                       'The request body ended before Content-Length bytes arrived.')
                chunks.append(chunk)
                read += len(chunk)
            return b''.join(chunks)

        def client_gone(self):
            """Has the caller probably gone? BEST EFFORT, and deliberately
            biased towards delivering: end of file on the REQUEST side does not
            say the reader left — a client may half-close its sending direction
            and go on reading, and this platform reports the same
            readable/hang-up condition for that as for a socket whose owner has
            left. The nearest honest question is whether a write still reaches
            somebody, so an HTTP/1.1 caller is asked with an interim response
            it must skip: a peer that has gone usually resets the first one and
            the second write fails.

            It is a HEURISTIC, not a proof. Sending is not delivery: two writes
            can both succeed and a reset arrive afterwards, so an abandoned
            request can still spend a turn. The 50 ms between them is an
            empirical loopback allowance, not a guarantee.

            An HTTP/1.0 caller is never probed: HTTP forbids 1xx to it, and a
            correct HTTP/1.0 client that half-closes would be handed something
            it has no rule for reading. It counts as present, which is the same
            direction this whole check errs in. A reader too slow to drain is
            still a reader — that is the write deadline's business, not this
            one's."""
            try:
                poller = select.poll()
                poller.register(self.connection, select.POLLIN)
                events = poller.poll(0)
                if not events:
                    return False                              # nothing pending
                if events[0][1] & (select.POLLERR | select.POLLNVAL):
                    return True
                if self.connection.recv(1, socket.MSG_PEEK):
                    return False                              # unread bytes, not EOF
                if self.request_version != PROBE_VERSION:
                    return False                              # no 1xx to an HTTP/1.0 client
                # The probe writes are part of this response's one window.
                self.writes.arm(write_seconds)
                self.wfile.write(INTERIM)
                time.sleep(PROBE_PAUSE)
                self.wfile.write(INTERIM)
                return False
            except (TimeoutError, socket.timeout):
                return False
            except OSError:
                return True

        def do_GET(self):
            self.answered = False
            try:
                self.guard()
                path = self.path.rstrip('/')
                if path == '/health':
                    return self.respond(200, {'status': 'ok', 'model': model,
                                              'note': 'Liveness only. A turn is attempted '
                                                      'when a completion is requested.'})
                if path == '/v1/models':
                    return self.respond(200, {'object': 'list', 'data': [
                        {'id': model, 'object': 'model', 'created': 0,
                         'owned_by': rt.IDENTITY['id']}]})
                raise GatewayError(404, 'not_found', f'Unknown endpoint {path!r}; this '
                                                     f'gateway serves /health, /v1/models and '
                                                     f'/v1/chat/completions.')
            except GatewayError as exc:
                self.fail(exc)
            except (OSError, ValueError, TypeError):
                self.fail(GatewayError(503, 'gateway_unavailable', 'Gateway unavailable.',
                                       'api_error'))

        def do_POST(self):
            self.answered = False
            # One monotonic budget per request, from the moment it is accepted:
            # the queue wait, the readiness commands and the vendor command all
            # draw on it. 170 s of inference after 60 s of queueing and 45 s of
            # readiness is a result nobody is still waiting for.
            self.budget = time.monotonic() + turn_timeout
            try:
                self.guard()
                if self.path.rstrip('/') != '/v1/chat/completions':
                    raise GatewayError(404, 'not_found', f'Unknown endpoint {self.path!r}; '
                                                         f'completions are at /v1/chat/completions.')
                body = self.read_body()
                if is_liveness_probe(body, binding):
                    return self.respond(200, pong(binding))
                request = validated(body, binding, model_binding)
                self.complete(request)
            except GatewayError as exc:
                self.fail(exc)
            except (OSError, ValueError, TypeError):
                self.fail(GatewayError(503, 'gateway_unavailable', 'Gateway unavailable.',
                                       'api_error'))

        def busy(self, detail):
            return GatewayError(503, 'busy', detail, 'api_error')

        def complete(self, request):
            """One turn at a time, and every wait drawn from the one budget: a
            turn is started only if enough of the request's own deadline is
            left to finish it, and a caller that has (probably) gone is not
            spent on. `self.budget` is an absolute monotonic instant and is
            passed on as one; `remaining` is computed at each point of use."""
            remaining = self.budget - time.monotonic()
            wait = min(queue_wait, remaining - min_inference)
            if wait <= 0:
                raise self.busy(f'The {turn_timeout:g} s end-to-end budget for this request has '
                                f'less than the {min_inference:g} s a turn needs left in it, so no '
                                f'turn was started. Raise --turn-timeout and the caller\'s '
                                f'request_timeout_s together.')
            if not one_turn.acquire(timeout=wait):
                raise self.busy(f'A turn is already running and this request\'s share of its '
                                f'{turn_timeout:g} s budget ({wait:g} s of queue wait) elapsed. '
                                f'This gateway performs one turn at a time.')
            try:
                remaining = self.budget - time.monotonic()
                if remaining < min_inference:
                    raise self.busy(f'Only {remaining:.1f} s of this request\'s {turn_timeout:g} s '
                                    f'budget was left when the turn lock came free, under the '
                                    f'{min_inference:g} s minimum; no turn was started.')
                if self.client_gone():
                    return self.drop(request, 'left while queued; no turn was started')
                # The absolute deadline travels, not the duration measured
                # above: the probe just spent part of it, and the revalidation
                # inside the turn will spend more.
                result = perform(request, binding, model_binding, self.budget)
            finally:
                one_turn.release()
            # The turn is already paid for. If the caller has gone, it is
            # finished and discarded here — never a second response attempt.
            if self.client_gone():
                return self.drop(request, 'left during the turn; the completed result is discarded')
            try:
                self.respond(200, result)
            except OSError:
                self.drop(request, 'stopped reading before the write deadline; the completed '
                                   'result is discarded')

        def drop(self, request, reason):
            self.close_connection = True
            self.answered = True
            note(f'{request["request_id"]}: the caller {reason}.')

    return Gateway((host, port if port is not None else DEFAULT_PORTS[rt.ENGINE['engine']]),
                   Requests, connections)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Serve one admitted turn binding '
                                                 'as a loopback OpenAI-compatible endpoint.')
    parser.add_argument('--binding', required=True, help='an ADMITTED binding document '
                                                         'or a workbench suite entry')
    parser.add_argument('--model-binding')
    parser.add_argument('--port', type=int, default=DEFAULT_PORTS[rt.ENGINE['engine']])
    parser.add_argument('--host', default=LOOPBACK)
    parser.add_argument('--turn-timeout', type=float, default=TURN_SECONDS,
                        help='the END-TO-END budget in seconds for one request — queue wait, '
                             'readiness commands and the vendor command together (default '
                             '%(default)s, under the context-cog caller\'s 180 s; a caller with '
                             'a longer request_timeout_s raises this to at least 10 s below it)')
    parser.add_argument('--queue-wait', type=float, default=QUEUE_SECONDS,
                        help='the longest a second request waits for the turn lock before '
                             '503 busy; the budget may shorten it (default %(default)s)')
    parser.add_argument('--min-inference', type=float, default=MIN_INFERENCE_SECONDS,
                        help='seconds of budget a turn needs to be worth starting; below it '
                             'the answer is 503 busy and no turn is spent (default %(default)s)')
    args = parser.parse_args(argv)
    try:
        refuse(args.turn_timeout > 0 and args.queue_wait > 0 and args.min_inference > 0,
               '--turn-timeout, --queue-wait and --min-inference must all be positive.')
        refuse(args.min_inference < args.turn_timeout,
               f'--min-inference {args.min_inference:g} is not less than --turn-timeout '
               f'{args.turn_timeout:g}: every request would be answered 503 busy.')
        binding, model_binding = prepare(args.binding, args.model_binding)
        server = make_server(binding, model_binding, args.port, args.host,
                             args.turn_timeout, args.queue_wait,
                             min_inference=args.min_inference)
    except ValueError as exc:
        print('turn gateway refuses to start: ' + str(exc), file=sys.stderr)
        return 2
    note(f'on http://{LOOPBACK}:{server.server_port}/v1 serving {served_model(binding)} '
         f'(one turn at a time; {args.turn_timeout:g} s end to end per request, of which a '
         f'turn needs {args.min_inference:g} s to start; bearer token required on every '
         f'route; loopback only; restart after any rebind)')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
