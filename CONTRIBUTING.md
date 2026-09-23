# Contributing

Start with the [suite repository guide](https://github.com/cogcloud-ai/cog-op-builder/blob/main/docs/repositories.md)
and [development setup](https://github.com/cogcloud-ai/cog-op-builder/blob/main/docs/getting-started.md).
Use the documented sibling checkout layout and run `pixi run test` in this repository.
Read AGENTS.md when present. Fix copied machinery in its upstream owner and
vendor it byte-identically; do not patch generated runtime files independently.

Open component issues here and link cross-repository work to a suite issue.
Use focused pull requests with the problem, behavior change, and test results.
Model-free fixtures are not live model qualification. Do not commit credentials,
provider login state, local bindings, generated run records, or private inputs.

Contributions are under this repository's LICENSE. Retain third-party notices.
