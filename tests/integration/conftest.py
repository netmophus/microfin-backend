"""Charge TOUS les modèles ORM avant les tests d'intégration.

Sans ça, un test qui n'importe que les modèles d'un module (ex. épargne) laisse des FK
inter-schémas non résolues (epargne.accounts.tier_id -> tiers.tiers) : le mapping échoue quand
on lance ce test SEUL, alors qu'il passe dans la suite complète (un autre test ayant importé
tiers). Importer tout ici rend chaque test robuste à l'isolation.
"""

import app.modules.audit.models  # noqa: F401
import app.modules.comptabilite.models  # noqa: F401
import app.modules.epargne.models  # noqa: F401
import app.modules.parameters.models  # noqa: F401
import app.modules.security.models  # noqa: F401
import app.modules.tiers.models  # noqa: F401
