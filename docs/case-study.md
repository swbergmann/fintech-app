# Case study walkthrough

Treat this project as the initial, deliberately limited implementation for an eight-person team. The app is a vehicle for demonstrating changes, tests, migrations, reviews, releases, and operational feedback.

## Suggested demonstration

| Step | Human action | Automated evidence |
|---|---|---|
| 1. Establish a baseline | Push the project and configure the runner. | CI results, release archive, first DEV deployment. |
| 2. Open a small change | Create a feature branch, change the heading, push, and open a PR. | Early Semgrep scan, React tests, Java tests, CodeQL analysis, and the security gate. |
| 3. Demonstrate review feedback | Ask another developer to request one change; update the same branch. | The same PR updates and its checks rerun. |
| 4. Merge | Reviewer approves and the author merges. | Main is tested; one release is packaged and deployed to DEV. |
| 5. Promote | Release owner requests UAT using the exact SHA. | Previous-environment evidence, unchanged image IDs, UAT deployment and smoke test. |
| 6. Accept and release | Business tester records UAT results; release owner requests PROD with acceptance confirmed. | Recorded requester, release ID, PROD deployment and smoke test. |
| 7. Show a blocked release | In a separate PR, deliberately break an existing test. | CI fails; do not merge. Show that the failed candidate never reaches DEV. |
| 8. Show gate behavior | Try to promote a release that has not passed DEV or UAT; omit UAT acceptance for PROD. | The promotion script fails with a clear reason. |

Capture actual screenshots and logs from your own runs. Label this repository's automated test results separately from the live GitHub and Oracle results you collect. Do not invent incident or performance measurements.

## Human UAT checklist

1. Confirm the environment label is UAT and the release matches DEV.
2. Add a fictional customer such as Alex Example / alex@example.com.
3. Reload the page and confirm the record persists in Oracle.
4. Remove that customer and reload again.
5. Confirm an invalid email cannot be submitted.
6. Confirm a record created only in DEV is absent from UAT.
7. Record tester, release SHA, date, actual outcome, and any defects before requesting PROD.

The automated smoke test covers the HTTP/database path. Human UAT assesses the actual interface and expected behavior. Neither is a replacement for the other.

## Small database-change exercise

Add a new file named `V2__add_created_at.sql` under the migration directory:

```sql
ALTER TABLE customers ADD (
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL
);
```

This is an exercise to implement and verify, not an already-applied migration. Run it through the same pull request, DEV, UAT, and PROD process. Observe Flyway's per-environment history and checksums. Existing application queries name their columns explicitly, so an additive column is designed to remain compatible with the previous application version; verify that assumption in your runs.

## Baseline and possible improvements

| Area | Included baseline | Case study improvement and evidence |
|---|---|---|
| CI | Semgrep SAST before tests/builds, blocking any reported finding; frontend interaction tests, Java tests, application builds, then CodeQL SAST with a gate blocking high/critical findings. Promotion-rule tests remain commented out in CI for the current stage. | Add secret scanning and dependency vulnerability gates. Demonstrate a security finding blocking a release. |
| Code review | PR template; instructions for requiring one reviewer. | Add a focused security checklist and ownership rules. Record a defect found during review. |
| Releases | Main SHA, checksum manifest, recorded runtime image IDs, no rebuilding during promotion. | Add signed provenance, SBOMs, digest-pinned base images, and verified artifacts. |
| Oracle | Versioned migrations, validation before application deployment, separate database volumes and passwords. | Separate migration and runtime users; give the runtime user only needed CRUD grants. Test backups and forward recovery. |
| Data protection | Synthetic records, parameterized SQL, input validation, localhost-only HTTP access. | Add TLS, encryption/key management, and authenticated users with server-enforced roles; add negative authorization tests. |
| Approval | Authorized manual promotion and UAT attestation. | Add independent environment reviewers where supported; demonstrate a rejected deployment and prevention of self-approval. |
| Monitoring | Health endpoint and post-deployment smoke tests. | Configure Nagios checks for `/api/health`, HTTP response time, memory, disk, and service availability. Record thresholds and real alert times. |
| Security logs | Standard application/container logs; no centralized security event collection. | Add structured audit events, data redaction, protected centralized storage, and incident alert rules. |
| Recovery | Failed releases block further promotion; no automatic rollback. | Design application rollback and database recovery separately. Measure recovery time in a controlled exercise. |

The app currently has no login or role-based authorization and serves HTTP locally. It is a learning baseline, not a financial-services production system. Keep its scope clear when evaluating the security improvements.

## Metrics you can collect

- CI duration and failure rate.
- Time from merge to successful DEV deployment.
- Human waiting time before UAT and PROD.
- Number of findings caught by tests, scanning, and review.
- Unchanged frontend/backend release IDs and runtime image IDs across environments.
- Successful and rejected promotions, with the reason for rejection.
- Detection and recovery time after adding monitoring and an incident exercise.

Use one simple traceability table in the assignment: **risk → proposed control → change → test evidence → result → residual limitation**. Keep implementation screenshots selective so the assignment remains within 15 pages.

Framework reference: [NIST Secure Software Development Framework](https://csrc.nist.gov/pubs/sp/800/218/final). Monitoring reference: [official Nagios documentation](https://www.nagios.org/documentation/).
