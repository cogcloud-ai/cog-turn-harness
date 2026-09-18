# JSON Turn Harness

A **Harness-only Cog** implementing one text-to-JSON turn against a separately admitted OpenRouter model binding. It supplies context assembly and output-schema enforcement, with no tools or conversation memory.

## Install and connect

```sh
pixi install
pixi run test
pixi run card
pixi run check
```

Configure and admit a model in cog-openrouter first. Supply the same `OPENROUTER_COG_TOKEN` to this harness as to the loopback gateway. The harness never receives the upstream API key.

The vendor CLI and its login store remain vendor-owned. The reference discovers
CLIs on PATH and in `~/.local/bin`. It does not initiate a login, copy credentials,
install a background login service, or route another user's subscription.
`check` checks CLI controls and login readiness for subscription adapters.
The pure harness checks its package; model availability is checked by the host.

## Bind and use through workbench

From the sibling cog-workbench directory:

```sh
pixi run suite -- bind --provider cog-turn-harness --request ../cog-turn-harness/examples/bind-request.json
pixi run suite -- compose --context cog-op-designer --binding-id binding-cog-turn-harness --revision 1
```

The compose result names a saved `record_path`. Invoke that record with:

```sh
pixi run suite -- invoke --composition /absolute/path/to/composition.json --bundle ../cog-op-designer/examples/sample-bundle.json
```

Or open workbench's **Design & build** screen at `/studio`. Its connection panel
performs selection and host admission, then reuses exact binding revisions.
For the pure harness, its example refers to `binding-author-model` revision 1;
change that reference to the model you admitted. Start the OpenRouter gateway
with workbench's **Start model gateway** button before a model-backed turn.

Direct provider tasks:

```sh
pixi run bind -- --request examples/bind-request.json
pixi run turn -- --request turn.json --binding admitted-binding.json
```

For Harness-only, add `--model-binding admitted-model.json`. The `turn` task is
a local host-authorized command, not an admission endpoint. Passing a file is
not a security boundary against the same OS user. Use workbench's immutable
store for correlation, package-change detection, revocation and provenance.
The command adapter stays available through workbench; each vendor invocation
is a fresh bounded process. Subscription turns allow up to ten minutes for
whole-source authoring; readiness checks retain their fifteen-second limits. There is no persistent hidden agent conversation.

## Serving a turn as an endpoint

A context Cog reaches its model through an OpenAI-compatible endpoint named in
its `model.json`. This Cog exposes a turn, not an endpoint, so `src/turn_gateway.py`
is a loopback shim that accepts one chat-completions call and performs exactly
one turn in process.

```sh
pixi run serve -- --binding /path/to/admitted-binding.json
pixi run serve -- --binding ../cog-workbench/var/suite/<hash>-<revision>.json --port 8123
```

`--binding` is an **admitted** binding document — either a bare document or a
workbench suite entry carrying one under `binding`. The gateway refuses to start
on anything else and names the reason. Add `--model-binding FILE` when the
binding references a separately admitted model. The default port is 8123; the
sibling providers default to 8121 (cog-claude) and 8122 (cog-chatgpt).

The surface is `POST /v1/chat/completions`, `GET /v1/models` and `GET /health`.
The one model id served is `turn-harness/<binding_id>@<revision>`; a request naming
another model is a 400. The system message and the user message become the
turn's rendered `task.input` under plain headers, and `response_format` must be
`json_schema` — its `schema` becomes the turn's `task.output_schema`.
`json_object`, no `response_format`, `stream: true`, `tools` and `n > 1` are
each a 400 whose message names what a turn does support. The reply's
`choices[0].message.content` is the turn's result as JSON text, alongside an
`x_cog` object carrying provider identity, the binding reference, the request
id, the provider's observations, `model_identity_verified: false` and
`evidence_scope: composed-system`.

If `COG_TURN_GATEWAY_TOKEN` is set in the gateway's environment, a matching
bearer token is required on every request. No tools, no thread, no memory —
exactly the turn contract.

Honestly:

- **Loopback only.** The gateway binds `127.0.0.1` and refuses any other host by
  name. It performs the turn with **the owner's own vendor login**. It is
  **never a route for anyone else's subscription**.
- **Model+Harness, not a model.** The vendor CLI wraps the turn in its own
  harness, so this is a **Model+Harness composition**; the Cog's envelope names
  the gateway's model id, **not a verified model**. Model identity is
  unverified and reported as such.
- **Restart after any rebind.** The gateway runs outside the workbench host, so
  **revocation there does not reach a running gateway**. Binding documents are
  read once at start (working rule 6).
- **`temperature` and `max_tokens` are accepted and ignored.** A turn has no
  sampling controls.
- **Turns take tens of seconds.** One turn runs at a time; a second request
  waits. A failed turn is a `502` carrying the provider's own error code in an
  OpenAI-style `error` object — never a fabricated completion.

## Contract and evidence

The binding draft's `composition` is `harness`. Both provider and harness
identities are retained. The harness binding names the exact separately admitted model binding and revision.
Changing vendor CLI version, package content, model choice or configuration
requires rebinding. Model weights and immutable revision are never claimed
verified. Only declaration-level qualification is implemented here; stronger
probe requirements fail. Packaged consumer checks run before and after the
turn in the context Cog's declared bridge. The provider does not advertise
`contract-checks/packaged` as its own independently implemented feature.

Subscription adapters explicitly advertise `tools/vendor-restricted`, not a
portable proof of `tools/none`. Codex disables known tool/customization surfaces
and uses read-only permissions; Claude disables built-in and MCP tools and
loads its safe mode. These remain vendor harnesses with vendor-managed policy,
system context and service retention. No external tool grants or remembered
threads are accepted. This is not an OS isolation guarantee against the CLI
itself or an attestation of every vendor internal action.

Both strict vendor JSON output and prompt-requested JSON are supported. Schemas
with open-ended objects use the latter; the full local JSON Schema validation
is always required. Invalid, malformed or interrupted results fail without
silently retrying, switching models or returning a fabricated successful turn.
OpenRouter turns additionally retain the gateway's observed model/provider and
run-record reference. Subscription observations distinguish requested model
from unverified actual model identity.

Use these subscription Cogs to evaluate a **composed system**, never as
bare-model benchmark satisfiers. An evaluator Cog is a separate work role;
using the same subscription/model for authoring and reviewing does not prove
independent model judgment.

## Verification

`pixi run test` is deterministic and uses fake vendor/process responses. It
covers composition rejection, candidate/admitted separation, exact references,
locality, feature and pinning constraints, credential filtering, context/schema
checks, command controls, version changes and schema fallback. See the suite's
`docs/verification-2026-09-07.md` for live-test status. No live quality claim is
made by these tests.

Core/profile package check from the CogLab workspace:

```sh
../cog-smith/.pixi/envs/default/bin/python ../cog-smith/src/cogsmith_cli.py check .
```

This is a custom runtime. Smith may warn that its template-runtime checker does
not apply; the dedicated provider tests validate the custom implementation.

## Source ownership

`src/turn_runtime.py`, `src/turn_gateway.py` and `tests/test_gateway.py` are
maintained in cog-turn-harness and copied byte-for-byte into the two
subscription repositories. Update all copies together; the engine and provider
declaration are package-specific. This keeps each Cog independently installable
without requiring a sibling repository at runtime. `tests/test_gateway.py`
enforces the gateway copy against whichever siblings are present, and skips when
none are.

Runtime SHA-256: `f8ecf8215d2b153234a24d60f021c61a5d7a02a5b32825d36c7a20664b988849`

Official integration references are in `docs/sources.md`.
