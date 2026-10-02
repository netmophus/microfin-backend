"""Journée comptable — chantier P1bis, lots 1, 2 et 3.

COUCHE MINCE : modèle, ouverture/fermeture, date comptable courante, branchement caisse,
datation des opérations. `date_comptable_obligatoire` (lot 3) est LE point d'entrée que tous
les modules datant une opération consomment désormais — voir son docstring pour la liste.

CENTRALISÉE (globale, pas par agence), au plus une 'ouverte' à la fois — le garde-fou
définitif est l'index unique partiel posé par la migration 0055
(`uq_journees_comptables_ouverte`) ; les contrôles ci-dessous (`deja is not None`) sont une
commodité de message, pas le dernier rempart, même philosophie que
`caisse.service.ouvrir_session`.

DÉFINITIVE (même décision que l'exercice comptable, `cloture_exercice.py`) : aucune
réouverture, aucune colonne ni fonction pour ça.

BRANCHEMENT CAISSE (chantier P1bis, lot 2) : `cloturer_journee` refuse si une session de
caisse reste ouverte quelque part sur le réseau (`_nombre_de_caisses_ouvertes`, requête
DIRECTE sur `caisse.sessions`, aucun import du module caisse — voir la fonction). Côté
ouverture, c'est `caisse.service.ouvrir_session` qui refuse si aucune journée n'est ouverte
(`caisse.service.JourneeFermeeError`) : ce fichier ne contient pas cette moitié de la
précondition, par construction (caisse dépend de comptabilite, jamais l'inverse).
"""

import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from typing import cast

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.modules.audit.service import CONTEXTE_VIDE, ContexteRequete, ecrire_audit
from app.modules.comptabilite.models import JourneeComptable

SAMEDI = 5
DIMANCHE = 6


class ActionsAudit:
    """Actions d'audit propres à la journée comptable (format module.action)."""

    OUVERTE = "compta.journee.ouverte"
    CLOTUREE = "compta.journee.cloturee"


class UneSeuleJourneeError(Exception):
    """Une journée est déjà ouverte : fermez-la avant d'en ouvrir une nouvelle."""


class JourneeExistanteError(Exception):
    """Une journée existe déjà pour cette date (ouverte ou clôturée) : pas de doublon."""


class AucuneJourneeOuverteError(Exception):
    """Aucune journée ouverte à clôturer."""


class CaissesOuvertesError(Exception):
    """Au moins une session de caisse reste ouverte quelque part sur le réseau (chantier
    P1bis, lot 2) : la journée ne peut pas être clôturée tant qu'un caissier n'a pas fermé
    son tiroir."""


@dataclass(frozen=True)
class JourneeCourante:
    """Ce que l'écran d'ouverture a besoin de savoir en un seul appel : la journée ouverte
    (ou son absence) ET la date proposée par défaut pour en ouvrir une nouvelle."""

    journee: JourneeComptable | None
    prochaine_date_proposee: date


def journee_ouverte(db: Session) -> JourneeComptable | None:
    """La journée actuellement ouverte, ou `None` — au plus une, garanti par l'index unique
    partiel (migration 0055)."""
    return db.execute(
        select(JourneeComptable).where(JourneeComptable.status == "ouverte")
    ).scalar_one_or_none()


def date_comptable_courante(db: Session) -> date | None:
    """La date de la journée ouverte, ou `None` si aucune journée n'est ouverte. Usage interne
    (`date_comptable_obligatoire`) ou pour un écran qui affiche l'état sans rien dater — tout
    module qui DATE une opération doit passer par `date_comptable_obligatoire`, jamais par
    celle-ci directement (un `None` ne doit jamais se propager jusqu'à une écriture)."""
    journee = journee_ouverte(db)
    return journee.date_comptable if journee is not None else None


def date_comptable_obligatoire(db: Session) -> date:
    """LE point d'entrée unique pour dater une opération métier (chantier P1bis, lot 3) :
    decaissement, remboursement, épargne, OD/contre-passation, affectation du résultat, parts,
    transferts, régularisation d'écart de caisse, retard de recouvrement. Lève
    `AucuneJourneeOuverteError` si aucune journée n'est ouverte — JAMAIS un retour silencieux
    à la date système, JAMAIS un `None` qui se propagerait jusqu'à une écriture."""
    jour = date_comptable_courante(db)
    if jour is None:
        raise AucuneJourneeOuverteError("Aucune journée comptable n'est ouverte.")
    return jour


def prochaine_date_ouvree(db: Session) -> date:
    """Date proposée par défaut à l'ouverture : aujourd'hui si c'est un jour ouvré (lundi à
    vendredi), sinon le prochain lundi. Pas de calendrier de jours fériés dans ce lot
    (lot 4, séparable) — « ouvré » se limite ici à « ni samedi ni dimanche ». La date du jour
    est lue côté base (`CURRENT_DATE`), même discipline que partout ailleurs dans ce
    module."""
    jour = cast(date, db.execute(text("SELECT CURRENT_DATE")).scalar_one())
    while jour.weekday() in (SAMEDI, DIMANCHE):
        jour += timedelta(days=1)
    return jour


def journee_courante(db: Session) -> JourneeCourante:
    """Agrège la journée ouverte (ou son absence) et la prochaine date ouvrée proposée —
    tout ce dont le formulaire d'ouverture a besoin, en un seul appel."""
    return JourneeCourante(
        journee=journee_ouverte(db), prochaine_date_proposee=prochaine_date_ouvree(db)
    )


def _nombre_de_caisses_ouvertes(db: Session) -> int:
    """Requête DIRECTE sur caisse.sessions, PAS d'import du module caisse (chantier P1bis, lot
    2) — même discipline que `caisse.service.calculer_solde_theorique`, qui interroge
    comptabilite en sens inverse par SQL direct plutôt que par import croisé. Lit le même
    index unique partiel que caisse.sessions utilise pour garantir « une session ouverte par
    caissier » ; aucune nouvelle migration."""
    resultat = db.execute(
        text("SELECT count(*) FROM caisse.sessions WHERE status = 'ouverte'")
    ).scalar_one()
    return int(resultat)


def lister_journees(db: Session) -> list[JourneeComptable]:
    """Tout l'historique, la plus récente d'abord — jamais supprimée (pas de fonction de
    suppression sur ce modèle)."""
    return list(
        db.execute(
            select(JourneeComptable).order_by(JourneeComptable.date_comptable.desc())
        ).scalars()
    )


def ouvrir_journee(
    db: Session,
    date_comptable: date,
    par: uuid.UUID | None,
    *,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> JourneeComptable:
    """Ouvre la journée comptable pour `date_comptable` — refuse si une journée est déjà
    ouverte (`UneSeuleJourneeError`) ou si une journée existe déjà pour cette date, ouverte
    ou clôturée (`JourneeExistanteError`)."""
    if journee_ouverte(db) is not None:
        raise UneSeuleJourneeError(
            "Une journée comptable est déjà ouverte : clôturez-la avant d'en ouvrir une "
            "nouvelle."
        )

    deja = db.execute(
        select(JourneeComptable.id).where(JourneeComptable.date_comptable == date_comptable)
    ).first()
    if deja is not None:
        raise JourneeExistanteError(
            f"Une journée comptable existe déjà pour le {date_comptable} : impossible d'en "
            "ouvrir une seconde à cette date."
        )

    journee = JourneeComptable(date_comptable=date_comptable, opened_by=par)
    db.add(journee)
    db.flush()

    ecrire_audit(
        db,
        action=ActionsAudit.OUVERTE,
        contexte=contexte,
        acteur_id=par,
        resource_type="compta.journee",
        resource_id=journee.id,
        new_values={"date_comptable": date_comptable.isoformat(), "status": "ouverte"},
    )
    return journee


def cloturer_journee(
    db: Session, par: uuid.UUID | None, *, contexte: ContexteRequete = CONTEXTE_VIDE
) -> JourneeComptable:
    """Clôture DÉFINITIVEMENT la journée ouverte — refuse s'il n'y en a aucune
    (`AucuneJourneeOuverteError`), ou si une session de caisse reste ouverte quelque part sur
    le réseau (`CaissesOuvertesError`, chantier P1bis lot 2 — tout le réseau, pas seulement
    l'agence de l'acteur : la journée est centralisée). FOR UPDATE anti double-clic : deux
    clics simultanés sur « Clôturer » ne doivent jamais produire deux clôtures."""
    journee = db.execute(
        select(JourneeComptable).where(JourneeComptable.status == "ouverte").with_for_update()
    ).scalar_one_or_none()
    if journee is None:
        raise AucuneJourneeOuverteError("Aucune journée comptable ouverte à clôturer.")

    caisses_ouvertes = _nombre_de_caisses_ouvertes(db)
    if caisses_ouvertes > 0:
        raise CaissesOuvertesError(
            f"{caisses_ouvertes} caisse(s) encore ouverte(s) : fermez-les avant de clôturer "
            "la journée comptable."
        )

    maintenant = db.execute(text("SELECT NOW()")).scalar_one()
    journee.status = "cloturee"
    journee.closed_at = maintenant
    journee.closed_by = par
    db.flush()

    ecrire_audit(
        db,
        action=ActionsAudit.CLOTUREE,
        contexte=contexte,
        acteur_id=par,
        resource_type="compta.journee",
        resource_id=journee.id,
        new_values={"status": "cloturee"},
    )
    return journee
