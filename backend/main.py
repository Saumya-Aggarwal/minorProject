from pathlib import Path

from dotenv import load_dotenv

# Load backend/.env before anything reads os.environ
load_dotenv(Path(__file__).parent / ".env")

from contextlib import asynccontextmanager  # noqa: E402

import httpx  # noqa: E402
from fastapi import FastAPI, HTTPException  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from db import init_db  # noqa: E402
from routers import webhook  # noqa: E402
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
app.include_router(webhook.router)


@app.get("/")
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
