# api-gateway/app.py

from fastapi import FastAPI, HTTPException
import os, httpx, logging

app = FastAPI()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("api-gateway")

# ----------------------------------------------------------------------
# ROUTER SERVICE URL
# ----------------------------------------------------------------------
ROUTER_URL = os.getenv("ROUTER_URL", "http://router").rstrip("/")


# ----------------------------------------------------------------------
# ROOT ENDPOINT -> fixes 404 spam in logs
# ----------------------------------------------------------------------
@app.get("/")
def root():
    return {"message": "api-gateway running"}


# ----------------------------------------------------------------------
# /process (ENTRYPOINT)
# ----------------------------------------------------------------------
@app.get("/process")
async def process(session_id: str):

    url = f"{ROUTER_URL}/process?session_id={session_id}"

    max_retries = 3
    timeout = 10.0

    for attempt in range(1, max_retries + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(url)
                response.raise_for_status()
                return response.json()

        except Exception as exc:
            logger.warning(
                f"Router call failed (attempt {attempt}/{max_retries}) — {exc}"
            )

            if attempt == max_retries:
                raise HTTPException(
                    status_code=502,
                    detail=f"Router unavailable: {str(exc)}"
                )


# ----------------------------------------------------------------------
# /healthz
# ----------------------------------------------------------------------
@app.get("/healthz")
def health():
    return {"ok": True}
