"""Pont Caisse -> comptabilité (CA3, migration 0044) : traduire l'écart d'UNE session en pièce
équilibrée, à la VALIDATION (pas à la fermeture — voir `service.py::valider_ecart`).

Résout les rôles du modèle d'écriture depuis `caisse.parametres` :
  - CAISSE -> `compte_caisse_id` DE LA SESSION (ancré à l'ouverture, jamais recalculé) ;
  - ECART  -> `compte_ecart_manquant_id` OU `compte_ecart_excedent_id`, selon le CODE posé —
    DEUX modèles distincts (`caisse.ecart_manquant`/`caisse.ecart_excedent`), jamais un signe
    négatif sur un seul compte (décision actée).
Si le rattachement manque (paramétrage incomplet), on REFUSE proprement — rien n'est écrit,
ni l'écriture ni la trace de validation (transaction unique, voir `valider_ecart`).

Journal OD : une RÉGULARISATION comptable, pas un mouvement de caisse d'un client — même
distinction que `credit.decaissement_epargne` (aucun argent physique ne bouge À CET INSTANT,
l'écriture aligne les livres sur un comptage déjà fait).

Postée SEULEMENT si `ecart != 0` (jamais de ligne à montant nul, même règle que partout) — en
pratique, `valider_ecart()` ne peut être atteint que pour un écart déjà SIGNIFICATIF (au-delà
du seuil de tolérance, donc jamais nul), le cas ecart=0 n'existe pas par construction ici."""

import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.modules.audit.service import CONTEXTE_VIDE, ContexteRequete
from app.modules.caisse.models import CaisseParametres, CaisseSession
from app.modules.comptabilite.models import JournalEntry
from app.modules.comptabilite.schemas_ecriture import ResolveurRole, poser_depuis_schema

CODE_ECART_MANQUANT = "caisse.ecart_manquant"
CODE_ECART_EXCEDENT = "caisse.ecart_excedent"


class RattachementEcartManquantError(Exception):
    """Le compte de l'écart (manquant ou excédent) n'est pas rattaché — paramétrage incomplet,
    refus propre, rien n'est écrit."""


def _resolveur(
    session: CaisseSession, *, compte_ecart_id: uuid.UUID | None, nature: str
) -> ResolveurRole:
    def resoudre(role: str) -> uuid.UUID:
        if role == "CAISSE":
            return session.compte_caisse_id
        if role == "ECART":
            if compte_ecart_id is None:
                raise RattachementEcartManquantError(
                    f"le compte de l'écart ({nature}) n'est pas rattaché (paramétrage) : "
                    "contactez le comptable avant de valider cette session."
                )
            return compte_ecart_id
        raise RattachementEcartManquantError(f"rôle « {role} » inconnu dans le modèle d'écriture")

    return resoudre


def poser_ecriture_ecart(
    db: Session,
    session: CaisseSession,
    config: CaisseParametres,
    *,
    par: uuid.UUID | None,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> JournalEntry | None:
    """Pose la pièce de régularisation de l'écart d'UNE session — `None` si `ecart` est nul ou
    absent (rien à écrire).

    MANQUANT (ecart < 0, le réel compté est INFÉRIEUR au théorique) : D ECART / C CAISSE — le
    théorique baisse jusqu'au réel compté.
    EXCÉDENT (ecart > 0) : D CAISSE / C ECART — le théorique monte jusqu'au réel compté.

    Peut lever `RattachementEcartManquantError` (compte non rattaché) : l'appelant
    (`valider_ecart`) ne doit PAS poser la trace de validation si cet appel lève — transaction
    unique, refus propre, rien n'est écrit."""
    if session.ecart is None or session.ecart == 0:
        return None

    manquant = session.ecart < 0
    code = CODE_ECART_MANQUANT if manquant else CODE_ECART_EXCEDENT
    compte_ecart_id = (
        config.compte_ecart_manquant_id if manquant else config.compte_ecart_excedent_id
    )
    nature = "manquant" if manquant else "excédent"

    jour = db.execute(text("SELECT CURRENT_DATE")).scalar_one()
    return poser_depuis_schema(
        db,
        code=code,
        montant=abs(session.ecart),
        resoudre_role=_resolveur(session, compte_ecart_id=compte_ecart_id, nature=nature),
        entry_date=jour,
        par=par,
        description=f"Régularisation écart de caisse ({nature}) — session {session.id}",
        contexte=contexte,
    )
