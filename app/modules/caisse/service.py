"""Caisse CA1 — ouverture, fermeture, calcul de l'écart. AUCUNE écriture comptable posée ici
(CA3, une fois le compte d'écart validé par l'expert), AUCUN blocage sur écart (CA2), AUCUNE
tolérance/seuil (CA2, `caisse.parametres` pas encore créée).

LE CALCUL CENTRAL (`calculer_solde_theorique`) est un calcul DÉRIVÉ, jamais un solde stocké
qu'on relit — même discipline que `epargne.rapprochement.rapprocher()` : on interroge les
écritures VALIDÉES du compte de caisse (`comptabilite.journal_lines`/`journal_entries`),
fenêtrées entre l'ouverture de la session et l'instant demandé, filtrées sur LE CAISSIER de
cette session (`journal_entries.created_by`) — pas sur tout le compte, qui est partagé par
l'agence entière si plusieurs caissiers travaillent en parallèle (décision : une session par
caissier, pas par agence).

UNE SEULE SESSION OUVERTE PAR CAISSIER : vérifié ici (message clair) ET en base (index unique
partiel `uq_caisse_sessions_caissier_ouverte`, dernier rempart si ce contrôle était contourné).
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.modules.audit.service import CONTEXTE_VIDE, ContexteRequete, ecrire_audit
from app.modules.caisse.models import CaisseSession
from app.modules.parameters.models import Agency
from app.modules.security.autorisation import UtilisateurCourant

RESSOURCE = "caisse.session"


class SessionDejaOuverteError(Exception):
    """Ce caissier a déjà une session ouverte — une seule à la fois."""


class SessionIntrouvableError(Exception):
    """Session inexistante, ou n'appartenant pas à l'acteur. -> 404 (jamais 403 : IDOR)."""


class SessionDejaFermeeError(Exception):
    """Cette session est déjà fermée — on ne referme pas ce qui l'est déjà."""


class RattachementManquantError(Exception):
    """L'agence de l'acteur n'a pas de compte de caisse rattaché (paramétrage)."""


def ouvrir_session(
    db: Session,
    courant: UtilisateurCourant,
    *,
    fonds_initial: int,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> CaisseSession:
    """Ouvre une session POUR L'ACTEUR LUI-MÊME (`caissier_id = courant.user_id`) — jamais pour
    un autre, jamais un champ soumis par le client. Refuse si une session est déjà ouverte pour
    lui (message clair ; l'index unique partiel est le dernier rempart en base si ce contrôle
    était contourné par un appel concurrent).

    `compte_caisse_id` est ANCRÉ ici, copié depuis l'agence COURANTE de l'acteur à cet instant —
    jamais recalculé ensuite, même si le rattachement de l'agence change après coup."""
    deja = db.execute(
        select(CaisseSession.id).where(
            CaisseSession.caissier_id == courant.user_id, CaisseSession.status == "ouverte"
        )
    ).first()
    if deja is not None:
        raise SessionDejaOuverteError(
            "Une session de caisse est déjà ouverte pour vous : fermez-la avant d'en ouvrir "
            "une nouvelle."
        )

    compte_caisse_id = db.execute(
        select(Agency.compte_caisse_id).where(Agency.id == courant.agency_id)
    ).scalar_one_or_none()
    if compte_caisse_id is None:
        raise RattachementManquantError(
            "votre agence n'a pas de compte de caisse rattaché (paramétrage)"
        )

    session = CaisseSession(
        agency_id=courant.agency_id,
        caissier_id=courant.user_id,
        compte_caisse_id=compte_caisse_id,
        fonds_initial=fonds_initial,
        created_by=courant.user_id,
        updated_by=courant.user_id,
    )
    db.add(session)
    db.flush()

    ecrire_audit(
        db,
        action="caisse.session.ouverte",
        contexte=contexte,
        acteur_id=courant.user_id,
        resource_type=RESSOURCE,
        resource_id=session.id,
        agency_id=courant.agency_id,
        new_values={"fonds_initial": fonds_initial},
    )
    return session


def _charger_session_de_lacteur(
    db: Session, courant: UtilisateurCourant, session_id: uuid.UUID, *, verrou: bool = False
) -> CaisseSession:
    """Charge une session qui appartient à L'ACTEUR — hors périmètre ou inexistante ->
    SessionIntrouvableError (404), jamais 403 : on ne révèle pas qu'une session d'un autre
    caissier existe. Contrôle au niveau de l'OBJET, pas seulement de la route (IDOR).

    `verrou=True` (fermeture, une mutation) sérialise une double fermeture concurrente ; une
    simple lecture ne verrouille jamais (coûterait la ligne pour toute la durée de la requête)."""
    stmt = select(CaisseSession).where(
        CaisseSession.id == session_id, CaisseSession.caissier_id == courant.user_id
    )
    if verrou:
        stmt = stmt.with_for_update()
    session = db.execute(stmt).scalar_one_or_none()
    if session is None:
        raise SessionIntrouvableError()
    return session


def calculer_solde_theorique(
    db: Session, session: CaisseSession, *, jusqu_a: datetime | None = None
) -> int:
    """fonds_initial + Σ mouvements de caisse VALIDÉS du CAISSIER de cette session, sur son
    compte de caisse ancré, entre l'ouverture et `jusqu_a` (par défaut : maintenant — utile pour
    un aperçu en cours de session, avant toute fermeture).

    Calcul PUR sur les écritures déjà postées (`journal_entries.status = 'validee'`) — même
    discipline que `epargne.rapprochement.rapprocher()` : jamais un solde stocké qu'on relirait,
    toujours recalculable à l'identique tant que les écritures (immuables) n'ont pas changé.

    101111 (Caisse) est de sens NORMAL DÉBITEUR : un dépôt (D CAISSE) l'augmente, un retrait
    (C CAISSE) le diminue — solde = Σ débits - Σ crédits, PAS l'inverse de `rapprocher()` (qui
    rapproche un compte de sens créditeur, une dette envers les membres)."""
    borne = jusqu_a
    if borne is None:
        borne = db.execute(text("SELECT NOW()")).scalar_one()

    mouvements = db.execute(
        text(
            "SELECT COALESCE(SUM(CASE WHEN jl.side = 'D' THEN jl.amount ELSE -jl.amount END), 0) "
            "FROM comptabilite.journal_lines jl "
            "JOIN comptabilite.journal_entries je ON je.id = jl.entry_id "
            "WHERE jl.account_id = :compte "
            "AND je.status = 'validee' "
            "AND je.created_by = :caissier "
            "AND je.posted_at >= :ouverture "
            "AND je.posted_at <= :borne"
        ),
        {
            "compte": session.compte_caisse_id,
            "caissier": session.caissier_id,
            "ouverture": session.opened_at,
            "borne": borne,
        },
    ).scalar_one()
    return session.fonds_initial + int(mouvements)


@dataclass(frozen=True)
class ResultatFermeture:
    """Ce que la fermeture rend — l'écart est déjà calculé, jamais à recalculer côté écran."""

    session: CaisseSession
    solde_theorique: int
    ecart: int


def fermer_session(
    db: Session,
    courant: UtilisateurCourant,
    session_id: uuid.UUID,
    *,
    montant_reel: int,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> ResultatFermeture:
    """Ferme la session de L'ACTEUR : calcule et FIGE le solde théorique et l'écart, à cet
    instant précis. NE BLOQUE JAMAIS (CA2 : la politique de tolérance/motif viendra plus tard,
    sans jamais empêcher la fermeture elle-même — un caissier ne reste pas coincé au guichet).
    NE POSE AUCUNE ÉCRITURE (CA3)."""
    session = _charger_session_de_lacteur(db, courant, session_id, verrou=True)
    if session.status != "ouverte":
        raise SessionDejaFermeeError(
            f"Cette session est déjà fermée depuis le {session.closed_at}."
        )

    maintenant = db.execute(text("SELECT NOW()")).scalar_one()
    theorique = calculer_solde_theorique(db, session, jusqu_a=maintenant)
    ecart = montant_reel - theorique

    avant = {"status": session.status}
    session.montant_reel_cloture = montant_reel
    session.solde_theorique_cloture = theorique
    session.ecart = ecart
    session.closed_at = maintenant
    session.status = "fermee"
    session.updated_by = courant.user_id
    db.flush()

    ecrire_audit(
        db,
        action="caisse.session.fermee",
        contexte=contexte,
        acteur_id=courant.user_id,
        resource_type=RESSOURCE,
        resource_id=session.id,
        agency_id=session.agency_id,
        old_values=avant,
        new_values={
            "status": "fermee",
            "fonds_initial": session.fonds_initial,
            "montant_reel_cloture": montant_reel,
            "solde_theorique_cloture": theorique,
            "ecart": ecart,
        },
    )
    return ResultatFermeture(session=session, solde_theorique=theorique, ecart=ecart)


def session_ouverte_de_lacteur(
    db: Session, courant: UtilisateurCourant
) -> CaisseSession | None:
    """La session actuellement ouverte de l'acteur, ou None — pour qu'un écran sache s'il doit
    proposer « ouvrir » ou « fermer » sans deviner."""
    return db.execute(
        select(CaisseSession).where(
            CaisseSession.caissier_id == courant.user_id, CaisseSession.status == "ouverte"
        )
    ).scalar_one_or_none()


def lire_session(db: Session, courant: UtilisateurCourant, session_id: uuid.UUID) -> CaisseSession:
    """Lit UNE session de l'acteur — mêmes règles IDOR que la fermeture."""
    return _charger_session_de_lacteur(db, courant, session_id)
