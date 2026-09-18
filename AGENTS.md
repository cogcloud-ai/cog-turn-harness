Read COG.md and README.md. Custom reference provider, not Smith template machinery.
turn_runtime.py, turn_gateway.py, tests/test_gateway.py and tests/test_provider.py are maintained in cog-turn-harness and vendored byte-identically; the README records their SHA-256.
The turn gateway is loopback-only and serves one admitted binding; it never routes another user's subscription.
COG_TURN_GATEWAY_TOKEN (>= 32 chars) is MANDATORY: the gateway refuses to start without it and every route, /health included, requires the bearer token. Never make it optional again.
Refuse browser-shaped requests before anything else (Host allowlist, any Origin, non-JSON Content-Type), bound every wait (headers, body, queue, turn, connections), and return an error object for every failure - nothing reaches the vendor before full validation.
System messages become the turn's context and the user message alone is task.input, so the rendered prompt matches the workbench host.
Never self-admit, copy login secrets, bypass vendor permissions, or claim bare-model evidence for subscriptions.
Run the deterministic tests and Smith package checks after changes.
