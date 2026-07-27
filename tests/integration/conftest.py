"""Charge TOUS les modèles ORM avant les tests d'intégration.

Sans ça, un test qui n'importe que les modèles d'un module (ex. épargne) laisse des FK
inter-schémas non résolues (epargne.accounts.tier_id -> tiers.tiers) : le mapping échoue quand
on lance ce test SEUL, alors qu'il passe dans la suite complète (un autre test ayant importé
tiers). Importer tout ici rend chaque test robuste à l'isolation.
"""

from app.modules.audit import models as audit_models
from app.modules.comptabilite import models as comptabilite_models
from app.modules.epargne import models as epargne_models
from app.modules.parameters import models as parameters_models
from app.modules.security import models as security_models
from app.modules.tiers import models as tiers_models

# Référencés pour enregistrer les mappers ; le tuple évite un « import inutilisé ».
_MODELES = (
    audit_models,
    comptabilite_models,
    epargne_models,
    parameters_models,
    security_models,
    tiers_models,
)
