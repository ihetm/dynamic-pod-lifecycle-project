# session_operator.py
import kopf
import logging
import time
import redis
import os
from kubernetes import client, config
from fastapi import FastAPI, Request
from threading import Thread
from datetime import datetime, timedelta

# -----------------------------------------------------------------------------------
# LOGGING
# -----------------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("session-operator")

# -----------------------------------------------------------------------------------
# LOAD IN-CLUSTER CONFIG
# -----------------------------------------------------------------------------------
try:
    config.load_incluster_config()
except Exception:
    config.load_kube_config()

core = client.CoreV1Api()
apps = client.AppsV1Api()

# -----------------------------------------------------------------------------------
# ENVIRONMENT VARIABLES
# -----------------------------------------------------------------------------------
NAMESPACE = os.getenv("NAMESPACE", "session-system")
RAW_REDIS_HOST = os.getenv("APP_REDIS_HOST", "redis.session-system.svc.cluster.local")
RAW_REDIS_PORT = os.getenv("APP_REDIS_PORT", "6379")
WORKER_IMAGE = os.getenv("WORKER_IMAGE")
IDLE_TIMEOUT = int(os.getenv("IDLE_TIMEOUT", "600"))

if not WORKER_IMAGE:
    raise RuntimeError("WORKER_IMAGE must be set")

# -----------------------------------------------------------------------------------
# REDIS CONNECTION
# -----------------------------------------------------------------------------------
def parse_redis(host_raw: str, port_raw: str):
    from urllib.parse import urlparse

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
    except:
        pass

    if port is None:
        try:
            port = int(port_raw)
        except:
            try:
                parsed2 = urlparse(port_raw)
                if parsed2.scheme and parsed2.netloc and ":" in parsed2.netloc:
                    port = int(parsed2.netloc.split(":")[1])
            except:
                port = 6379

    return host, port


REDIS_HOST, REDIS_PORT = parse_redis(RAW_REDIS_HOST, RAW_REDIS_PORT)
logger.info(f"Parsed Redis: {REDIS_HOST}:{REDIS_PORT}")

try:
    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
    r.ping()
    logger.info(f"Connected to Redis at {REDIS_HOST}:{REDIS_PORT}")
except Exception as exc:
    logger.error(f"Failed connecting to Redis: {exc}")
    raise

# -----------------------------------------------------------------------------------
# FASTAPI SERVER (OPERATOR API)
# -----------------------------------------------------------------------------------
app = FastAPI()

@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/pod_ip")
def pod_ip(pod_name: str):
    try:
        pod = core.read_namespaced_pod(name=pod_name, namespace=NAMESPACE)
        return {"pod_ip": pod.status.pod_ip}
    except Exception as exc:
        logger.error(f"get pod ip failed: {exc}")
        return {"pod_ip": None}

# -----------------------------------------------------------------------------------
# create_session ENDPOINT
# -----------------------------------------------------------------------------------
@app.post("/create_session")
async def create_session(request: Request):
    data = await request.json()
    session_id = data.get("session_id")

    if not session_id:
        return {"error": "missing session_id"}, 400

    pod_name = f"pod-session-{session_id}"

    # Check if pod already exists
    try:
        existing = core.list_namespaced_pod(
            namespace=NAMESPACE,
            label_selector=f"session={session_id}"
        )
        if existing.items:
            return {"pod": existing.items[0].metadata.name, "status": "exists"}, 200
    except Exception:
        pass

    manifest = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": pod_name,
            "labels": {
                "app": "session-worker",
                "session": session_id
            }
        },
        "spec": {
            "containers": [
                {
                    "name": "worker",
                    "image": WORKER_IMAGE,
                    "env": [
                        {"name": "SESSION_ID", "value": session_id},
                        {"name": "REDIS_HOST", "value": RAW_REDIS_HOST},
                        {"name": "REDIS_PORT", "value": str(REDIS_PORT)},
                    ],
                    "ports": [{"containerPort": 8080}]
                }
            ]
        }
    }

    logger.info(f"Creating worker pod {pod_name}")
    try:
        core.create_namespaced_pod(namespace=NAMESPACE, body=manifest)
    except client.exceptions.ApiException as e:
        if e.status == 409:
            return {"pod": pod_name, "status": "exists"}, 200
        raise

    return {"pod": pod_name, "status": "created"}, 201

# -----------------------------------------------------------------------------------
# POD EVENT WATCHER
# -----------------------------------------------------------------------------------
@kopf.on.event("pods")
def on_pod_event(event, **kwargs):
    obj = event.get("object")
    if not obj:
        return

    # obj is a dict, not a Kubernetes Pod object
    metadata = obj.get("metadata", {})
    status = obj.get("status", {})

    name = metadata.get("name")
    phase = status.get("phase")

    logger.info(f"[POD EVENT] {name}: {phase}")

# -----------------------------------------------------------------------------------
# IDLE CLEANER THREAD (DELETES PODS >10 MIN IDLE)
# -----------------------------------------------------------------------------------
def idle_cleaner():
    while True:
        try:
            now = time.time()

            pods = core.list_namespaced_pod(
                namespace=NAMESPACE,
                label_selector="app=session-worker"
            )

            for pod in pods.items:
                pod_name = pod.metadata.name

                # UPDATED REDIS KEY FORMAT
                last = r.get(f"{pod_name}:last_active")

                if not last:
                    continue  # no activity record yet

                last = int(last)

                if now - last > IDLE_TIMEOUT:
                    logger.info(f"Deleting idle pod {pod_name}")
                    try:
                        core.delete_namespaced_pod(
                            name=pod_name,
                            namespace=NAMESPACE,
                            body=client.V1DeleteOptions(),
                        )
                        # Clean Redis keys
                        r.delete(f"{pod_name}:last_active")
                    except Exception as exc:
                        logger.error(f"Failed deleting idle pod {pod_name}: {exc}")

        except Exception as exc:
            logger.error(f"idle cleaner error: {exc}")

        time.sleep(10)

Thread(target=idle_cleaner, daemon=True).start()

# -----------------------------------------------------------------------------------
# FASTAPI server inside KOPF
# -----------------------------------------------------------------------------------
def run_api():
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8085)

Thread(target=run_api, daemon=True).start()

# -----------------------------------------------------------------------------------
# KOPF STARTUP
# -----------------------------------------------------------------------------------
@kopf.on.startup()
def startup(**_):
    logger.info(f"Session operator starting — namespace={NAMESPACE}")

@kopf.on.probe(id="health")
def probe():
    return {"ok": True}
