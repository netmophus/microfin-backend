from fastapi import FastAPI

from app.modules.audit.router import router as audit_router
from app.modules.caisse.router import router as caisse_router
from app.modules.comptabilite.router import router as comptabilite_router
from app.modules.credit.engagements import enregistrer as enregistrer_engagements_credit
from app.modules.credit.router import router as credit_router
from app.modules.epargne.engagements import enregistrer as enregistrer_engagements_epargne
from app.modules.epargne.router import router as epargne_router
from app.modules.parameters.router import router as agences_router
from app.modules.parameters.router import (
    router_countries,
    router_currencies,
    router_doctypes,
    router_secteurs,
)
from app.modules.security.router import router as auth_router
from app.modules.security.router_roles import router as roles_router
from app.modules.security.router_users import router as users_router
from app.modules.tiers.parts_engagements import enregistrer as enregistrer_engagements_parts
from app.modules.tiers.router import router as tiers_router

app = FastAPI(title="Microfinance SIG", version="0.1.0")

# Branche les vérificateurs d'engagements des modules métier (désactivation d'un tiers). Ici, à
# l'assemblage de l'app, pour que le garde-fou ne soit jamais inerte. Un méta-test le vérifie.
enregistrer_engagements_epargne()
enregistrer_engagements_parts()
enregistrer_engagements_credit()

app.include_router(auth_router)
app.include_router(users_router)
app.include_router(agences_router)
app.include_router(router_countries)
app.include_router(router_currencies)
app.include_router(router_doctypes)
app.include_router(router_secteurs)
app.include_router(roles_router)
app.include_router(audit_router)
app.include_router(tiers_router)
app.include_router(epargne_router)
app.include_router(comptabilite_router)
app.include_router(credit_router)
app.include_router(caisse_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
