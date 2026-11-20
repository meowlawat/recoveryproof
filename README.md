# RecoveryProof

Live Azure demo: <https://recoveryproof-demo-7f2a.centralindia.cloudapp.azure.com>. The Azure operator key is stored in the local `.env.azure` file and on the VM in `~/RecoveryProof/.env`; neither belongs in source control.

A small, working resilience lab built from `ppt3.pdf` and `RecoveryProof_Project_Report.pdf`. It runs one fixed Docker test workload, injects a bounded fault, records health probes and lifecycle evidence, evaluates detection/recovery limits, and exports JSON or Markdown reports. The slide deck's example numbers are **not** used as results.

## Start locally

Requirements: Docker Engine with the Compose plugin.

```powershell
cd E:\RecoveryProof
python -c "import secrets,pathlib; p=pathlib.Path('.env'); p.write_text('RECOVERYPROOF_API_KEY='+secrets.token_urlsafe(32)+'\n') if not p.exists() else None"
docker compose up -d --build
python tests/smoke.py
```

Open <http://127.0.0.1:8000>. Enter the key from `.env`. The key is kept in browser session storage. The dashboard and API share one origin. The controller is bound to localhost locally; the target is reachable only on the Compose network.

If `.env` already exists, keep it. Run `docker compose ps` for status and `docker compose logs controller target` for diagnostics. `docker compose down` stops the stack without deleting its named database volume.

## Experiment behavior

- **Container termination:** A fixed command kills the synthetic app process in the labeled target container. Its supervisor exits; Docker's `restart: always` starts the container again. This measures container restart, not host or ECS recovery.
- **Dependency latency:** An authenticated internal hook slows the synthetic dependency for 3 seconds, then automatically resets. The health and work endpoints experience the delay.
- Baseline requires three successful probes. Probes run at a nominal 250 ms interval, with a 1 second default timeout. Recovery requires three consecutive healthy responses.
- TTD is first failed probe minus fault initiation; TTR is verified recovery minus fault initiation. Monotonic time supplies durations, UTC supplies event timestamps. Missing recovery fails; runner errors are inconclusive.
- One experiment runs at a time. The target ID, fault types, durations, and timeouts are bounded server side. The fault API requires `X-API-Key`.

The API contract is available at `/docs`. Routes include `/health`, `/api/v1/targets`, `/api/v1/runs`, `/api/v1/runs/{id}`, `/api/v1/runs/{id}/events`, `/api/v1/runs/{id}/report?format=json|markdown`, and `/api/v1/runs/{id}/cancel`. Run history uses a persistent SQLite named volume.

## Azure deployment

The `compose.azure.yaml` overlay adds a Caddy HTTPS gateway for a single isolated Azure Linux VM. Give the VM a public DNS name, allow ports 80 and 443, install Docker with Compose, and copy the source files. Enable a system-assigned managed identity and grant it `Monitoring Metrics Publisher` on the VM resource. Create a fresh `.env` with `RECOVERYPROOF_API_KEY`, `RECOVERYPROOF_DOMAIN`, `AZURE_LOCATION`, and `AZURE_RESOURCE_ID`, then run:

```sh
docker compose -f compose.yaml -f compose.azure.yaml up -d --build
```

Check `https://<domain>/health`, run both fault types, and export the reports. The controller publishes `TTRMilliseconds` and `SLAViolation` to Azure Monitor's `RecoveryProof` namespace using the VM identity. It also prints structured JSON events to container logs. The Docker socket grants the controller host level Docker control; use an isolated test VM and keep the key private. Do not point this project at production workloads.

Current deployment: resource group `rg-recoveryproof-demo`, VM `recoveryproof-vm` in Central India, Ubuntu 22.04, `Standard_B2ls_v2`, Azure DNS hostname above. Only HTTP and HTTPS are allowed inbound; SSH was closed after deployment. Docker was installed using `deploy/install-docker.sh`. This VM's Docker build could not fetch packages from PyPI, so images were built locally, transferred with `docker save`/`docker load`, and started with `--no-build`. The Azure VM and public IP consume subscription credit while running.

The `RecoveryProof-SLA-Violation` Azure Monitor metric alert is enabled for `SLAViolation >= 1` over five minutes. It has no notification action group. Azure Monitor Agent and a Log Analytics workspace have not been configured; structured container logs remain on the VM and experiment evidence is available in persistent history and exports.

## Local verification

`python tests/smoke.py` checks authentication, rejected targets, two real Docker restart verdicts, dependency latency, event persistence, and both report formats. A local Docker run on 20 Nov 2026 measured container TTR 1.83 seconds under a 30 second objective and 1.84 seconds under a deliberately strict 0.1 second objective; the dependency run measured 3.62 seconds. These are individual local trials, not cloud results or performance guarantees.

The same smoke suite passed against the Azure URL on 20 Nov 2026. The Azure container runs measured TTR 1.53 seconds (pass under 30 seconds) and 1.49 seconds (fail under 0.1 seconds). The dependency run measured 3.59 seconds and passed; a 2.5 second timeout trial failed as expected. History was confirmed after restarting the Azure controller. These are single trials, not general performance claims.
