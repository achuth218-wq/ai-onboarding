# Konnect AI onboarding pipeline

Executable GitHub Actions workflows and Python scripts for LLM, MCP and A2A onboarding through **Konnect AI Gateway 2.3.x**. The sample runtime is pinned to **2.3.1** and the management CLI to **kongctl 1.28.4**.

**Kong Gateway 2.3 is a different, legacy product and cannot run this pipeline's AI configuration.** Provision or migrate to a compatible Konnect AI Gateway and its data planes first. This repository configures existing infrastructure; it does not upgrade data planes, create a Konnect organization, deploy upstream applications, or create your enterprise identity system.

## What executes

1. A pull request validates strict manifests, references, route overlaps and pinned native Kong schemas, runs tests, and builds a preview artifact without deployment credentials.
2. A merge to `main` builds one immutable bundle containing all three environments.
3. The reusable deploy workflow plans against live Konnect state using a read token and publishes a value-free change summary plus its digest.
4. The environment approval gate runs before the write job. The write job repeats preflight and planning, rejects a changed plan, and applies the freshly verified plan.
5. All registered data planes must have the pinned runtime, a fresh heartbeat, no configuration errors, and the control plane's expected configuration version.
6. Authenticated protocol smoke tests must pass before the next environment starts.
7. A deployment receipt records success or partial deployment status. Failed verification stops promotion; it does not blindly replay an old plan.

The supported baseline includes native `model` entities, basic-header-auth model providers supported by the pinned schema, MCP `passthrough-listener` and `conversion-listener`, and A2A JSON-RPC 0.3.0 agents. The included LLM example is OpenAI. Provider formats, IAM authentication, other MCP revisions and other A2A bindings require an adapter/test extension rather than silent fallback.

## First run locally

Python 3.12 is used in CI. Python 3.9 or later can run the local commands.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python -m aiops validate
python -m aiops build --revision local
python -m aiops verify
```

No credentials or live gateway are needed for these checks. The tests start a local mock HTTP server on an ephemeral loopback port. The build rejects a nonempty destination, so use a new `--output` directory when rebuilding locally.

Inspect `build/release/dev/kong.yaml` to see the generated native configuration. `!secret` and `!env` references remain unresolved in the artifact.

## Configure Konnect prerequisites

Use a separate existing AI Gateway for each environment. On each gateway:

- Run AI Gateway 2.3.1 data planes, or deliberately update `runtime_version` in the environment file to another validated 2.3 patch. Set the gateway's `min_runtime_version` to `2.3`. If Konnect manages automatic upgrades, coordinate that setting with this pipeline's exact version pin.
- Ensure the AI Gateway entitlement is active and nodes can reach upstream model providers, MCP servers and agents.
- Create or select an inbound AI Auth Strategy. Use an existing OIDC strategy or key-auth strategy. Copy its UUID into `auth_strategy_id`.
- Create an AI Consumer Group such as `ai-onboarding-testers`, and ensure the smoke-test identity belongs to it. For OIDC, configure claim-to-consumer/group mapping. A valid token alone is not enough if it lacks the required group.
- Create shared, protocol-appropriate AI policies. Supply their names or IDs in `policy_profiles`. Each profile must be nonempty for deployment. For example, use approved request/token limits for LLMs and request limits/audit controls for MCP/A2A. LLM prompt policies should not be attached to MCP traffic.
- Reserve the route prefixes used in this repository. The compiler checks overlaps within its catalog; your platform must coordinate routes owned outside this namespace.
- Make upstream MCP/A2A endpoints reachable using your established network and upstream-authentication arrangement. This baseline does not create Token Vault enrollment, inject arbitrary upstream bearer credentials, or provision private-network connectivity.

The pipeline manages its generated model providers, models, MCP servers and agents. Shared gateways, policies, consumers and auth strategies remain owned by the platform. Existing resources with conflicting names must be deliberately adopted or renamed before onboarding; this pipeline does not take over resources automatically.

## Edit all three environment files

Update `environments/dev.yaml`, `staging.yaml` and `prod.yaml`:

| Field | Required value |
|---|---|
| `product` | Exactly `kong-ai-gateway` |
| `runtime_version` | Actual tested AI Gateway 2.3 patch, initially `2.3.1` |
| `region` | `us` in dev, staging and prod; use AI Gateways located in the US Konnect region |
| `gateway_id` | Existing AI Gateway UUID, not a traditional Gateway control-plane ID |
| `namespace` | Exclusive ownership namespace for this environment |
| `auth_strategy_id` | Existing inbound AI Auth Strategy UUID |
| `consumer_groups` | Authorized groups including the smoke-test consumer |
| `proxy_url` | HTTPS gateway origin visible to the deployment runner |
| `policy_profiles` | Existing policy names or IDs, grouped by protocol |
| `upstreams` | HTTPS MCP/agent upstream URLs visible to data planes |
| `smoke_auth_header` | `Authorization` for OIDC, or your configured API-key header |
| `minimum_nodes` | Required registered node count |

The zero UUIDs, `.invalid` URLs and empty policy profiles are intentional placeholders. They pass offline schema checks but are rejected before any live deployment.

Edit the catalog examples to use your actual model, MCP tool names, A2A skills and route paths. Tool names in `expected_tools` must exactly match what the smoke identity can discover. This is a verification assertion, not a runtime per-tool allowlist: configure tool-level authorization in the existing MCP server or extend the MCP adapter with native tool ACLs. The generated server-level access ACL restricts the authorized consumer groups.

Only configure `readonly_call` or `readonly_message` smoke inputs for operations you have verified are safe to repeat. They are omitted by default. LLM smoke tests make two small live provider requests when streaming is enabled and may incur provider charges.

## Configure GitHub

Copy this entire directory, including `.github`, into the root of a new or existing repository. The workflows assume these files are at the repository root and the protected release branch is named `main`.

Create GitHub Environments named **dev**, **staging** and **prod**. Allow only `main` to deploy. Configure required reviewers on `prod` and, if desired, `staging`. Prevent self-review where your GitHub plan supports it. Protect `main` with PR review and the `Validate / validate` required check; protect workflow, adapter and environment-file changes with CODEOWNERS suitable for your organization.

Set this **repository secret**:

| Secret | Purpose |
|---|---|
| `KONNECT_READ_TOKEN` | Least-privilege Konnect automation token with read/list access to all three AI Gateways and the dependencies needed for planning |

Set these secrets separately in **each GitHub Environment**:

| Secret | Value |
|---|---|
| `KONNECT_TOKEN` | Konnect system-account token allowed to read/create/update managed AI entities in that environment |
| `AIOPS_SECRET_VALUES` | JSON object containing deferred provider secrets |
| `SMOKE_AUTH_VALUE` | Full inbound header value for the test identity, such as `Bearer <access-token>` or a raw API key |

Example shape for `AIOPS_SECRET_VALUES` (store the actual value only in GitHub Secrets):

```json
{"AIOPS_LLM_AUTH_HEADER":"Bearer YOUR_PROVIDER_API_KEY"}
```

The key must match the catalog's `credential_env`; the value must include the header scheme required by that provider. For Anthropic-style header authentication, select the provider and header supported by the Kong schema and supply a raw key when that header expects one. Secrets are created through deferred `!secret` sources. This repository deliberately does not pass `--write-secrets`; changing an existing provider secret requires your separate, explicit secret-rotation procedure.

Reusable workflow callers pass these exact secret names. The environment on the called `apply` job supplies the corresponding environment values. Do not replace them with differently named aliases without updating the workflow contract.

For a private gateway endpoint, set the repository variable `AIOPS_RUNNER_JSON` to a JSON runner-label array, for example:

```json
["self-hosted","linux","ai-deploy"]
```

Use isolated/ephemeral deployment runners. The deployment runner needs HTTPS connectivity to Konnect, GitHub releases, Python package indexes and the gateway's public/client endpoint. The data planes separately need connectivity to upstreams. PR validation always uses a GitHub-hosted runner and receives no deployment secrets.

## Deploy

Open a PR with the configured environment files and catalog. Review the rendered preview, merge it, then watch **Release**:

```text
Build → Dev plan/apply/tests → Staging plan/approval/apply/tests
      → Production plan → Production approval → Apply/readiness/tests
```

Before approving production, inspect the plan job's summary and the `ai-release` artifact. The summary shows resources and changed field names; values remain in the reviewed native configuration artifact. The apply job recalculates the complete plan and compares its digest with the reviewed digest. Metadata generation time is excluded from comparison. Other differences cause a failure and require a new run/review.

GitHub serializes Release and Recovery with one repository-wide writer group. Do not add another deployment repository or enable manual concurrent writes to the same namespaces without a shared locking strategy. This controls this repository's writers; it cannot make Konnect API changes transactional or lock external administrators. A state change between the final check and API execution remains a race, so production should have one operational writer.

The baseline applies authenticated routes in place. For initial onboarding, keep access restricted to the test cohort and expand `consumer_groups` in a second reviewed release after verification. Percentage canaries, automatic traffic switching and automatic rollback are not implemented in this baseline.

## Local live execution

After configuring the environment files and supplying the same secrets through your shell or secret manager:

```bash
python scripts/install_kongctl.py
export AIOPS_KONGCTL="$PWD/.tools/kongctl"
# Supply KONNECT_TOKEN, AIOPS_SECRET_VALUES and SMOKE_AUTH_VALUE securely.
python -m aiops build --output build/live --revision local
python -m aiops plan --bundle build/live --environment dev --output build/dev-review.json
# Inspect build/live/dev/kong.yaml and build/dev-review.json before executing.
PLAN_DIGEST="$(python -c "import json; print(json.load(open('build/dev-review.json'))['plan_digest'])")"
python -m aiops apply --bundle build/live --environment dev \
  --expected-plan-digest "$PLAN_DIGEST" --output build/dev-deployment.json
```

The installer downloads the official CLI archive, verifies its committed SHA-256 checksum and extracts only the executable. Actions are pinned to commit SHAs in the workflows. `toolchain.lock.json` records the reviewed pins.

## Drift and recovery

**Detect drift** runs daily at 04:17 UTC and can be run manually. It creates a read-only apply plan; exit code 2 means managed resources differ from Git. It never writes. Because apply does not prune, this detects missing/changed declared resources, not every unmanaged or removed resource in the gateway. A run during a pending deployment can report expected differences.

**Recover previous release** takes the run ID of a successful `Release` workflow and a target environment. It verifies that the artifact came from this repository's successful `main` release, checks the source SHA and bundle checksums, creates a fresh recovery plan and uses the normal environment approval and verification gates. It executes current trusted pipeline code against the old immutable configuration.

Recovery is create/update only. It cannot undo tool calls or agent work, restore deleted secrets, or remove newly created entities merely because they are absent from the older bundle. Review and retire newly introduced resources separately. Reconcile Git after recovery so a later release does not reintroduce the failed change. Artifact retention is 90 days; archive release bundles in your approved long-term store if longer recovery history is required.

If deployment fails, inspect `deployment-<environment>` for its status: `not-applied`, `apply-started`, `applied-awaiting-verification` or `verified`. A partial apply can exist when the process fails. Raw Kong CLI/API output is not uploaded because it may contain sensitive fields; `.state/kongctl.log` is available only during local execution and is removed from deployment runners at job end. No job claims rollback succeeded automatically.

For retirement, remove consumer access, drain active sessions/tasks, generate an explicit Kong delete plan for the intended resource, review it, execute it through your change process, and remove the declaration. This repository never uses `sync` or wildcard deletion.

## Extend the implementation

- Add resource manifests under `catalog/` for more onboardings; change only environment references for endpoints and shared governance.
- Add a protocol adapter in `aiops/adapters.py`, its input schema in `schemas/onboarding.json`, and protocol tests. Register it in `ADAPTERS`.
- Add provider-specific authentication/rendering when basic header authentication is insufficient. Native schema validation rejects unsupported combinations.
- Replace `aiops/deploy.py` behind its plan/apply/readiness contract for another management backend. A decK backend must render traditional Gateway entities and is not bundled here.
- Add task lifecycle, denied-tool, load and failure-injection tests appropriate to your upstream systems before broad production rollout. The included probes cover generation/streaming, anonymous rejection, MCP session/discovery and optional read-only calls, and A2A discovery/URL rewriting and optional read-only messages.

## Validation evidence and sources

Local automated tests exercise tampered artifacts, stale approvals, unexpected deletions, namespace boundaries, version/heartbeat/configuration readiness, anonymous access, protocol behavior and partial-failure receipts. Workflows were checked with actionlint 1.7.12. Generated resources validate against machine-readable schemas exported from the official kongctl 1.28.4 binary. These checks do not replace live validation against your Konnect account, entitlement, policy configuration and upstreams.

- [Kong AI Gateway management](https://developer.konghq.com/ai-gateway/kongctl/)
- [Kong planning and recovery behavior](https://developer.konghq.com/kongctl/declarative/planning/)
- [Kong AI Gateway lifecycle and runtime requirements](https://developer.konghq.com/kongctl/declarative/resource-behavior/)
- [MCP entities and access control](https://developer.konghq.com/ai-gateway/entities/ai-mcp-server/)
- [AI Agent entities](https://developer.konghq.com/ai-gateway/entities/ai-agent/)
- [GitHub reusable workflows](https://docs.github.com/en/actions/how-tos/reuse-automations/reuse-workflows)

The native schema snapshot retains the Kong project's license in `schemas/kongctl-1.28.4/LICENSE`.
