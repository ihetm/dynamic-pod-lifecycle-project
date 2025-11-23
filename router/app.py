import asyncio
from fastapi import FastAPI, HTTPException
import redis, os, time, httpx, logging
from urllib.parse import urlparse

app = FastAPI()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("router")

# ----------------------------------------------------------------------
# REDIS HOST/PORT HANDLING
# ----------------------------------------------------------------------

RAW_REDIS_HOST = os.getenv("APP_REDIS_HOST", "redis.session-system.svc.cluster.local")
RAW_REDIS_PORT = os.getenv("APP_REDIS_PORT", "6379")


def parse_redis(RAW_HOST: str, RAW_PORT: str):
    host = RAW_HOST
    port = None

    try:
        parsed = urlparse(RAW_HOST)
        if parsed.scheme and parsed.netloc:
            if ":" in parsed.netloc:
                h, p = parsed.netloc.split(":", 1)
                host = h
                port = int(p)
            else:
                host = parsed.netloc
    except Exception:
        pass

    if port is None:
        try:
            port = int(RAW_PORT)
        except Exception:
            try:
                parsed2 = urlparse(RAW_PORT)
                if parsed2.scheme and parsed2.netloc:
                    if ":" in parsed2.netloc:
                        _, p = parsed2.netloc.split(":", 1)
                        port = int(p)
                elif ":" in RAW_PORT:
                    _, p = RAW_PORT.split(":", 1)
                    port = int(p)
            except Exception:
                port = None

    if port is None:
        port = 6379

    return host, port


REDIS_HOST, REDIS_PORT = parse_redis(RAW_REDIS_HOST, RAW_REDIS_PORT)

# ----------------------------------------------------------------------
# OPERATOR SERVICE
# ----------------------------------------------------------------------

OPERATOR_SVC = os.getenv(
    "OPERATOR_SVC",
    "http://session-operator.session-system.svc.cluster.local:8085"
).rstrip("/")

WORKER_PORT = int(os.getenv("WORKER_PORT", "8080"))

# ----------------------------------------------------------------------
# REDIS CLIENT
# ----------------------------------------------------------------------

try:
    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0, decode_responses=True)
    r.ping()
    logger.info("Router connected to Redis at %s:%s", REDIS_HOST, REDIS_PORT)
except Exception as exc:
    logger.error("Router failed connecting to Redis at startup: %s", exc)
    raise


# ----------------------------------------------------------------------
# INTERNAL HELPER – FORWARD TO WORKER POD
# ----------------------------------------------------------------------

async def forward_to_worker(pod_ip: str, session_id: str):
    url = f"http://{pod_ip}:{WORKER_PORT}/process?session_id={session_id}"
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.json()


# ----------------------------------------------------------------------
# MAIN PROCESS ENDPOINT
# ----------------------------------------------------------------------

@app.get("/process")
async def process(session_id: str):

    # 1. Check Redis if session has pod
    try:
        pod_name = r.get(session_id)
    except Exception:
        logger.exception("Redis GET failed")
        raise HTTPException(status_code=500, detail="redis error")

    # ------------------------------- CASE 1: POD EXISTS -------------------------------------
    if pod_name:
        pod_name = pod_name.strip()

        # refresh last active
        try:
            now = int(time.time())
            r.set(f"{pod_name}:last_active", now)
        except Exception:
            logger.exception("failed to refresh last_active")

        # ask operator for pod IP
        async with httpx.AsyncClient(timeout=5.0) as client:
            try:
                resp = await client.get(f"{OPERATOR_SVC}/pod_ip?pod_name={pod_name}")
                if resp.status_code == 200:
                    pod_ip = resp.json().get("pod_ip")
                    if pod_ip:
                        return await forward_to_worker(pod_ip, session_id)
            except Exception:
                logger.exception("failed to fetch pod ip")

        raise HTTPException(status_code=503, detail="pod exists but not reachable")

    # ------------------------------- CASE 2: CREATE NEW POD -------------------------------------
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.post(
                f"{OPERATOR_SVC}/create_session",
                json={"session_id": session_id},
            )

            if resp.status_code not in (200, 201):
                logger.error("operator create returned %s", resp.status_code)
                raise HTTPException(status_code=500, detail="operator create failed")

            data = resp.json()

            if isinstance(data, list) and len(data) > 0:
                data = data[0]

            pod_name = data.get("pod")
            if not pod_name:
                raise HTTPException(status_code=500, detail="operator create failed")

        except Exception:
            logger.exception("operator create failed")
            raise HTTPException(status_code=500, detail="operator create failed")

    # Wait for pod IP
    async with httpx.AsyncClient(timeout=5.0) as client:
        for _ in range(30):
            try:
                resp = await client.get(f"{OPERATOR_SVC}/pod_ip?pod_name={pod_name}")
                if resp.status_code == 200:
                    pod_ip = resp.json().get("pod_ip")
                    if pod_ip:
                        now = int(time.time())
                        r.set(f"{pod_name}:last_active", now)
                        r.set(session_id, pod_name)
                        return await forward_to_worker(pod_ip, session_id)
            except Exception:
                pass

            await asyncio.sleep(1)

    raise HTTPException(status_code=504, detail="timed out waiting for pod")


# ----------------------------------------------------------------------
# HEALTHCHECK
# ----------------------------------------------------------------------

@app.get("/healthz")
def health():
    return {"ok": True}
