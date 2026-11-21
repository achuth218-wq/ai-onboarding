# Security boundaries and operational controls

This design separates intake, build, plan, deployment and cloud networking. Automated checks cover repository inputs and deployment behavior; external infrastructure controls remain the platform team's responsibility.

## Cloud and exposure boundaries

| Boundary | AWS | Azure | GCP |
|---|---|---|---|
| Administrative scope | Account/IAM roles per target or equivalent enforced scope | Subscription/resource-group RBAC per target | Project/service-account IAM per target |
| Internal ingress pattern | Private subnets, internal load balancer, approved private routing | Private VNet ingress/internal load balancer; Private Link where appropriate | Private VPC ingress/internal load balancer; private service connectivity where appropriate |
| External ingress pattern | Protected public edge with WAF/DDoS controls; restrict direct backend access | Protected public edge with WAF/DDoS controls; restrict direct backend access | Supported external load balancer with Cloud Armor policy; restrict direct backend access |
| Common controls | Approved DNS/egress, metadata denial, TLS validation, audit redaction | Approved DNS/egress, metadata denial, TLS validation, audit redaction | Approved DNS/egress, metadata denial, TLS validation, audit redaction |
| Deployment execution | Dedicated ephemeral runner group and target credentials | Dedicated ephemeral runner group and target credentials | Dedicated ephemeral runner group and target credentials |

These are deployment patterns to adapt to the organization's landing zones, not resources created by this repository. Private ingress must be enforced by network reachability/firewall/load-balancer configuration; an `internal` field alone does not make a gateway private. Public clients still require authentication and authorization. Deny direct backend access that bypasses the public edge.

AWS provides resource/subnet traffic controls and flow logs; use them in the target's network design. [AWS VPC security](https://docs.aws.amazon.com/vpc/latest/userguide/VPC_Security.html)

Azure Private Link can provide private service access within a virtual network, where the target service and chosen architecture support it. [Azure Private Link](https://learn.microsoft.com/en-us/azure/private-link/private-link-overview)

Cloud Armor supplies configurable edge protection for supported GCP resources; select the correct load-balancer/policy combination. [Cloud Armor overview](https://cloud.google.com/armor/docs/cloud-armor-overview)

## Automated checks

| Stage | Enforcement | Failure behavior |
|---|---|---|
| Issue intake | Exact field contract, duplicate rejection, bounded body, no executable interpolation | No branch/PR created |
| Candidate generation | One manifest plus only that lane's four upstream mappings | Cannot edit policy/credentials/workflows |
| Candidate secret scan | Pinned Gitleaks before Git writes; no raw scanner output | No Git mutation on scanner failure |
| Placement policy | Three clouds, two exposures, stage inventory, unique gateways/namespaces, US Konnect region | Validation fails |
| Upstream policy | HTTPS/443, exact hostname approval, no credentials/query/fragment, no IP literals/local/metadata endpoints | Validation fails |
| Classification/provider | Platform-owned allowlists; sensitive external data denied by default | Validation fails |
| Pull request CI | Tests, native/input schemas, secrets in working tree/history, Bandit medium+ severity/confidence, pip-audit | Required check fails |
| Workflow policy | Full action SHAs, no persisted checkout credentials, no PR-target trigger, scoped write permission, no inline expressions in scripts | Required check fails |
| Build and every independent promotion | Repeats security checks | No deployment on scanner/advisory failure |
| Promotion provenance | Same original artifact/digest, source commit, lane and successful immediate predecessor workflow/receipt | Cannot skip a stage or mix releases |
| Recovery provenance | Previous successful original Deploy run at the same target stage | Cannot use recovery as an untested-stage shortcut |
| Deployment preflight | Enabled/configured target, required control attestations, gateway identity/runtime and node health | No apply |
| Apply | Plan digest freshness, resource-type/namespace allowlist, create/update only | Stops on changes outside approved plan |
| Post-deployment | Node convergence, anonymous rejection, authorized protocol probes, partial-failure receipt | Stops stage promotion |

The static workflow policy is one CI check, not a replacement for a protected organization ruleset: a contributor proposing workflow-policy changes must receive platform/security review. Bandit contains narrow documented suppressions for SafeLoader/BaseLoader and fixed HTTPS checksum-verified downloads; subprocesses receive argument arrays and do not use `shell=True`.

The scanner step fails closed if the advisory service or scanner fails. Dependency auditing is time-dependent and must be repeated after dependency changes and periodically in your organization. Tool binaries use pinned release checksums; Python package hashes cover the selected releases. Refresh locks in reviewed dependency updates. Use organization secret scanning/push protection where available in addition to CI. A clean scan cannot prove that no secret exists.

## Identity and runners

Plan and write tokens are separate and target-specific. Grant only the Konnect roles needed for that gateway and managed entities; validate actual role boundaries in your organization. Namespace checks prevent accidental cross-ownership plans, but do not reduce a token's underlying privileges. Do not use an organization-wide administrator token.

Use individual GitHub Environment secrets. Provider credentials are deferred into native secret references and are not resolved into artifacts. Secret values should not appear in command arguments, issue fields or logs. Raw Kong API/CLI output is not uploaded. The deploy workflow removes `.state` diagnostics; runners must still be destroyed after the job and disks/log sinks managed by the platform.

Untrusted PRs and issue intake run on GitHub-hosted runners without Konnect/provider credentials. Intake receives a repository token with the minimal write scopes necessary to create a branch and draft PR. It checks out `main`, reads issue text as data, never checks out the generated branch, never evaluates form content, never merges and never deploys. Credentialed GitHub API requests do not follow redirects.

Runner-group restrictions must prevent fork or modified PR workflows from scheduling private-network runners. Scope groups to the protected deployment workflow/ref when supported. A group name or `self-hosted` label is not itself a security boundary if repository/workflow access is unrestricted. Verify these settings before activation. [GitHub runner access controls](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/manage-access)

No cloud API login is necessary for this baseline: Konnect manages gateway configuration, and cloud infrastructure already exists. When extending to AWS/Azure/GCP APIs, use short-lived OIDC federation with issuer/audience/repository/ref/environment restrictions and a minimal role. Grant `id-token: write` only to the job that needs it. OIDC for cloud access does not automatically replace Konnect's token. [GitHub OIDC](https://docs.github.com/en/actions/concepts/security/openid-connect)

## Residual risks and required platform verification

- Allowlisted DNS can change or resolve to private/metadata addresses. The compiler does not resolve every hostname, inspect redirects at data-plane runtime or configure cloud egress. Enforce outbound destination controls, DNS integrity, metadata denial and upstream TLS at runtime. Smoke clients do not follow redirects.
- `controls` booleans and `security_review_url` are attestations. Verify private-only reachability, edge protection, quotas, redaction and TLS with infrastructure tests and attach evidence. No cloud posture inspection is implemented.
- Data classification is declared, not inferred from real prompts. Review model data use, retention, region and privacy agreements independently. US Konnect management location is not a model-processing residency guarantee.
- Authenticated MCP tool discovery is checked against the expected list. Enforce per-tool authorization, dangerous-operation approval and server-side argument validation at the upstream. Discovery names alone are not access control. A2A task authorization and cancellation/lifecycle policies also belong in the service and gateway policy design.
- Rate/quota/WAF policy IDs are referenced, not policy-body-audited. Verify shared policy behavior in preproduction. LLM prompt injection and tool misuse require application controls beyond this pipeline.
- Tokens can expire between planning and verification. Use appropriate short-lived/rotated credentials and least-privilege test identities; safely rerun only after understanding any partial apply.
- Plan comparison is not an atomic Konnect transaction. Coordinate external writers. Recovery is reviewed create/update and cannot automatically undo side effects or remove newly introduced resources.
- GitHub repository/runner/environment protections cannot be enforced by adding YAML files alone. Required reviewers must be configured separately; multiple listed reviewers normally mean one may approve. Use additional organization controls when two independent teams must approve.

If a secret is submitted in an issue or committed, rotate/revoke it first, restrict access as appropriate, remove exposed copies under the incident process and assess use. Do not report confidential security issues through a public onboarding form; use the organization's private incident channel.

## Independent promotion controls

Build release produces an artifact without deploying. Four manual dispatch workflows deploy dev, test, preprod and prod separately. A run never launches the next workflow. The release run ID and selected lane stay constant through the chain; test requires dev evidence, preprod requires test, and prod requires preprod. Each stage requires its own configured GitHub Environment approval.

The gate reads GitHub Actions run/artifact metadata with `actions: read`, accepts only successful expected main-branch workflows from this repository, selects exact unexpired artifact IDs, and matches receipt identities, run attempts and bundle checksums. Tokens are not forwarded through redirects. A verified promotion receipt is produced only after a verified deployment receipt and successful protocol checks. Failed or partial runs cannot authorize promotion. The code uses the current trusted dispatch revision and never executes scripts from an old release artifact.

Configuration is immutable within a release. The selected target file must match current trusted configuration. Enabling a target or changing its endpoint/allowlist after a build requires a new build and dev-first evidence. Preceding-stage evidence is historical successful verification, not proof of the currently deployed predecessor version. Add organizational evidence-age/current-version requirements if needed. Artifacts expire after 90 days; all original and predecessor evidence is required.

Writes are serialized per cloud/exposure lane across promotion and recovery. Different lanes remain independent. GitHub concurrency is not a durable FIFO queue; verify pending/cancelled runs. Direct local/API writes and other repositories are outside this control. Local CLI debugging does not implement the GitHub evidence chain; keep production write credentials confined to protected execution environments.
