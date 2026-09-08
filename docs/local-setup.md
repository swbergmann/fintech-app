# Local setup

## Prerequisites

- Node.js **24 LTS** and npm. The `.nvmrc` selects Node 24 if you use nvm.
- Java **21**. Set `JAVA_HOME` to your Java 21 installation.
- Python **3.10 or newer**.
- Docker with **Compose v2 or newer**, running Linux containers. Docker Desktop is one option; Colima is another on macOS.
- Internet access for dependencies and initial container downloads.

Use a machine with at least 16 GB of memory; allocate approximately 12 GB to Docker when running all three environments together. Allow at least 25 GB of free disk space for Oracle images, three database volumes, build dependencies, and release images. These are planning estimates, not measured resource requirements for this project. You can stop environments when you are not using them.

The Oracle image is `container-registry.oracle.com/database/free:23.26.0.0-lite` (Oracle AI Database Free). Its published manifest supports Linux ARM64 and AMD64, so the same configuration can be used on Apple silicon and Intel/AMD hosts. No existing Oracle installation is required. Follow any Oracle image terms presented by your container tooling.

Check the prerequisites:

```bash
node --version
java -version
python3 --version
docker info
docker compose version
python3 scripts/lab.py doctor
```

On macOS, start Docker Desktop and wait until it is ready. If using an existing Colima installation, a typical command is:

```bash
colima start --cpu 4 --memory 12 --disk 40
```

If Docker reports `unknown command: docker compose` with a Homebrew installation, install the `docker-compose` package and follow Homebrew's instructions to expose its CLI plugin to Docker. Having the `docker` client alone does not provide a running engine or the Compose plugin.

If macOS reports that virtualization is unavailable, the current machine or VM cannot start the required Linux VM with that configuration. Enable supported nested virtualization in the host, or run the demo on a computer with a working Docker engine. Do not substitute an in-memory database and call that an Oracle test.

## First deployment

Open a terminal in the project folder and run:

```bash
./scripts/local-demo.sh
```

The script stops early if Docker is unavailable. The first successful run downloads dependencies and images and initializes Oracle, which can take several minutes. DEV opens at http://localhost:8080. Record the release ID printed by the script.

The script does not start UAT or PROD automatically. Promote the same release yourself:

```bash
python3 scripts/lab.py deploy --release YOUR_RELEASE_ID --environment uat
```

Open http://localhost:8081 and perform the acceptance checklist in the case study guide. Then:

```bash
python3 scripts/lab.py deploy --release YOUR_RELEASE_ID --environment prod --accept-uat
```

Open http://localhost:8082. All three pages should show the same release ID and different environment labels. Their customer records remain separate.

## What is isolated

Each environment has a separate Compose project (`deliverylab-dev`, `deliverylab-uat`, `deliverylab-prod`), private Docker network, Oracle container, named volume, Java container, frontend container, and generated passwords. Only the frontend port is published, bound to `127.0.0.1`. Java and Oracle have no host-published ports.

All containers share the same Docker host. This is isolation for a learning exercise, not a substitute for separate production infrastructure.

Credentials are created in `.local/env/*.env` with file permissions `0600`. `lab.py init` preserves existing credentials. Keep these files with their corresponding database volumes; rerunning initialization does not rotate passwords in an existing database. Never commit them or real customer data.

## Database changes

Add a new migration under `backend/src/main/resources/db/migration/`, for example `V2__add_created_at.sql`. Do not edit an applied migration. Flyway tracks versions and checksums in each environment independently.

Deployment starts Oracle, waits for both database health and successful application-user creation, runs the release's migration container to completion, then starts the application. A failed migration or smoke test fails the deployment and does not create promotion evidence.

There is no automatic database rollback. Oracle DDL can commit independently, so a failed multi-statement migration may leave partial changes. Investigate and make a forward correction, or restore a tested backup. The case study guide proposes this as a recovery exercise.

## Run checks without Oracle

```bash
npm --prefix frontend ci
npm --prefix frontend test
npm --prefix frontend run build
./backend/mvnw -f backend/pom.xml -B -ntp verify
python3 -m unittest discover -s tests -v
```

These check the frontend, Java API behavior using a test repository, and pipeline rules. The deployment smoke test is the additional check that exercises real Oracle.

## Work on the frontend

After a working DEV deployment, run:

```bash
npm --prefix frontend run dev
```

Open the printed local URL (normally http://127.0.0.1:5173). The frontend development server forwards `/api` requests to DEV at port 8080. Code changes reload the interface. Building and promoting the complete release still happens through the pipeline.

## Stop and inspect

```bash
python3 scripts/lab.py status
python3 scripts/lab.py stop --environment dev
python3 scripts/lab.py stop --environment uat
python3 scripts/lab.py stop --environment prod
```

Stopping retains the Oracle volume. Status shows the last successful deployment receipt; it is not a live health monitor. Visit an environment's `/api/health` endpoint or inspect Docker for current health. Receipt JSON lives under `.local/history/`; failed attempts live under `.local/failures/`.

To retain a different storage directory, set `LAB_HOME` to an absolute path before **all** commands. For GitHub, use the path configured in the repository variable consistently. The three Docker project names and host ports are fixed, so use one installation of this demo per Docker engine at a time.
