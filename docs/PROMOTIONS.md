# Independent dev, test, preprod and prod triggers

Each stage has its own manual GitHub Actions trigger. Build a release once, keep the same original release run ID, and promote one cloud/exposure lane at a time. The gates preserve order; success does not launch the next stage.

## Trigger sequence

| Action | Release input | Previous-run input | What completes |
|---|---|---|---|
| Build release | None | None | Artifact only; record run ID `R` |
| Deploy dev | `R` | None | Dev deployment and verified receipt; record `D` |
| Deploy test | `R` | `D` | Test deployment and verified receipt; record `T` |
| Deploy preprod | `R` | `T` | Preprod deployment and verified receipt; record `P` |
| Deploy prod | `R` | `P` | Production deployment and verified receipt |

Use the same `lane` throughout, for example `aws-internal`. The six available lanes are AWS/Azure/GCP × internal/external. Each run deploys only its selected lane and stage. Another lane's failure does not block this one.

## GitHub UI

1. Merge the reviewed onboarding PR into protected `main`. Wait for **Build release** to succeed and copy its run ID from the URL or run summary.
2. Open **Actions → Deploy dev → Run workflow**. Choose `main`, enter the release run ID, select the lane and start the run.
3. Inspect the read-only plan and complete the configured approval for `<lane>-dev`. Wait for the whole workflow to finish successfully, including its receipt-recording job.
4. When ready, start **Deploy test** with the same release ID/lane and the successful dev run ID as `previous_run_id`. Review/approve `<lane>-test` independently.
5. Repeat for **Deploy preprod**, supplying the successful test run ID, then **Deploy prod**, supplying the successful preprod run ID. Each has its own protected environment.

A workflow run ID is the numeric `.../actions/runs/<ID>` value. It is not a commit SHA, artifact ID, job ID or PR number. The original release run ID does not change between stages; the predecessor deployment run ID changes at each step.

## GitHub CLI

These commands are for you to execute in the installed repository using an authenticated `gh` CLI. Replace the example numeric IDs with actual successful run IDs. Do not execute the commands as one unattended chain: review each plan, approve its environment, wait for success, and record the resulting run ID before starting the next stage.

```bash
# Builds only; use an existing successful push-triggered build if available.
gh workflow run release.yml --ref main
# Find the successful build and record its run ID.
gh run list --workflow release.yml --branch main --limit 5
```

```bash
# Suppose 10001 is the successful original Build release run.
gh workflow run deploy-dev.yml --ref main \
  -f release_run_id=10001 -f lane=aws-internal
# After approval and successful completion, record the dev run ID (e.g. 10002).
gh run list --workflow deploy-dev.yml --branch main --limit 5
```

```bash
# Independently trigger test when ready.
gh workflow run deploy-test.yml --ref main \
  -f release_run_id=10001 -f lane=aws-internal -f previous_run_id=10002
# Wait for successful test verification; suppose its run ID is 10003.
```

```bash
# Independently trigger preprod when ready.
gh workflow run deploy-preprod.yml --ref main \
  -f release_run_id=10001 -f lane=aws-internal -f previous_run_id=10003
# Wait for successful preprod verification; suppose its run ID is 10004.
```

```bash
# Independently trigger production when ready.
gh workflow run deploy-prod.yml --ref main \
  -f release_run_id=10001 -f lane=aws-internal -f previous_run_id=10004
```

## Promotion evidence

Each promotion validates the original successful `release.yml` run and exact `ai-release` artifact, then checks the artifact's source revision and SHA-256 bundle digest. Later stages additionally validate the prior successful stage's workflow and `promotion-<lane>-<stage>` artifact. Evidence records the original release/artifact IDs, source commit, lane, stage, workflow run/attempt and deployment receipt digest.

The current run copies the verified bundle into its own `ai-release` artifact for the shared plan/apply workflow; this is a copy, not a rebuild. It only emits a successful promotion receipt after the deployment status and protocol checks are verified. Source/prior runs must have completed successfully in this repository on `main`, not in a fork or PR context.

## Retry and recovery

- **Input or approval problem:** correct the issue and start a fresh stage dispatch with the same release and preceding-stage IDs. There is no automatic onward trigger.
- **Partial apply or failed smoke test:** inspect `deployment-<target>` and live state first. A fresh dispatch creates a fresh plan; it cannot pretend rollback occurred. Earlier successful stages do not need to rerun if the exact release/configuration remains unchanged.
- **Changed release or target configuration:** build a new release and restart at dev. The old artifact cannot be modified to incorporate a new endpoint or enable a previously disabled stage.
- **Missing/expired original or predecessor artifact:** promotion is blocked. Retain artifacts within the configured 90 days or implement an approved immutable long-term evidence store; do not bypass the gate.
- **Recovery:** run **Recover verified release** with the old release ID, lane, target stage, and a successful original Deploy run for that same old release/target. Restore any changed target configuration through a reviewed PR first. Recovery cannot qualify a release for a stage it has never passed.
- **Re-run all jobs:** prefer a new dispatch. Re-uploading immutable artifact names within an old run may conflict; successful evidence binds the run attempt and rejects mismatched attempts.

All environment protection settings, reviewer rules and runner-group restrictions must exist in GitHub. Workflow YAML does not create them. Restrict every apply environment to `main`, require its own approval, and retain target-scoped credentials. Local CLI commands are for controlled debugging and do not replace these promotion gates.

## Migration from the three-stage package

Replace the old automatic deployment workflows with the supplied files. The old `staging` target is replaced by `test`; `preprod` is added. Provision/update GitHub Environments, runner groups, gateway IDs, policies, host approvals and credentials for the renamed/new targets. Do not reuse one gateway across test and preprod. Recreate pending MCP/A2A issues with the updated four-URL forms, or add the exact new headings (`Test upstream URL` and `Preprod upstream URL`) and remove the old staging heading.

Build a new four-stage release. Previous three-stage artifacts do not provide the required test/preprod configuration or independent stage evidence. The region remains `us`; the resource kinds and six internal/external forms are unchanged.

Manual triggers are standard `workflow_dispatch` events; artifact downloads use the verified source run and artifact IDs. [GitHub workflow triggers](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow), [GitHub artifact API](https://docs.github.com/en/rest/actions/artifacts).
