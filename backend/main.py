from pathlib import Path

from dotenv import load_dotenv

# Load backend/.env before anything reads os.environ
load_dotenv(Path(__file__).parent / ".env")

import httpx  # noqa: E402
from fastapi import FastAPI, HTTPException  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from routers import webhook  # noqa: E402
from whatsapp import send_whatsapp_message  # noqa: E402

app = FastAPI(title="WhatsApp Shopping Assistant")
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
