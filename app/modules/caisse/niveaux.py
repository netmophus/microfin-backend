"""Paramétrage des niveaux caisse par agence (chantier coffre-fort/caisses, sous-chantier 1,
Bloc 1) — lire/écrire `caisse.niveaux_caisse` : pour une agence, quel compte de saisie joue le
rôle « coffre » ou « principale ».

PAS LE NIVEAU SECONDAIRE : celui-ci reste sur `caisse.postes.compte_caisse_id`, déjà nullable et
déjà indépendant par poste (Bloc A, migration 0041) — voir docstring du modèle.

GARDE-FOU : le compte soumis passe par `comptabilite.comptes.compte_caisse_valide` (PAS
`compte_saisie_actif` seul) — exige EN PLUS qu'il descende de la rubrique 1011 (Billets et
monnaies émis par la BCEAO), contrainte propre à la caisse, jamais imposée aux autres
rattachements (épargne, parts, écart).

VIDE PAR DÉFAUT : une agence sans ligne pour un niveau n'est PAS une erreur — `lire_niveaux`
synthétise les deux niveaux (`coffre`, `principale`) pour toute agence, `None` là où rien n'a
encore été rattaché. Aucune ligne n'est créée tant que le comptable n'a pas choisi un compte.

MOTIF OBLIGATOIRE à chaque rattachement (même discipline que `parameters.rattachements` et
`epargne.rattachements`) : tracé avant/après dans l'audit. Vider un rattachement (compte=None)
reste une action légitime, pas une erreur.

AUCUN IMPACT SUR LE PASSÉ : les écritures déjà posées référencent directement un compte concret
(`journal_lines.account_id`, figé à la pose), jamais ce paramètre — changer un rattachement ne
peut affecter que les PROCHAINES opérations."""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.audit.service import CONTEXTE_VIDE, ContexteRequete, ecrire_audit
from app.modules.caisse.models import NiveauCaisse
from app.modules.comptabilite.comptes import compte_caisse_valide
from app.modules.comptabilite.models import Account

RESSOURCE = "caisse.niveau"

NIVEAUX_VALIDES = ("coffre", "principale")


class NiveauCaisseError(Exception):
    """Base des erreurs métier de ce module."""


class NiveauInvalideError(NiveauCaisseError):
    """Le niveau demandé n'est ni « coffre » ni « principale »."""

    def __init__(self, niveau: str) -> None:
        super().__init__(
            f"Niveau « {niveau} » inconnu : seuls « coffre » et « principale » sont "
            "paramétrables ici (le niveau secondaire se rattache par poste)."
        )
        self.niveau = niveau


def lire_niveaux(db: Session, agency_id: uuid.UUID) -> dict[str, NiveauCaisse | None]:
    """Les deux niveaux de CETTE agence, synthétisés — `None` pour un niveau jamais rattaché
    (aucune ligne en base), pas une erreur : c'est l'état de départ normal."""
    lignes = db.execute(
        select(NiveauCaisse).where(NiveauCaisse.agency_id == agency_id)
    ).scalars()
    par_niveau = {ligne.niveau: ligne for ligne in lignes}
    return {niveau: par_niveau.get(niveau) for niveau in NIVEAUX_VALIDES}


def rattacher_niveau(
    db: Session,
    agency_id: uuid.UUID,
    niveau: str,
    *,
    compte_caisse_number: str | None,
    motif: str,
    par: uuid.UUID | None,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> NiveauCaisse:
    """Rattache (ou vide) le compte d'un niveau pour une agence. Crée la ligne si elle n'existe
    pas encore (premier rattachement de ce niveau pour cette agence), la met à jour sinon.

    Peut lever (propagées telles quelles, jamais réimplémentées) :
    `NiveauInvalideError`, `comptes.CompteInvalideRattachementError` (compte inexistant/de
    regroupement/désactivé), `comptes.CompteHorsCaisseError` (compte hors rubrique 1011)."""
    if niveau not in NIVEAUX_VALIDES:
        raise NiveauInvalideError(niveau)

    nouveau = compte_caisse_valide(db, compte_caisse_number) if compte_caisse_number else None

    ligne = db.execute(
        select(NiveauCaisse).where(
            NiveauCaisse.agency_id == agency_id, NiveauCaisse.niveau == niveau
        )
    ).scalar_one_or_none()

    avant_numero = None
    if ligne is not None and ligne.compte_caisse_id is not None:
        avant = db.get(Account, ligne.compte_caisse_id)
        avant_numero = avant.account_number if avant else None

    if ligne is None:
        ligne = NiveauCaisse(
            agency_id=agency_id,
            niveau=niveau,
            compte_caisse_id=nouveau.id if nouveau else None,
            created_by=par,
            updated_by=par,
        )
        db.add(ligne)
    else:
        ligne.compte_caisse_id = nouveau.id if nouveau else None
        ligne.updated_by = par
    db.flush()

    ecrire_audit(
        db,
        action="caisse.niveau.rattache",
        contexte=contexte,
        acteur_id=par,
        resource_type=RESSOURCE,
        resource_id=ligne.id,
        agency_id=agency_id,
        old_values={"niveau": niveau, "compte_caisse": avant_numero},
        new_values={"niveau": niveau, "compte_caisse": compte_caisse_number, "motif": motif},
    )
    return ligne
