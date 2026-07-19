"""Écriture du journal d'audit (C5) — service partagé par TOUS les modules.

Extrait de security/auth.py au bloc 4c, quand les écritures administratives ont montré que
le helper d'auth ne suffisait plus. Tiers, comptabilité, caisse, épargne et crédit
écriront ici : le journal est unique, son format doit l'être aussi.

ACTEUR ET CIBLE SONT DEUX CHOSES DIFFÉRENTES. C'est la correction structurante du 4c.

    user_id     = QUI a agi          (l'administrateur)
    resource_id = SUR QUOI il a agi  (le compte modifié)

Tant qu'on n'auditait que des connexions, les deux se confondaient : celui qui se connecte
est celui dont on parle. Dès la première écriture administrative, les confondre ferait dire
au journal que le nouveau compte s'est créé lui-même. Ce serait un faux — dans une table
immuable, conservée cinq ans, opposable à la BCEAO, et impossible à corriger après coup.
D'où deux paramètres distincts et obligatoires à l'esprit de l'appelant.

AUCUN SECRET, JAMAIS. Ni mot de passe en clair, ni password_hash, ni refresh_token_hash, ni
jeton. Le helper n'écrit que ce qu'on lui passe : la garantie tient aux sites d'appel, et
c'est pourquoi _sans_secret refuse activement les clés interdites plutôt que de faire
confiance. Une fuite dans un journal immuable ne se rattrape pas — on ne peut ni l'effacer
ni la réécrire.

La détection est volontairement GROSSIÈRE (sous-chaînes), donc elle produit des faux
positifs : « must_change_password » contient « password » sans rien révéler. Ils sont levés
par une liste d'exceptions explicites plutôt qu'en affinant la règle — une règle fine
finirait par laisser passer un champ vraiment sensible, alors qu'une exception se relit.

ÉCRIRE TARD. À appeler le plus près possible du commit. Le trigger de chaînage (0003) prend
un verrou consultatif ; écrire l'audit tôt dans une transaction qui verrouille ensuite des
lignes métier inverse l'ordre des verrous et finit en interblocage.
"""

import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

# Clés interdites dans old_values / new_values, quel qu'en soit le module appelant.
# Comparaison sur le nom de clé en minuscules, sous-chaîne comprise : « password_hash »,
# « new_password », « mot_de_passe_genere » tombent tous.
FRAGMENTS_INTERDITS = ("password", "mot_de_passe", "secret", "token", "hash")

# Exceptions EXPLICITES aux fragments ci-dessus : des champs dont le nom contient un
# fragment interdit alors qu'ils ne portent aucun secret. Chaque entrée est une décision
# relue, jamais un contournement de confort — et la liste doit rester courte assez pour se
# lire d'un coup d'œil, comme l'allowlist des routes publiques.
#
#   must_change_password : booléen d'ÉTAT (« un renouvellement est-il dû ? »). Ne dit rien
#       du mot de passe lui-même, et sa trace est nécessaire : elle prouve qu'une
#       réinitialisation a bien imposé le renouvellement.
#   password_changed_at  : horodatage. Utile au contrôle de la politique d'expiration (C9),
#       et ne révèle pas plus qu'une date de modification ordinaire.
CHAMPS_AUTORISES = frozenset({"must_change_password", "password_changed_at"})


class SecretDansAuditError(RuntimeError):
    """Un site d'appel a tenté de journaliser un champ sensible.

    Volontairement FATALE, et non un avertissement filtré en silence : si un développeur
    croit journaliser un mot de passe, il faut qu'il l'apprenne au premier test, pas qu'un
    filtre discret le protège sans qu'il comprenne pourquoi la valeur a disparu.
    """


@dataclass(frozen=True)
class ContexteRequete:
    """Origine de la requête, propagée jusqu'à l'audit.

    Ne porte que des données d'origine (IP, agent, corrélation), jamais de secret. L'IP
    vient de request.client.host, jamais d'un en-tête applicatif falsifiable.
    """

    ip: str | None = None
    user_agent: str | None = None
    request_id: uuid.UUID | None = None


CONTEXTE_VIDE = ContexteRequete()


def _sans_secret(valeurs: dict[str, Any] | None, ou: str) -> dict[str, Any] | None:
    """Refuse tout dictionnaire contenant une clé sensible. Lève plutôt que de filtrer."""
    if valeurs is None:
        return None
    for cle in valeurs:
        minuscule = cle.lower()
        if minuscule in CHAMPS_AUTORISES:
            continue
        if any(fragment in minuscule for fragment in FRAGMENTS_INTERDITS):
            raise SecretDansAuditError(
                f"Champ « {cle} » interdit dans {ou} : le journal d'audit est immuable, "
                "une fuite ne peut ni être effacée ni réécrite."
            )
    return valeurs


def ecrire_audit(
    db: Session,
    *,
    action: str,
    contexte: ContexteRequete,
    acteur_id: uuid.UUID | None,
    resource_type: str | None = None,
    resource_id: uuid.UUID | None = None,
    agency_id: uuid.UUID | None = None,
    old_values: dict[str, Any] | None = None,
    new_values: dict[str, Any] | None = None,
) -> None:
    """Insère une ligne d'audit — SQL paramétré, JAMAIS via l'ORM (le modèle AuditLog lève).

    acteur_id alimente la colonne user_id : c'est QUI a agi. Pour une connexion, l'acteur
    est aussi la cible et resource_id reste vide ; pour une écriture administrative, les
    deux diffèrent et resource_id désigne le compte touché.

    Ne fournit pas chain_hash : le trigger de la 0003 le pose sous verrou consultatif.

    CAST(:x AS type) et non « :x::type » : le « :: » empêche SQLAlchemy de reconnaître le
    paramètre, qui partirait littéralement dans le SQL (piège documenté des tests d'audit).
    """
    old_values = _sans_secret(old_values, "old_values")
    new_values = _sans_secret(new_values, "new_values")

    db.execute(
        text(
            "INSERT INTO audit.audit_logs "
            "(user_id, action, resource_type, resource_id, old_values, new_values, "
            " agency_id, ip_address, user_agent, request_id) "
            "VALUES (CAST(:acteur_id AS uuid), :action, :resource_type, "
            "        CAST(:resource_id AS uuid), CAST(:old_values AS jsonb), "
            "        CAST(:new_values AS jsonb), CAST(:agency_id AS uuid), "
            "        CAST(:ip AS inet), :user_agent, CAST(:request_id AS uuid))"
        ),
        {
            "acteur_id": acteur_id,
            "action": action,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "old_values": json.dumps(old_values) if old_values is not None else None,
            "new_values": json.dumps(new_values) if new_values is not None else None,
            "agency_id": agency_id,
            "ip": contexte.ip,
            "user_agent": contexte.user_agent,
            "request_id": contexte.request_id,
        },
    )
