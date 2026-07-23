from fastapi import FastAPI

from app.modules.audit.router import router as audit_router
from app.modules.parameters.router import router as agences_router
from app.modules.security.router import router as auth_router
from app.modules.security.router_roles import router as roles_router
from app.modules.security.router_users import router as users_router
from app.modules.tiers.router import router as tiers_router

app = FastAPI(title="Microfinance SIG", version="0.1.0")

app.include_router(auth_router)
app.include_router(users_router)
app.include_router(agences_router)
app.include_router(roles_router)
app.include_router(audit_router)
app.include_router(tiers_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
