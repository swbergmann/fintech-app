# Verification record

Prepared on 8 September 2026. These results describe the delivered source; they do not claim a live GitHub or Oracle deployment.

| Check | Observed result |
|---|---|
| Frontend dependency installation from committed lockfile | Passed using Node.js 24.20.0. |
| React interaction tests | 3 passed: add/remove, unavailable API, failed deletion preserves the visible record. |
| React production build | Passed using Vite 8.2.2. |
| Java build through the checked-in Maven wrapper | Passed using Java 21 and Spring Boot 4.1.1. |
| Java API tests | 2 passed: create/list/delete and invalid input rejection. Uses an in-memory test implementation of the repository; does not claim an Oracle integration test. |
| Pipeline tests | 11 passed, including required previous-stage evidence, human UAT acceptance, changed image evidence, artifact tampering, failure blocking, safe extraction, and separate preserved credentials. |
| GitHub Actions workflow syntax and expressions | Passed actionlint 1.7.12 with the declared custom runner label. ShellCheck and Pyflakes were not run by actionlint; Python compilation and Bash syntax were checked separately. |
| DEV/UAT/PROD Compose configuration | Parsed successfully with Compose 5.5.1; checked separate Oracle volume names, environment labels, localhost bindings and ports, and no published Oracle/Java ports. |
| Release packaging | Built an archive, extracted it using the deployment code, preserved executable scripts, and verified all 10 manifest checksums. |
| Oracle image architecture | The published manifest for `23.26.0.0-lite` contains Linux ARM64 and AMD64 variants. This is an image metadata check, not a database startup test. |
| Docker runtime image builds and Oracle startup | Not executed successfully: the current macOS environment reported that virtualization is unavailable when starting the Linux VM. |
| End-to-end application smoke test against Oracle | Included in every deployment; not executed here because the Docker engine could not start. |
| Live GitHub Actions runs, PR settings and approvals | Not executed or configured: this is a source project ready for you to upload, as requested. |
| Browser interaction test against the complete stack | Not performed; frontend interactions were tested through Vitest, and the complete stack requires running Oracle. |

Total automated tests passed: **16**. Configuration validation and archive checks are additional to that count.

## First checks on your computer

1. Make `docker info` and `docker compose version` succeed, then run `python3 scripts/lab.py doctor`.
2. Run `./scripts/local-demo.sh` and record the real DEV migration and smoke-test results.
3. Promote the printed release to UAT, complete human acceptance testing, and promote to PROD with the acceptance flag.
4. Verify the same release appears in all three environments and records remain separate.
5. Upload to GitHub, configure the runner and repository rules, and repeat using a real pull request.

Record any environment-specific fixes before treating the stack as fully validated. Keep real observations separate from proposed security improvements in the case study.
