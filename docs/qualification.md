# Provider qualification

Qualification is optional and must be rerun for the installed CLI version,
platform, adapter package and explicitly selected model. No range of future
vendor versions is silently considered compatible. Existing bindings already
pin `harness.version`; a changed version refuses turns until host rebinding.
Workbench also detects changed adapter source independently.

## Version/platform matrix (2026-10-08)

| Adapter | Installed version | Platform | Recorded scope |
| --- | --- | --- | --- |
| Codex | codex-cli 0.161.0 | macOS arm64 | CLI controls and subscription login readiness passed; live turns not run for this PR |
| Claude Code | 2.1.293 | macOS arm64 | CLI controls and subscription login readiness passed; live turns not run for this PR |
| Both | exact version recorded by each report | Linux x86_64 | Public install platform; deterministic CI tests, live qualification pending |

A supported installation must expose all controls checked by `check`, satisfy
subscription login requirements, and pass this procedure on its exact
version/platform/model. The table records evidence rather than promising that
an untested combination works. Native Windows is outside the Pixi manifest.
Do not change safety flags to make a failing CLI pass.

## Procedure

1. Install the official vendor CLI and log in directly through its supported
   subscription workflow. `pixi run check` inspects controls/login; it exports
   availability and version, without exporting the login response.
2. Choose a model explicitly in both the bind request's configuration and model
   requirement. Use Workbench's `suite bind` to independently admit a new binding.
   Do not change candidate state yourself or reuse a stale model/version binding.
3. Run the declared task against the resulting admitted Workbench record:

```sh
pixi run qualify -- --binding ../cog-workbench/var/suite/YOUR_ADMITTED_RECORD.json --model YOUR_EXACT_MODEL --output /tmp/new-qualification.json --run-live
```

This may spend **two subscription turns**, capped at 120 seconds each. Inputs are
fixed fictional adapter probes, with no workspace/project data. The binding
must be independently admitted and the explicit model must match exactly.
Without `--run-live`, no vendor command starts. Existing report paths are refused
before a turn. The procedure neither logs in nor admits/rebinds itself.

The checks cover authentication and controls on the exact bound version, native
closed-object structured output, open-schema prompt output with local full-schema
validation, a deterministic local supervisor timeout plus an
already expired turn deadline refused without inference,
and unsupported tool-grant refusal before inference. The last is the adapter's
refusal boundary; it does not attest vendor safety-policy refusal, and a synthetic
JSON reply is not an attestation of no internal vendor tools. Invalid output,
permissions, CLI interruption and vendor failure remain failures without retry or
model substitution. Vendor safety refusal cannot have a guaranteed prompt; inspect
it directly and retain only a failed check outcome in public evidence.

## Safe evidence and interpretation

Reports contain named booleans/durations, date, platform, Python/CLI version,
requested model, binding reference and adapter digest. They exclude prompt/result
text, vendor stderr/stdout, exception detail, authentication responses,
credentials and home-directory paths. Inspect sensitive raw diagnostics directly
in the vendor CLI when necessary. Publish only sanitized reports; fabricated
passing reports or credentialed logs are not qualification evidence.

A failed readiness/version check stops before inference. A failed live check
makes the report fail and does not revoke existing bindings automatically;
the host owns admission decisions. The provider advertises declaration evidence,
not independently attested model weights or bare-model quality. Reports do not
upgrade that claim. Credentialed qualification does not run in ordinary CI.

Official references: [Codex CLI](https://developers.openai.com/codex/cli/reference/),
[Claude CLI](https://code.claude.com/docs/en/cli-reference),
[Claude authentication](https://code.claude.com/docs/en/authentication).
Local `--help`, `--version` and readiness were inspected for the matrix above.

The timeout probe terminates a deterministic local Python process and refuses
an already-expired turn deadline. It does not interrupt live model inference.
The requested model must match the binding; moving aliases (for example sonnet)
remain aliases and actual model identity is unverified. Reports fingerprint the
complete behavior file set; the CLI requires a current Workbench record with a
matching package digest. This detects stale packages, not forged local admission.
Output is exclusively created before checks so missing/unwritable parents refuse
before any subscription turn.
