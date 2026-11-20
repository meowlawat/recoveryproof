"""Exercise the live Compose stack without printing the operator key."""
import os
import time
from pathlib import Path

import requests


base = os.getenv("RECOVERYPROOF_URL", "http://127.0.0.1:8000")
key = next(line.split("=", 1)[1].strip() for line in Path(os.getenv("RECOVERYPROOF_ENV_FILE", ".env")).read_text().splitlines() if line.startswith("RECOVERYPROOF_API_KEY="))
headers = {"X-API-Key": key}
assert requests.get(base + "/health").status_code == 200
assert requests.get(base + "/api/v1/targets").status_code == 401
assert requests.get(base + "/api/v1/targets", headers=headers).json()[0]["id"] == "target-demo-api"
assert requests.post(base + "/api/v1/runs", headers=headers, json={"target_id": "arbitrary"}).status_code == 422


def trial(objective, fault="container_termination", **overrides):
    response = requests.post(base + "/api/v1/runs", headers=headers, json={"recovery_objective_ms": objective, "fault_type": fault, **overrides})
    assert response.status_code == 202, response.text
    run_id = response.json()["id"]
    for _ in range(120):
        run = requests.get(f"{base}/api/v1/runs/{run_id}", headers=headers).json()
        if run["status"] in ("recovered", "timed_out", "failed", "cancelled"):
            break
        time.sleep(.5)
    else:
        raise AssertionError("Run did not finish")
    events = requests.get(f"{base}/api/v1/runs/{run_id}/events", headers=headers).json()
    report_json = requests.get(f"{base}/api/v1/runs/{run_id}/report?format=json", headers=headers)
    report_md = requests.get(f"{base}/api/v1/runs/{run_id}/report?format=markdown", headers=headers)
    assert report_json.status_code == report_md.status_code == 200
    assert report_json.json()["run"]["id"] == run_id
    assert "# RecoveryProof run" in report_md.text
    assert any(e["kind"] == "fault_applied" for e in events), events
    assert run["metrics"]["probe_count"] > 0, run
    print(run_id, run["status"], run["verdict"], run["metrics"])
    return run


passing = trial(30000)
assert passing["status"] == "recovered" and passing["verdict"] == "pass", passing
failing = trial(100)
assert failing["verdict"] == "fail", failing
dependency = trial(30000, "dependency_latency")
assert dependency["status"] == "recovered" and dependency["verdict"] == "pass", dependency
timeout = trial(30000, "dependency_latency", fault_duration_ms=10000, experiment_timeout_ms=2500)
assert timeout["status"] == "timed_out" and timeout["verdict"] == "fail", timeout
print("Live smoke test passed")
