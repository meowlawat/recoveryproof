import asyncio
import hmac
import json
import os
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field


DB = Path(os.getenv("RECOVERYPROOF_DB", "data/runs.db"))
TARGET_URL = os.getenv("RECOVERYPROOF_TARGET_URL", "http://localhost:8001")
TARGET_CONTAINER = os.getenv("RECOVERYPROOF_TARGET_CONTAINER", "recoveryproof-target")
ACTIVE: dict[str, asyncio.Task] = {}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def connect():
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with connect() as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, status TEXT NOT NULL, config TEXT NOT NULL, metrics TEXT, verdict TEXT, error TEXT)")
        con.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, at TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL, FOREIGN KEY(run_id) REFERENCES runs(id))")
        con.execute("UPDATE runs SET status='interrupted', verdict='inconclusive', error='Controller restarted during experiment' WHERE status IN ('created','baseline','observing')")


def event(run_id, kind, detail=None):
    print(json.dumps({"at": now(), "run_id": run_id, "kind": kind, "detail": detail or {}}, separators=(",", ":")), flush=True)
    with connect() as con:
        con.execute("INSERT INTO events(run_id,at,kind,detail) VALUES(?,?,?,?)", (run_id, now(), kind, json.dumps(detail or {})))


def update(run_id, **fields):
    fields["updated_at"] = now()
    with connect() as con:
        con.execute(f"UPDATE runs SET {','.join(k+'=?' for k in fields)} WHERE id=?", [*(json.dumps(v) if k in ('metrics',) and v is not None else v for k, v in fields.items()), run_id])


def get_run(run_id):
    with connect() as con:
        row = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Run not found")
    item = dict(row)
    item["config"] = json.loads(item["config"])
    item["metrics"] = json.loads(item["metrics"]) if item["metrics"] else None
    return item


def get_events(run_id):
    with connect() as con:
        rows = con.execute("SELECT at,kind,detail FROM events WHERE run_id=? ORDER BY id", (run_id,)).fetchall()
    return [{"at": r["at"], "kind": r["kind"], "detail": json.loads(r["detail"])} for r in rows]


def authorize(x_api_key: str | None = Header(default=None)):
    key = os.getenv("RECOVERYPROOF_API_KEY", "")
    if not key or not x_api_key or not hmac.compare_digest(key, x_api_key):
        raise HTTPException(401, "Valid X-API-Key required")


class HealthRule(BaseModel):
    required_consecutive_successes: int = Field(default=3, ge=1, le=10)


class RunRequest(BaseModel):
    target_id: str = "target-demo-api"
    fault_type: str = "container_termination"
    recovery_objective_ms: int = Field(default=30000, ge=100, le=120000)
    detection_objective_ms: int = Field(default=2000, ge=100, le=30000)
    probe_interval_ms: int = Field(default=250, ge=100, le=2000)
    probe_timeout_ms: int = Field(default=1000, ge=100, le=5000)
    experiment_timeout_ms: int = Field(default=60000, ge=2000, le=180000)
    fault_duration_ms: int = Field(default=3000, ge=500, le=10000)
    health_rule: HealthRule = Field(default_factory=HealthRule)


def container():
    import docker
    client = docker.from_env(timeout=5)
    target = client.containers.get(TARGET_CONTAINER)
    target.reload()
    if target.labels.get("recoveryproof.scope") != "demo-target":
        raise RuntimeError("Target container lacks required scope label")
    return target


async def probe(client, timeout_ms):
    started = time.monotonic()
    try:
        response = await client.get(f"{TARGET_URL}/health", timeout=timeout_ms / 1000)
        data = response.json()
        ok = response.status_code == 200 and data.get("status") == "healthy" and data.get("target_id") == "target-demo-api"
        return ok, {"status_code": response.status_code, "latency_ms": round((time.monotonic()-started)*1000, 2)}
    except (httpx.HTTPError, ValueError) as exc:
        return False, {"error": type(exc).__name__, "latency_ms": round((time.monotonic()-started)*1000, 2)}


async def publish_azure_metrics(run_id, metrics, verdict):
    resource_id = os.getenv("AZURE_RESOURCE_ID", "")
    location = os.getenv("AZURE_LOCATION", "")
    if not resource_id or not location:
        return
    async with httpx.AsyncClient(trust_env=False, timeout=8) as client:
        token_response = await client.get(
            "http://169.254.169.254/metadata/identity/oauth2/token",
            params={"api-version": "2018-02-01", "resource": "https://monitoring.azure.com/"},
            headers={"Metadata": "true"},
        )
        token_response.raise_for_status()
        headers = {"Authorization": "Bearer " + token_response.json()["access_token"]}
        values = {"SLAViolation": 0 if verdict == "pass" else 1}
        if metrics["ttr_ms"] is not None:
            values["TTRMilliseconds"] = metrics["ttr_ms"]
        url = f"https://{location}.monitoring.azure.com/{resource_id.lstrip('/')}/metrics"
        for name, value in values.items():
            body = {"time": now(), "data": {"baseData": {"metric": name, "namespace": "RecoveryProof", "dimNames": [], "series": [{"dimValues": [], "min": value, "max": value, "sum": value, "count": 1}]}}}
            response = await client.post(url, headers=headers, json=body)
            response.raise_for_status()
        event(run_id, "azure_metrics_published", {"metrics": list(values)})


async def execute(run_id, cfg):
    start = None
    t1 = None
    t2 = None
    t3 = None
    observed = 0
    failed = 0
    streak = 0
    try:
        update(run_id, status="baseline")
        async with httpx.AsyncClient() as client:
            for _ in range(3):
                ok, detail = await probe(client, cfg.probe_timeout_ms)
                event(run_id, "baseline_probe", {"ok": ok, **detail})
                if not ok:
                    raise RuntimeError("Target baseline is unhealthy; no fault injected")
                await asyncio.sleep(cfg.probe_interval_ms / 1000)
            target = await asyncio.to_thread(container) if cfg.fault_type == "container_termination" else None
            original_started = target.attrs["State"]["StartedAt"] if target else None
            start = time.monotonic()
            event(run_id, "fault_requested", {"target_id": cfg.target_id, "fault_type": cfg.fault_type})
            if target:
                # Kill the fixed test process. Its supervisor exits, and
                # Docker applies the container's restart policy.
                result = await asyncio.to_thread(target.exec_run, ["python", "-c", "import os,signal; os.kill(int(open('/tmp/recoveryproof-app.pid').read()), signal.SIGKILL)"], user="10001")
                if result.exit_code != 0:
                    raise RuntimeError("Fault command failed: " + result.output.decode(errors="replace")[:160])
            else:
                response = await client.post(f"{TARGET_URL}/internal/dependency-delay", json={"duration_ms": cfg.fault_duration_ms, "delay_ms": max(cfg.probe_timeout_ms + 300, 500)}, headers={"X-Hook-Key": os.getenv("RECOVERYPROOF_TARGET_HOOK_KEY", "")}, timeout=5)
                response.raise_for_status()
            event(run_id, "fault_applied", {"fault_type": cfg.fault_type})
            update(run_id, status="observing")
            deadline = start + cfg.experiment_timeout_ms / 1000
            while time.monotonic() < deadline:
                tick = time.monotonic()
                ok, detail = await probe(client, cfg.probe_timeout_ms)
                observed += 1
                if not ok:
                    failed += 1
                    streak = 0
                    if t1 is None:
                        t1 = time.monotonic()
                        event(run_id, "failure_detected", detail)
                else:
                    streak += 1
                event(run_id, "probe", {"ok": ok, **detail})
                if target and t2 is None:
                    try:
                        target.reload()
                        if target.attrs["State"]["StartedAt"] != original_started and target.status == "running":
                            t2 = time.monotonic()
                            event(run_id, "container_restarted", {"started_at": target.attrs["State"]["StartedAt"]})
                    except Exception:
                        pass
                if t1 is not None and streak >= cfg.health_rule.required_consecutive_successes:
                    t3 = time.monotonic()
                    event(run_id, "health_restored", {"consecutive_successes": streak})
                    break
                await asyncio.sleep(max(0, cfg.probe_interval_ms / 1000 - (time.monotonic()-tick)))
        ttd = round((t1-start)*1000, 2) if t1 else None
        ttr = round((t3-start)*1000, 2) if t3 else None
        metrics = {"ttd_ms": ttd, "ttr_ms": ttr, "post_detection_recovery_ms": round((t3-t1)*1000, 2) if t3 and t1 else None,
                   "restart_observed_ms": round((t2-start)*1000, 2) if t2 else None,
                   "probe_count": observed, "failed_probes": failed, "error_rate": round(failed/observed, 4) if observed else None,
                   "probe_interval_nominal_ms": cfg.probe_interval_ms}
        verdict = "pass" if ttd is not None and ttr is not None and ttd <= cfg.detection_objective_ms and ttr <= cfg.recovery_objective_ms else "fail"
        status = "recovered" if t3 else "timed_out"
        update(run_id, status=status, metrics=metrics, verdict=verdict)
        event(run_id, "verdict", {"result": verdict, "metrics": metrics})
        if os.getenv("AZURE_RESOURCE_ID"):
            try:
                await publish_azure_metrics(run_id, metrics, verdict)
            except Exception as exc:
                event(run_id, "azure_metrics_error", {"error": type(exc).__name__})
    except asyncio.CancelledError:
        update(run_id, status="cancelled", verdict="inconclusive")
        event(run_id, "cancelled")
    except Exception as exc:
        update(run_id, status="failed", verdict="inconclusive", error=str(exc))
        event(run_id, "runner_error", {"error": str(exc)})
    finally:
        ACTIVE.pop(run_id, None)


@asynccontextmanager
async def lifespan(app):
    init_db()
    yield


app = FastAPI(title="RecoveryProof", version="1.0", lifespan=lifespan)


@app.get("/")
def home():
    return FileResponse(Path(__file__).with_name("index.html"))


@app.get("/health")
def health():
    return {"status": "healthy", "storage": "ready"}


@app.get("/api/v1/targets", dependencies=[Depends(authorize)])
def targets():
    return [{"id": "target-demo-api", "name": "Demo API", "fault_types": ["container_termination", "dependency_latency"]}]


@app.get("/api/v1/runs", dependencies=[Depends(authorize)])
def runs():
    with connect() as con:
        ids = [r["id"] for r in con.execute("SELECT id FROM runs ORDER BY created_at DESC LIMIT 50")]
    return [get_run(i) for i in ids]


@app.post("/api/v1/runs", status_code=202, dependencies=[Depends(authorize)])
async def create_run(cfg: RunRequest):
    if cfg.target_id != "target-demo-api" or cfg.fault_type not in ("container_termination", "dependency_latency"):
        raise HTTPException(422, "Unregistered target or unsupported fault type")
    if cfg.fault_type == "dependency_latency" and cfg.probe_timeout_ms + 300 > 5000:
        raise HTTPException(422, "Probe timeout is too high for dependency fault")
    if cfg.experiment_timeout_ms <= cfg.probe_timeout_ms + cfg.probe_interval_ms * cfg.health_rule.required_consecutive_successes:
        raise HTTPException(422, "Experiment timeout is too short for the probe rule")
    if ACTIVE:
        raise HTTPException(409, "Another experiment is running")
    run_id = str(uuid.uuid4())
    with connect() as con:
        con.execute("INSERT INTO runs(id,created_at,updated_at,status,config) VALUES(?,?,?,?,?)", (run_id, now(), now(), "created", cfg.model_dump_json()))
    event(run_id, "created", {"target_id": cfg.target_id})
    ACTIVE[run_id] = asyncio.create_task(execute(run_id, cfg))
    return get_run(run_id)


@app.get("/api/v1/runs/{run_id}", dependencies=[Depends(authorize)])
def run(run_id: str):
    return get_run(run_id)


@app.get("/api/v1/runs/{run_id}/events", dependencies=[Depends(authorize)])
def events(run_id: str):
    get_run(run_id)
    return get_events(run_id)


@app.get("/api/v1/runs/{run_id}/report", dependencies=[Depends(authorize)])
def report(run_id: str, format: str = Query(default="json", pattern="^(json|markdown)$")):
    item = get_run(run_id)
    doc = {"run": item, "events": get_events(run_id), "caveats": ["Only the fixed test workload is measured.", "Probe cadence is nominal; restart observation may be unavailable."]}
    if format == "json":
        return JSONResponse(doc, headers={"Content-Disposition": f'attachment; filename="recoveryproof-{run_id}.json"'})
    lines = [f"# RecoveryProof run {run_id}", "", f"Status: {item['status']}", f"Verdict: {item['verdict'] or 'pending'}", "", "## Configuration", "", "```json", json.dumps(item["config"], indent=2), "```", "", "## Metrics", "", "```json", json.dumps(item["metrics"], indent=2), "```", "", "## Timeline", ""]
    lines += [f"- {e['at']} · {e['kind']} · {json.dumps(e['detail'])}" for e in doc["events"]]
    lines += ["", "## Caveats", "", *[f"- {c}" for c in doc["caveats"]]]
    return PlainTextResponse("\n".join(lines), media_type="text/markdown", headers={"Content-Disposition": f'attachment; filename="recoveryproof-{run_id}.md"'})


@app.post("/api/v1/runs/{run_id}/cancel", dependencies=[Depends(authorize)])
def cancel(run_id: str):
    get_run(run_id)
    task = ACTIVE.get(run_id)
    if not task:
        raise HTTPException(409, "Run is not active")
    task.cancel()
    return {"status": "cancelling", "run_id": run_id}
