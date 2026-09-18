Read COG.md and README.md. Custom reference provider, not Smith template machinery.
turn_runtime.py, turn_gateway.py and tests/test_gateway.py are maintained in cog-turn-harness and vendored byte-identically.
The turn gateway is loopback-only and serves one admitted binding; it never routes another user's subscription.
Never self-admit, copy login secrets, bypass vendor permissions, or claim bare-model evidence for subscriptions.
Run the deterministic tests and Smith package checks after changes.
