from fastapi import FastAPI

from app.modules.security.router import router as auth_router

app = FastAPI(title="Microfinance SIG", version="0.1.0")

app.include_router(auth_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
