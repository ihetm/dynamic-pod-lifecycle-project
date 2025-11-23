# worker/main.py

from fastapi import FastAPI
from prometheus_client import start_http_server, Gauge
import os, time, threading, redis, logging
from urllib.parse import urlparse

app = FastAPI()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("worker")

# ----------------------------------------------------------------------
# SESSION ID (set by operator)
# ----------------------------------------------------------------------
SESSION_ID = os.getenv("SESSION_ID", "unknown")

# ----------------------------------------------------------------------
# REDIS HOST + PORT
# ----------------------------------------------------------------------
RAW_REDIS_HOST = os.getenv("REDIS_HOST", "redis.session-system.svc.cluster.local")
RAW_REDIS_PORT = os.getenv("REDIS_PORT", "6379")

def parse_redis(host_raw: str, port_raw: str):
    host = host_raw
    port = None

    try:
        parsed = urlparse(host_raw)
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
            port = int(port_raw)
        except Exception:
            try:
                parsed2 = urlparse(port_raw)
                if parsed2.scheme and parsed2.netloc and ":" in parsed2.netloc:
                    _, p = parsed2.netloc.split(":", 1)
                    port = int(p)
                elif ":" in port_raw:
                    _, p = port_raw.split(":", 1)
                    port = int(p)
            except Exception:
                port = None

    if port is None:
        port = 6379

    return host, int(port)

REDIS_HOST, REDIS_PORT = parse_redis(RAW_REDIS_HOST, RAW_REDIS_PORT)
logger.info(f"Worker using Redis at {REDIS_HOST}:{REDIS_PORT}")

# ----------------------------------------------------------------------
# REDIS CLIENT (startup test)
# ----------------------------------------------------------------------
try:
    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0, decode_responses=True)
    r.ping()
    logger.info("Worker connected to Redis successfully")
except Exception as e:
    logger.error(f"Worker failed to connect to Redis: {e}")
    raise

# ----------------------------------------------------------------------
# PROMETHEUS METRIC
# ----------------------------------------------------------------------
last_activity = time.time()
LAST_ACTIVITY = Gauge(
    "session_last_request_timestamp",
    "Last time this session pod received traffic",
    ["session"]
)
LAST_ACTIVITY.labels(session=SESSION_ID).set(last_activity)

# ----------------------------------------------------------------------
# /process — MAIN ENDPOINT
# ----------------------------------------------------------------------
@app.get("/process")
def process(session_id: str):
    """
    Called every time router forwards a request.
    """
    global last_activity
    last_activity = time.time()

    # Prometheus metric
    LAST_ACTIVITY.labels(session=SESSION_ID).set(last_activity)

    # NEW CORRECT REDIS KEY FORMAT
    pod_name = os.getenv("HOSTNAME")

    try:
        # 🟩 FIX: use colon key format to match router & operator
        r.set(f"{pod_name}:last_active", int(last_activity))
    except Exception:
        logger.exception("Redis update failed for worker pod")

    return {
        "session": str(session_id),
        "pod": pod_name,
        "message": f"Processed by worker for session {SESSION_ID}"
    }

# ----------------------------------------------------------------------
# HEALTHCHECK
# ----------------------------------------------------------------------
@app.get("/healthz")
def health():
    return {"ok": True}

# ----------------------------------------------------------------------
# Start Prometheus metrics server
# ----------------------------------------------------------------------
def start_metrics():
    start_http_server(8000)

threading.Thread(target=start_metrics, daemon=True).start()
