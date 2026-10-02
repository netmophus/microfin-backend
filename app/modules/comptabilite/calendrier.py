"""Calendrier des jours ouvrés — chantier P1bis, lot 4a.

FONDATION ADDITIVE SEULE : « savoir si un jour est ouvré » et le paramétrage des fériés. Le
REPORT EFFECTIF d'une échéance sur un jour férié est le lot 4b, séparé — ce fichier ne touche
NI `_ajouter_periode` (échéancier), NI le calcul d'intérêts, NI la souffrance.

NON OUVRÉ = samedi + dimanche (automatiques) OU une date présente dans `jours_feries`. Les
fériés sont saisis PAR DATE précise (jamais une règle récurrente) : les fêtes musulmanes
suivent le calendrier lunaire et n'ont pas de date fixe d'une année à l'autre.

Consommé par `comptabilite.journee.prochaine_date_ouvree` (seule modification d'un
comportement existant permise dans ce lot — voir son docstring) : le calcul de la date
proposée à l'ouverture de la journée tient désormais compte des fériés, pas seulement du
week-end.
"""

import uuid
from datetime import date, timedelta

from sqlalchemy import extract, select
from sqlalchemy.orm import Session

from app.modules.audit.service import CONTEXTE_VIDE, ContexteRequete, ecrire_audit
from app.modules.comptabilite.models import JourFerie

SAMEDI = 5
DIMANCHE = 6


class ActionsAudit:
    """Actions d'audit propres au calendrier des jours fériés (format module.action)."""

    AJOUTE = "compta.calendrier.ajoute"
    SUPPRIME = "compta.calendrier.supprime"


class JourFerieExistantError(Exception):
    """Un jour férié existe déjà pour cette date : pas de doublon (UNIQUE en base)."""


class JourFerieIntrouvableError(Exception):
    """Ce jour férié n'existe pas (hors périmètre ou déjà supprimé)."""


def est_jour_ouvre(db: Session, jour: date) -> bool:
    """Faux si `jour` est un samedi, un dimanche, ou une date présente dans `jours_feries` —
    vrai sinon. Aucune notion de jour ouvré « à moitié » : un jour l'est ou ne l'est pas."""
    if jour.weekday() in (SAMEDI, DIMANCHE):
        return False
    return (
        db.execute(select(JourFerie.id).where(JourFerie.date_feriee == jour)).first() is None
    )


def prochain_jour_ouvre(db: Session, jour: date, *, strict: bool = False) -> date:
    """Le premier jour ouvré À PARTIR DE `jour` — `jour` lui-même INCLUS si `strict=False`
    (défaut : cohérent avec `journee.prochaine_date_ouvree`, qui propose AUJOURD'HUI s'il est
    déjà ouvré, pas le jour suivant). `strict=True` cherche STRICTEMENT après `jour` (utile au
    lot 4b : le jour SUIVANT une échéance tombée un férié, jamais l'échéance elle-même)."""
    candidat = jour + timedelta(days=1) if strict else jour
    while not est_jour_ouvre(db, candidat):
        candidat += timedelta(days=1)
    return candidat


def lister_jours_feries(db: Session, annee: int) -> list[JourFerie]:
    """Les fériés d'UNE année, du plus ancien au plus récent — le paramétrage se fait année
    par année (fêtes lunaires jamais à la même date d'une année sur l'autre)."""
    return list(
        db.execute(
            select(JourFerie)
            .where(extract("year", JourFerie.date_feriee) == annee)
            .order_by(JourFerie.date_feriee)
        ).scalars()
    )


def ajouter_jour_ferie(
    db: Session,
    date_feriee: date,
    libelle: str,
    par: uuid.UUID | None,
    *,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> JourFerie:
    """Ajoute un jour férié — refuse un doublon de date (`JourFerieExistantError`, le dernier
    rempart étant l'UNIQUE en base, migration 0056)."""
    deja = db.execute(select(JourFerie.id).where(JourFerie.date_feriee == date_feriee)).first()
    if deja is not None:
        raise JourFerieExistantError(
            f"Un jour férié existe déjà pour le {date_feriee} : pas de doublon."
        )

    jour_ferie = JourFerie(date_feriee=date_feriee, libelle=libelle, created_by=par)
    db.add(jour_ferie)
    db.flush()

    ecrire_audit(
        db,
        action=ActionsAudit.AJOUTE,
        contexte=contexte,
        acteur_id=par,
        resource_type="compta.jour_ferie",
        resource_id=jour_ferie.id,
        new_values={"date_feriee": date_feriee.isoformat(), "libelle": libelle},
    )
    return jour_ferie


def supprimer_jour_ferie(
    db: Session,
    jour_ferie_id: uuid.UUID,
    par: uuid.UUID | None,
    *,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> None:
    """Supprime un jour férié — suppression PHYSIQUE délibérée (pure donnée de paramétrage,
    pas une écriture métier : aucune règle d'immuabilité ne s'applique ici), tracée à
    l'audit."""
    jour_ferie = db.get(JourFerie, jour_ferie_id)
    if jour_ferie is None:
        raise JourFerieIntrouvableError("Ce jour férié n'existe pas.")

    ecrire_audit(
        db,
        action=ActionsAudit.SUPPRIME,
        contexte=contexte,
        acteur_id=par,
        resource_type="compta.jour_ferie",
        resource_id=jour_ferie.id,
        old_values={
            "date_feriee": jour_ferie.date_feriee.isoformat(),
            "libelle": jour_ferie.libelle,
        },
    )
    db.delete(jour_ferie)
    db.flush()
