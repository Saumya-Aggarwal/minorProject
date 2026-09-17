import sys
from pathlib import Path

from dotenv import load_dotenv

# Repo root on sys.path so `common` (a sibling of backend/) is importable
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Load backend/.env before anything reads os.environ
load_dotenv(Path(__file__).parent / ".env")

from contextlib import asynccontextmanager  # noqa: E402

import httpx  # noqa: E402
from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel  # noqa: E402
from starlette.middleware.sessions import SessionMiddleware  # noqa: E402

from auth import session_secret  # noqa: E402
from db import init_db  # noqa: E402
from routers import api, store, webhook  # noqa: E402
from whatsapp import send_whatsapp_message  # noqa: E402


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Create missing tables. Postgres being down must not block the webhook."""
    try:
        init_db()
        print("[db] tables ready")
    except Exception as exc:
        print(f"[db] init skipped: {exc!r}")
    yield


app = FastAPI(title="WhatsApp Shopping Assistant", lifespan=lifespan)

# Signed cookie session for the storefront. The bot has no session of its own —
# link_tokens are what bridge a browser login to a WhatsApp number.
app.add_middleware(
    SessionMiddleware,
    secret_key=session_secret(),
    same_site="lax",
    https_only=False,  # ngrok serves https, but local dev is plain http
)

app.mount(
    "/static",
    StaticFiles(directory=str(Path(__file__).parent / "static")),
    name="static",
)

app.include_router(webhook.router)
app.include_router(api.router)
# Last: its "/" route would otherwise shadow the API and webhook paths
app.include_router(store.router)


@app.get("/health")
async def health():
    return {"status": "ok"}


class SendTestRequest(BaseModel):
    to: str
    body: str


@app.post("/dev/send-test")
async def send_test(req: SendTestRequest):
    """Dev-only: exercise send_whatsapp_message() from curl. Remove before deploying."""
    try:
        return await send_whatsapp_message(req.to, req.body)
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=e.response.json())
