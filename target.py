import asyncio
import hmac
import os
import time

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

app = FastAPI(title="RecoveryProof test workload")
slow_until = 0.0
slow_ms = 0


class Delay(BaseModel):
    duration_ms: int = Field(ge=500, le=10000)
    delay_ms: int = Field(ge=500, le=5000)


async def synthetic_dependency():
    if time.monotonic() < slow_until:
        await asyncio.sleep(slow_ms / 1000)


@app.get("/health")
async def health():
    await synthetic_dependency()
    return {"status": "healthy", "target_id": "target-demo-api"}


@app.get("/work")
async def work():
    await synthetic_dependency()
    return {"ok": True, "result": "synthetic-work-complete"}


@app.post("/internal/dependency-delay")
def dependency_delay(delay: Delay, x_hook_key: str | None = Header(default=None)):
    global slow_until, slow_ms
    key = os.getenv("RECOVERYPROOF_TARGET_HOOK_KEY", "")
    if not key or not x_hook_key or not hmac.compare_digest(key, x_hook_key):
        raise HTTPException(401, "Unauthorized")
    slow_ms = delay.delay_ms
    slow_until = time.monotonic() + delay.duration_ms / 1000
    return {"status": "armed", "duration_ms": delay.duration_ms, "delay_ms": delay.delay_ms}
