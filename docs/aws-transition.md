# Demonstrate and complete the AWS transition

This is the final acceptance exercise for the delivery lab. A successful
implementation check, a live deployment and human UAT are separate forms of
evidence. Record their actual outcomes; a passing local test does not establish
that a GitHub workflow ran or that a person accepted the application.

## Reproducible demonstration

Use the current successful main-branch release and its full commit SHA throughout.
Keep the original eight-hour environment deadlines and the total €20 lab budget.
Never rebuild an image merely to promote or recover the same release.

| Stage | Procedure | Evidence to retain |
|---|---|---|
| CI | Inspect the PR checks and successful main CI run. | Semgrep, React/Java tests, CodeQL gate, archive and ECR digest metadata. |
| Failed checks | Inspect the deliberate failed React-test run and its skipped later steps. Run the deployment gate tests for the current implementation. | Distinguish historical live failure evidence from current local rejection tests. |
| Automatic DEV | Let Deploy DEV finish after main CI. | Successful run, receipt, source CI ID, exact SHA and runtime image checks. |
| Recovery | Verify prerequisites; recover a previously successful DEV release with unchanged migrations; restore the demonstration release. | Separate recovery receipts, exact digests and a fictional record that survives both operations. |
| Restore promotion eligibility | Rerun the demonstration release's existing Deploy DEV run after recovery. | Fresh normal receipt matching the current task-definition revision; no CI rebuild. |
| Invalid promotion | Request PROD without human acceptance; request UAT using evidence for another SHA. | Expected failed runs, skipped deployment steps and unchanged target environments. |
| UAT | Manually promote the demonstration SHA using its refreshed DEV run. | Successful UAT receipt linked to the selected DEV attempt and original CI artifacts. |
| Human acceptance | Complete the checklist below on the deployed UAT release. | Tester confirmation, SHA, date and actual outcome. |
| PROD | Provision only the missing small PROD foundation; promote using the successful UAT run after human acceptance. | IaC run, eight-hour expiry, acceptance attestation and successful PROD receipt. |
| Final audit | Compare the three deployment receipts against the live stacks, services, HTTP metadata and ECR images. | Machine-readable audit and the observation table below. |
| Cleanup | Leave scheduled deletion enabled; inspect deletion after each deadline. | Record actual deletion separately from the presence of a schedule. |

The existing recovery guide defines its conservative database limits. This
exercise restores application images, not database contents. It does not inject
a failing ECS deployment or demonstrate restoration from an RDS backup.

## Human acceptance

Open the public UAT URL from the successful promotion summary. Confirm the UAT label and the selected release SHA. Then:

1. Add a fictional customer, reload and confirm persistence.
2. Remove that customer, reload and confirm removal.
3. Attempt an invalid email and confirm rejection.
4. Confirm the DEV-only fictional marker is absent from UAT.
5. Record defects or explicitly accept this particular SHA for PROD.

Automated API/Oracle smoke tests complement these interface checks. The agent
must not invent a tester identity or treat automated tests as human acceptance.
The workflow records a manual attestation; it does not enforce an independent
second reviewer.

## Repeat the final audit

`scripts/verify_aws_transition.py` is a read-only verification helper. It validates
GitHub run/artifact provenance, the exact promotion chain, the original release
archive, ECR digests, current ECS task health and images, HTTP release/environment
identity, and distinct database/network/credential resources. It does not fetch
secret values, deploy, change databases, create customers or extend sessions.

```bash
python3 scripts/verify_aws_transition.py --profile default \
  --release FULL_RELEASE_SHA \
  --dev-run PASSED_DEV_RUN_ID --uat-run PASSED_UAT_RUN_ID --prod-run PASSED_PROD_RUN_ID \
  --repository swbergmann/fintech-app --account 637423555881 --region eu-west-1 \
  --output .local/aws-transition/final-audit.json
```

Omit `--prod-run` while waiting for human acceptance. Such a report explicitly
sets `all_three_environments_verified: false`; it must not be described as a
completed PROD demonstration. A failed audit overwrites prior success evidence.
Run this while environments remain active: deleted or expired foundations cannot
pass a live audit. Preserve the dated report and GitHub artifacts for the case
study before the disposable infrastructure is deleted.

## Observations — 26 September 2026

The selected application release is
`059f96c017b5456dd12ff71af0ac2d48c6b607ad`. Verification documentation and the audit
helper on `feature/verify-demonstrate` are subsequent local changes; they are not
part of that already built application release.

| Check | Actual result / evidence |
|---|---|
| Main CI | [Run 36239177994](https://github.com/swbergmann/fintech-app/actions/runs/36239177994) passed for the selected SHA. |
| Initial automatic DEV | [Run 36239347845](https://github.com/swbergmann/fintech-app/actions/runs/36239347845) passed; later attempts refresh evidence after the recovery exercise. |
| Historical failed React test | [Run 35011316632](https://github.com/swbergmann/fintech-app/actions/runs/35011316632), 15 September: React test/build failed; Java, CodeQL analysis and package/store steps were skipped. This predates AWS deployment and is not a new failure injection. |
| Local verification | 119 Python tests passed, including five final-audit tests; all five CloudFormation templates and the relevant workflow syntax passed validation. |
| GitHub recovery preflight | [Run 36241389266](https://github.com/swbergmann/fintech-app/actions/runs/36241389266) passed using the environment's temporary AWS role. |
| GitHub application recovery | [Run 36241498910](https://github.com/swbergmann/fintech-app/actions/runs/36241498910) restored `6ad7f28388259d0ff2d35580b2805879ebb42664`; exact-image and Oracle smoke checks passed, and the additional fictional customer remained present. |
| Restore demonstration release | [Run 36241855045](https://github.com/swbergmann/fintech-app/actions/runs/36241855045) restored `059f96c…`; image/Oracle checks passed again. The fictional customer survived both recoveries and was then removed. |
| Refreshed DEV evidence | [DEV attempt 2](https://github.com/swbergmann/fintech-app/actions/runs/36239347845/attempts/2) passed after recovery. Its receipt matches the live task-definition revision and reuses the original CI images. |
| Reject mismatched prior release | [Run 36242238730](https://github.com/swbergmann/fintech-app/actions/runs/36242238730) failed because the supplied DEV run belongs to another SHA. UAT foundation, application and migration stack snapshots were unchanged. |
| Reject PROD without acceptance | [Run 36242867812](https://github.com/swbergmann/fintech-app/actions/runs/36242867812) failed at the human-acceptance gate; target authentication and deployment steps were skipped. |
| UAT verification interrupted | [Run 36242500117, attempt 1](https://github.com/swbergmann/fintech-app/actions/runs/36242500117/attempts/1) updated the UAT application, but the client's public IP changed during the run. HTTP access was blocked by the existing /32 restriction; the run was cancelled pending a stable connection and must not count as successful UAT evidence. |
| Internal DEV/UAT runtime | AWS API checks found both ECS services healthy on the selected release's exact image digests, with distinct environment resources. This does not replace HTTP smoke tests or a successful UAT workflow receipt. |
| Final status | Pending a stable client connection, successful UAT retry, human acceptance, PROD provisioning/promotion and the three-environment audit. PROD has not been provisioned. |

Packaging is main-only, so the absence of release artifacts on a PR does not,
by itself, prove that a failed main release was blocked. The historical run
demonstrates the React failure stopping subsequent build/check steps. The current
Deploy DEV workflow separately requires successful CI from a push to `main`;
this exercise does not deliberately merge failing application code into `main`.

### Resume after the network interruption

The historical runs above used an IP allowlist. The current provisioner opens the
application load balancer to public IPv4 access, and deployment, promotion,
recovery and audit scripts no longer require a stable runner address.
After merging this change, explicitly provision the environments needed for a
new session and use a CI/Deploy DEV run from the new commit. Existing stacks need
a CloudFormation update or recreation before their old firewall rule changes.

Complete a fresh DEV-to-UAT promotion, then record real human UAT acceptance
before PROD. Old cancelled attempts remain incident evidence and must not count
as successful promotion receipts. Use the normal infrastructure `delete` between
sessions; use **AWS final cleanup** only at the end of the project, after saving
the case-study evidence (see [cleanup instructions](../infra/aws/README.md#daily-sessions-versus-permanent-project-cleanup)).

## Limits of the evidence

- The active main ruleset requires a PR and prevents branch deletion/force-push.
  It currently requires **zero approving reviews** and has **no required status
  checks**. Do not claim that GitHub prevents merging failed checks. Deployment
  still depends on successful main CI and the workflow's own release gates.
- A manual PROD acceptance checkbox is an operator attestation, not proof that
  an independent tester participated.
- Runtime checks establish a dated observation, not continuous monitoring or
  future availability. DEV/UAT/PROD are separate resources within one lab account
  and region; this is not multi-account isolation or a high-availability design.
- The lab uses fictional data and a public HTTP endpoint. Authentication,
  production TLS/domain setup, load testing, operational alerting and database
  disaster recovery remain outside this demonstration.
- Scheduled cleanup is configured, but its future success and final invoiced
  costs must not be inferred from a passing deployment or a current cost estimate.
