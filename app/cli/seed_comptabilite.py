"""Seed du cadre comptable : les journaux standard (données PROVISOIRES) et l'ouverture
d'un exercice.

Les journaux sont des DONNÉES (comme le plan de comptes) : livrés provisoires, à valider et
compléter par le comptable, sans redéploiement. Idempotent (upsert par code).

L'ouverture d'exercice est une OPÉRATION (dates propres à l'IMF), séparée du seed des journaux :
`ouvrir_exercice` crée un exercice « ouvert ». La contrainte d'exclusion (0016) empêche tout
chevauchement — donc deux exercices ouverts ne peuvent pas se recouvrir.
"""

from dataclasses import dataclass
from datetime import date

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.modules.comptabilite.models import Exercice


@dataclass(frozen=True)
class JournalStandard:
    code: str
    name: str
    type: str


# Journaux minimaux d'une IMF. PROVISOIRES : le comptable les valide/complète.
JOURNAUX: tuple[JournalStandard, ...] = (
    JournalStandard("CA", "Journal de caisse", "tresorerie"),
    JournalStandard("BQ", "Journal de banque", "tresorerie"),
    JournalStandard("OD", "Opérations diverses", "operations_diverses"),
    JournalStandard("AN", "À-nouveaux", "a_nouveaux"),
)


_UPSERT_JOURNAL = text(
    """
    INSERT INTO comptabilite.journals (code, name, type, is_provisional, is_system)
    VALUES (:code, :name, :type, TRUE, FALSE)
    ON CONFLICT (code) DO UPDATE SET
        name       = EXCLUDED.name,
        type       = EXCLUDED.type,
        updated_at = NOW()
    """
)


def executer_seed_journaux(db: Session) -> int:
    """Installe/actualise les journaux standard (provisoires). Ne committe pas."""
    for journal in JOURNAUX:
        db.execute(
            _UPSERT_JOURNAL,
            {"code": journal.code, "name": journal.name, "type": journal.type},
        )
    return len(JOURNAUX)


# --- Modèles d'écriture (pont comptable E1), PROVISOIRES ---------------------------------------
@dataclass(frozen=True)
class ModeleEcriture:
    code: str
    label: str
    journal: str  # code du journal où poser la pièce
    lignes: tuple[tuple[str, str], ...]  # (rôle, sens D/C), ordonnées


# Dépôt : D CAISSE / C EPARGNE (la caisse entre, la dette envers le membre monte).
# Retrait : l'inverse. Comptes exacts résolus au moment de l'opération (rôle -> compte).
MODELES: tuple[ModeleEcriture, ...] = (
    ModeleEcriture("epargne.depot", "Dépôt d'épargne", "CA", (("CAISSE", "D"), ("EPARGNE", "C"))),
    ModeleEcriture(
        "epargne.retrait", "Retrait d'épargne", "CA", (("EPARGNE", "D"), ("CAISSE", "C"))
    ),
    # Clôture : mêmes D/C qu'un retrait, mais ÉTIQUETÉE clôture (restitution de fin de vie ≠
    # retrait courant) — traçabilité fine en audit.
    ModeleEcriture(
        "epargne.cloture", "Clôture d'épargne (restitution)", "CA",
        (("EPARGNE", "D"), ("CAISSE", "C")),
    ),
    # Intérêts : la charge de l'IMF monte (D 603), la dette envers le membre monte (C 3111).
    # Journal des opérations diverses (pas de mouvement de caisse).
    ModeleEcriture(
        "epargne.interet", "Intérêts d'épargne (versement)", "OD",
        (("INTERETS", "D"), ("EPARGNE", "C")),
    ),
    # --- Parts sociales (PS1), PROVISOIRES. 1022 sens D (souscrites non libérées, créance),
    # 1021 sens C (libérées, capital). Souscription-engagement SANS caisse (journal OD) ;
    # libération et comptant AVEC caisse (journal CA — argent qui entre, comme un dépôt).
    ModeleEcriture(
        "parts.souscription", "Souscription de parts (engagement)", "OD",
        (("PARTS_NON_LIBEREES", "D"), ("PARTS_LIBEREES", "C")),
    ),
    ModeleEcriture(
        "parts.liberation", "Libération de parts (paiement)", "CA",
        (("CAISSE", "D"), ("PARTS_NON_LIBEREES", "C")),
    ),
    ModeleEcriture(
        "parts.souscription_comptant", "Souscription de parts au comptant", "CA",
        (("CAISSE", "D"), ("PARTS_LIBEREES", "C")),
    ),
    # Remboursement (départ du sociétaire) : rendre le capital libéré. D 1021 / C caisse.
    ModeleEcriture(
        "parts.remboursement", "Remboursement de parts", "CA",
        (("PARTS_LIBEREES", "D"), ("CAISSE", "C")),
    ),
    # Annulation d'une souscription NON libérée (promesse jamais payée) : on solde l'engagement,
    # sans caisse (rien n'a été versé). D 1021 / C 1022 — l'inverse de la souscription-engagement.
    ModeleEcriture(
        "parts.annulation", "Annulation de souscription (non libérée)", "OD",
        (("PARTS_LIBEREES", "D"), ("PARTS_NON_LIBEREES", "C")),
    ),
    # Décaissement de crédit (CR3) : la caisse sort, la créance sur le tiers monte.
    # D CREDIT (202211/202221 ou 203111/203121, selon produit + membre/client) / C CAISSE.
    ModeleEcriture(
        "credit.decaissement", "Décaissement de crédit", "CA",
        (("CREDIT", "D"), ("CAISSE", "C")),
    ),
    # Décaissement de crédit DIRECT sur un compte du tiers (mode 'epargne') : aucun argent
    # physique ne bouge, virement comptable interne — journal OD, même distinction que la
    # souscription-engagement des parts sociales (pas de caisse tant que rien n'est versé/reçu
    # physiquement). D CREDIT / C EPARGNE (le compte epargne.accounts choisi, n'importe quel
    # produit — résolu par credit/decaissement.py, pas par le résolveur d'Épargne).
    ModeleEcriture(
        "credit.decaissement_epargne", "Décaissement de crédit sur compte du tiers", "OD",
        (("CREDIT", "D"), ("EPARGNE", "C")),
    ),
)

_UPSERT_SCHEMA = text(
    """
    INSERT INTO comptabilite.entry_schemas (code, label, journal_id, is_provisional)
    VALUES (:code, :label, (SELECT id FROM comptabilite.journals WHERE code = :journal), TRUE)
    ON CONFLICT (code) DO UPDATE SET
        label      = EXCLUDED.label,
        journal_id = EXCLUDED.journal_id,
        updated_at = NOW()
    RETURNING id
    """
)


def executer_seed_schemas(db: Session) -> int:
    """Installe/actualise les modèles d'écriture provisoires (dépôt, retrait). Ne committe pas."""
    for modele in MODELES:
        schema_id = db.execute(
            _UPSERT_SCHEMA,
            {"code": modele.code, "label": modele.label, "journal": modele.journal},
        ).scalar_one()
        # Réécrit les lignes du modèle (idempotent) : on repart d'un jeu propre.
        db.execute(
            text("DELETE FROM comptabilite.entry_schema_lines WHERE schema_id = :s"),
            {"s": schema_id},
        )
        for ordre, (role, side) in enumerate(modele.lignes, start=1):
            db.execute(
                text(
                    "INSERT INTO comptabilite.entry_schema_lines "
                    "(schema_id, line_order, role, side) VALUES (:s, :o, :r, :side)"
                ),
                {"s": schema_id, "o": ordre, "r": role, "side": side},
            )
    return len(MODELES)


def rattacher_caisse_agences(db: Session, numero: str = "101111") -> int:
    """Rattache PROVISOIREMENT le compte de caisse `numero` aux agences qui n'en ont pas.

    Ne remplace jamais un rattachement déjà posé (la vraie config caisse sera propre à l'IMF).
    Renvoie le nombre d'agences nouvellement rattachées (0 si le compte n'existe pas).
    """
    return len(
        db.execute(
            text(
                "UPDATE parameters.agencies SET compte_caisse_id = "
                "(SELECT id FROM comptabilite.accounts WHERE account_number = :n), "
                "updated_at = NOW() "
                "WHERE compte_caisse_id IS NULL "
                "AND EXISTS (SELECT 1 FROM comptabilite.accounts WHERE account_number = :n) "
                "RETURNING id"
            ),
            {"n": numero},
        ).fetchall()
    )


def seed_parametres_parts(db: Session) -> int:
    """Installe la config PROVISOIRE des parts sociales (une ligne) + rattache les comptes
    d'extension à 6 chiffres (571111/571121 — jamais les officiels 57111/57112 directement,
    voir docs/conformite-comptable.md).

    Idempotent et non destructif : crée la ligne si aucune n'existe (valeurs neutres — l'IMF fixe
    la valeur d'une part et le minimum) ; sinon complète seulement les rattachements comptables
    laissés à NULL. Renvoie 1 si la ligne a été créée, 0 sinon.
    """
    # Historique des rôles (tiers.share_account_roles) : peuplé ici aussi (pas seulement par
    # parts_parametres.modifier()), pour qu'une installation FRAÎCHE ait, dès le seed, la mémoire
    # nécessaire au rapprochement du capital — jamais dépendant d'un premier changement manuel.
    db.execute(
        text(
            "INSERT INTO tiers.share_account_roles (role, account_id) "
            "SELECT 'liberees', id FROM comptabilite.accounts WHERE account_number = '571111' "
            "UNION ALL "
            "SELECT 'non_liberees', id FROM comptabilite.accounts WHERE account_number = '571121' "
            "ON CONFLICT (role, account_id) DO NOTHING"
        )
    )

    existe = db.execute(text("SELECT count(*) FROM tiers.share_parameters")).scalar_one()
    if existe:
        # Complète les rattachements manquants sans écraser la config d'une IMF.
        db.execute(
            text(
                "UPDATE tiers.share_parameters SET "
                "compte_parts_liberees_id = COALESCE(compte_parts_liberees_id, "
                "  (SELECT id FROM comptabilite.accounts WHERE account_number = '571111')), "
                "compte_parts_non_liberees_id = COALESCE(compte_parts_non_liberees_id, "
                "  (SELECT id FROM comptabilite.accounts WHERE account_number = '571121')), "
                "updated_at = NOW()"
            )
        )
        return 0
    db.execute(
        text(
            "INSERT INTO tiers.share_parameters "
            "(unit_value, minimum_shares, is_refundable, membership_on, "
            " compte_parts_liberees_id, compte_parts_non_liberees_id, is_provisional) "
            "VALUES (0, 1, TRUE, 'liberation', "
            " (SELECT id FROM comptabilite.accounts WHERE account_number = '571111'), "
            " (SELECT id FROM comptabilite.accounts WHERE account_number = '571121'), TRUE)"
        )
    )
    return 1


class ExerciceChevauchantError(Exception):
    """Un exercice existant recouvre déjà la période demandée."""


def ouvrir_exercice(
    db: Session, *, code: str, label: str, date_debut: date, date_fin: date
) -> Exercice:
    """Crée un exercice OUVERT. Ne committe pas. La non-superposition est garantie par la base."""
    exercice = Exercice(
        code=code,
        label=label,
        date_debut=date_debut,
        date_fin=date_fin,
        status="ouvert",
    )
    db.add(exercice)
    db.flush()
    return exercice
