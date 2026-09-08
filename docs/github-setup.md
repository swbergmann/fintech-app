# GitHub setup

This project is ready to push; no repository or account connection has been created for you. You can test locally before configuring GitHub.

## 1. Upload the source

Create an empty GitHub repository. A private repository is a sensible default when deployment runs on your own computer. Give access only to trusted teammates. From the project folder, substitute your actual repository URL below:

```bash
git init -b main
git add .
git commit -m "Add React Java Oracle delivery lab"
git remote add origin YOUR_GITHUB_REPOSITORY_URL
git push -u origin main
```

The first push runs **CI**. **Deploy DEV** may wait for a local runner until step 3 is complete. Do not upload `.local/`, passwords, `node_modules`, or `target`; the included `.gitignore` excludes them.

## 2. Configure review of main

Where supported by your repository plan, add a branch rule or ruleset for `main` that:

- Requires a pull request and one approval before merging.
- Dismisses stale approvals when new commits are pushed.
- Requires the **Build and test** status check.
- Blocks force pushes and deletion, and limits bypass permissions.

Run CI once before selecting its status check. The workflow checks pull requests automatically, but repository settings enforce review and merge restrictions. Private repository rules and deployment-review features depend on your GitHub plan. If a required rule is unavailable, document the review as a team convention; do not claim GitHub enforces it.

For eight developers, short feature branches, one reviewer, and one protected main branch are sufficient. DEV, UAT, and PROD are deployment environments, not separate long-lived Git branches.

## 3. Connect a local deployment runner

GitHub's hosted runners perform builds and tests. A **self-hosted runner** is a small GitHub program on your computer that receives deployment jobs and operates your local Docker engine. Your computer must be awake, online, and running Docker and the runner. No inbound internet port is needed for GitHub to dispatch jobs.

In **Settings → Actions → Runners → New self-hosted runner**, select your operating system and architecture (macOS / ARM64 for Apple silicon). Follow the exact download and registration commands GitHub displays. Use its current runner version. The registration token stays local; do not put it in the project.

- Install the runner in a separate directory, outside this source checkout.
- During registration, add the custom label **`delivery-lab`**.
- Ensure the runner user's `PATH` includes `docker` and `python3`, and that `docker info` and `docker compose version` both work in the terminal used to start the runner.
- Start it using GitHub's displayed command, normally `./run.sh`.
- If you use a non-default Docker context, set `DOCKER_CONTEXT` in that same terminal before starting the runner.

Docker service containers and Docker container actions on GitHub runners require Linux, so these workflows use ordinary shell commands to operate Docker Compose instead. That permits a macOS runner to deploy into its local Linux Docker VM.

Only trusted main-branch push results reach this local runner. Pull-request builds run on GitHub-hosted machines. A repository writer can change workflow code, so the local runner is still a trusted-team resource; repository review settings matter.

## 4. Set persistent storage

Choose an absolute directory on the runner computer outside the runner checkout, for example `/Users/YOUR_NAME/Projects/delivery-lab-state`. Create its parent directory if needed.

In **Settings → Secrets and variables → Actions → Variables**, add the repository variable:

| Variable | Value |
|---|---|
| `LAB_HOME` | Your chosen absolute directory |

Use the full path, not `$HOME`, `~`, or a relative path. The scripts require this variable inside Actions so checkout cleanup cannot remove credentials or deployment evidence. This is a repository variable, not an environment-specific variable, and is not a password.

No GitHub database secrets are needed for the baseline. The runner creates separate local Oracle credentials with restricted file permissions. It never prints the generated passwords.

For manual commands against these same environments, set the same value in your terminal:

```bash
export LAB_HOME=/YOUR/ABSOLUTE/delivery-lab-state
```

## 5. Run the normal development cycle

```bash
git switch -c feature/register-heading
# Make a small change and run the tests.
git add .
git commit -m "Improve customer register heading"
git push -u origin feature/register-heading
```

Open a pull request on GitHub; `git push` does not automatically create one. CI runs automatically. A reviewer can request changes, approve, or close the proposal. Further pushes to that feature branch update the same pull request and rerun CI.

After an approved merge to `main`:

1. **CI** builds and tests the merged code, creates a checksum manifest, and uploads `release-COMMIT_SHA`.
2. **Deploy DEV** downloads that exact run's artifact. Its checks exclude pull-request and fork results.
3. The local runner creates the release's runtime images once, migrates DEV, deploys, and runs the Oracle smoke test.
4. Open http://localhost:8080 on the runner computer. Copy the full 40-character release SHA from the successful workflow.

The build artifact is retained on GitHub for 30 days. Installed release files and images remain on the local runner until you remove them; promotion uses those installed copies.

## 6. Human promotion and UAT

Open **Actions → Promote release → Run workflow** and select branch `main`.

For UAT:

- Select `uat`.
- Paste the release SHA that passed DEV.
- Leave the UAT acceptance checkbox false; acceptance testing happens after UAT deployment.
- Run the workflow. It deploys the same installed images and applies pending migrations to UAT's own Oracle database.

Perform the acceptance tests at http://localhost:8081 and record their outcome in the pull request or your assignment evidence.

For PROD:

- Select `prod` and paste the same release SHA.
- Check the confirmation that human acceptance testing passed.
- Run the workflow.

Promotion fails if the release has no matching successful receipt from the previous environment. PROD also requires the UAT acceptance flag. The workflow actor, run ID, artifact manifest hash, image IDs, and outcome are recorded locally. **Manual dispatch is the human decision in this baseline; it does not require a separate person to approve the requester.** The checkbox is a human attestation, not an automated UAT test.

## 7. Optional improvement: enforced deployment reviewers

When your repository plan and visibility support required environment reviewers, create GitHub environments named `uat` and `prod`. Add required reviewers, prevent self-review, restrict deployments to `main`, and disable administrative bypass where available.

In the existing `promote` job, add this line at the same indentation as `runs-on`:

```yaml
environment: ${{ inputs.environment }}
```

Then the job waits for an independent GitHub environment approval before executing. Keep the existing release-order and UAT checks. **Modify the existing workflow; do not leave another promotion workflow that bypasses these approvals.**

On GitHub Free, Pro, and Team, required environment reviewers are available for public repositories; private-repository support differs by plan. Do not make a repository public solely to obtain this feature without considering the local runner. [GitHub's environment protection documentation](https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments) describes the current restrictions.

## Troubleshooting

| Symptom | Check |
|---|---|
| DEV waits for a runner | The runner is online and has both `self-hosted` and `delivery-lab` labels. |
| Docker is not ready | Start its engine; verify the runner terminal's Docker context and Compose plugin. |
| LAB_HOME error | Set an absolute repository variable and use the same directory for all deployment commands. |
| Cannot promote a release | Use the exact SHA that passed the previous environment on this runner; do not use a PR branch SHA. |
| Oracle startup times out | Check Docker memory, first-start logs, free disk, and whether the application-user setup succeeded. |
| Smoke test fails | Inspect the deployed API and container health. No successful receipt is written, so promotion is blocked. |
| Main builds queue up | One local deployment runs at a time. GitHub concurrency may replace older pending runs with newer ones; a release without a DEV pass cannot be promoted. |

Sources: [self-hosted runners](https://docs.github.com/en/actions/reference/runners/self-hosted-runners), [runner labels](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/use-in-a-workflow), [deployment reviews](https://docs.github.com/en/actions/how-tos/managing-workflow-runs-and-deployments/managing-deployments/reviewing-deployments).
