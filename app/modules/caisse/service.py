"""Caisse CA1 — ouverture, fermeture, calcul de l'écart. AUCUNE écriture comptable posée ici
(CA3, une fois le compte d'écart validé par l'expert).

CA2 (migration 0043) : seuil de tolérance (`caisse.parametres`, voir `parametres.py`), motif
obligatoire au-delà, validation a posteriori du responsable. NE BLOQUE TOUJOURS PAS la fermeture
— décision actée dès l'analyse initiale, jamais remise en cause : empêcher un caissier de
rentrer chez lui a un coût opérationnel réel qu'un motif à saisir n'a pas. Le statut « à
valider » n'est JAMAIS stocké — il se DÉRIVE (fermée + `abs(ecart) > seuil` + `valide_le IS
NULL`), même philosophie que `calculer_solde_theorique` : un calcul, jamais un cache qui
pourrait diverger du seuil courant si celui-ci change après coup.

LE CALCUL CENTRAL (`calculer_solde_theorique`) est un calcul DÉRIVÉ, jamais un solde stocké
qu'on relit — même discipline que `epargne.rapprochement.rapprocher()` : on interroge les
écritures VALIDÉES du compte de caisse (`comptabilite.journal_lines`/`journal_entries`),
fenêtrées entre l'ouverture de la session et l'instant demandé, filtrées sur LE CAISSIER de
cette session (`journal_entries.created_by`) — pas sur tout le compte, qui est partagé par
l'agence entière si plusieurs caissiers travaillent en parallèle (décision : une session par
caissier, pas par agence).

UNE SEULE SESSION OUVERTE PAR CAISSIER : vérifié ici (message clair) ET en base (index unique
partiel `uq_caisse_sessions_caissier_ouverte`, dernier rempart si ce contrôle était contourné).

LECTURE : deux publics, un seul chemin (`lire_session`/`lister_sessions_manquantes`). Le
CAISSIER voit TOUJOURS ses propres sessions (caisse.session.read, sans condition d'agence —
c'est SA donnée, comme consulter sa propre fiche). Un tiers (responsable/audit/direction) ne
voit une session qui n'est pas la sienne que s'il détient caisse.session.read.autres ET qu'elle
est dans son périmètre (condition_perimetre : son agence, ou tout le réseau pour voit_tout).
Sert la lettre de demande d'explication (manquant à la fermeture) — document, pas un
garde-fou : aucune écriture, aucun blocage, orthogonal à CA2 (seuil/motif, pas construit).
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.modules.audit.service import CONTEXTE_VIDE, ContexteRequete, ecrire_audit
from app.modules.caisse.ecart_operations import RattachementEcartManquantError, poser_ecriture_ecart
from app.modules.caisse.models import CaisseParametres, CaisseSession, Poste, PosteAssignation
from app.modules.caisse.postes import PosteIntrouvableError
from app.modules.comptabilite.models import Account
from app.modules.parameters.models import Agency
from app.modules.security.autorisation import UtilisateurCourant
from app.modules.security.models import User

RESSOURCE = "caisse.session"

# Permission élargie : lire une session qui n'appartient pas à l'acteur (voir docstring module).
PERMISSION_LECTURE_AUTRES = "caisse.session.read.autres"

TAILLE_PAGE_DEFAUT = 25
TAILLE_PAGE_MAX = 100


class SessionDejaOuverteError(Exception):
    """Ce caissier a déjà une session ouverte — une seule à la fois."""


class SessionIntrouvableError(Exception):
    """Session inexistante, ou n'appartenant pas à l'acteur. -> 404 (jamais 403 : IDOR)."""


class SessionDejaFermeeError(Exception):
    """Cette session est déjà fermée — on ne referme pas ce qui l'est déjà."""


class RattachementManquantError(Exception):
    """Le poste choisi n'a pas de compte de caisse rattaché (paramétrage)."""


class AucuneSessionOuverteError(Exception):
    """Aucun guichet ne peut opérer en espèces sans une session de caisse ouverte pour ce
    caissier — le tiroir doit être ouvert avant tout mouvement."""


class MotifRequisError(Exception):
    """L'écart dépasse le seuil de tolérance (CA2) : un motif est requis pour fermer la
    session. Ne bloque PAS la fermeture — le caissier resoumet avec un motif, rien de plus."""


class SessionDejaValideeError(Exception):
    """L'écart de cette session a déjà été validé — on ne valide pas deux fois."""


class EcartNonSignificatifError(Exception):
    """Cette session n'a pas d'écart au-delà du seuil de tolérance : rien à valider."""


def seuil_tolerance(db: Session) -> int:
    """Le seuil de tolérance courant (CA2). 500 F si `caisse.parametres` n'a pas encore été
    seedée — cohérent avec le défaut posé par la migration 0043 elle-même (`server_default`),
    jamais un nombre magique recopié à la main : si le défaut de la migration change un jour,
    celui-ci doit changer avec lui, pas être découvert en désaccord silencieux."""
    seuil = db.execute(select(CaisseParametres.seuil_tolerance).limit(1)).scalar_one_or_none()
    return seuil if seuil is not None else 500


def session_a_valider(session: CaisseSession, seuil: int) -> bool:
    """LE calcul dérivé (CA2) : une session est « à valider » si elle est FERMÉE, que son écart
    dépasse le seuil de tolérance (manquant ET excédent comptent — la matérialité comptable ne
    connaît pas de sens), et qu'elle n'a pas déjà été validée. Fonction PURE, réutilisée par le
    filtre SQL de `lister_sessions_a_valider` ET par `_vers_schema` (router) pour exposer le
    même booléen sur la fiche d'une session unique — un seul endroit qui sait ce qu'« à valider »
    veut dire, jamais deux implémentations qui pourraient diverger."""
    return (
        session.status == "fermee"
        and session.ecart is not None
        and abs(session.ecart) > seuil
        and session.valide_le is None
    )


def ouvrir_session(
    db: Session,
    courant: UtilisateurCourant,
    *,
    poste_id: uuid.UUID,
    fonds_initial: int,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> CaisseSession:
    """Ouvre une session POUR L'ACTEUR LUI-MÊME (`caissier_id = courant.user_id`) — jamais pour
    un autre, jamais un champ soumis par le client. Refuse si une session est déjà ouverte pour
    lui (message clair ; l'index unique partiel est le dernier rempart en base si ce contrôle
    était contourné par un appel concurrent).

    `poste_id` TOUJOURS soumis par le client (Bloc C) — jamais déduit ici, même quand l'acteur
    n'a qu'un seul poste assigné (voir `schemas.OuvertureSession`, motif détaillé). Le poste
    doit être ACTIF, ASSIGNÉ à l'acteur (`PosteAssignation`) ET dans l'agence COURANTE de sa
    session (`courant.agency_id`, pas seulement une agence où il est habilité — une session de
    caisse s'ouvre POUR l'agence où l'on travaille aujourd'hui) : filtré par un id FIXE (clé
    primaire), la requête ne peut plus jamais rendre deux lignes — l'ancienne ambiguïté du
    Bloc A/B (résolution automatique sur toute l'agence) disparaît structurellement, pas par un
    garde-fou en plus. Hors périmètre ou inexistant -> `PosteIntrouvableError` (404, jamais
    403 : IDOR, on ne révèle pas qu'un poste hors de portée existe).

    `compte_caisse_id` est ANCRÉ ici, copié depuis le POSTE choisi à cet instant — jamais
    recalculé ensuite, même si le rattachement change après coup."""
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

    poste = db.execute(
        select(Poste)
        .join(PosteAssignation, PosteAssignation.poste_id == Poste.id)
        .where(
            Poste.id == poste_id,
            Poste.agency_id == courant.agency_id,
            Poste.is_active.is_(True),
            PosteAssignation.user_id == courant.user_id,
        )
    ).scalar_one_or_none()
    if poste is None:
        raise PosteIntrouvableError()
    if poste.compte_caisse_id is None:
        raise RattachementManquantError(
            "ce poste de caisse n'a pas de compte rattaché (paramétrage)"
        )

    session = CaisseSession(
        agency_id=courant.agency_id,
        caissier_id=courant.user_id,
        poste_id=poste.id,
        compte_caisse_id=poste.compte_caisse_id,
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


def resoudre_session_active(db: Session, caissier_id: uuid.UUID | None) -> CaisseSession:
    """LE point de contrôle centralisé (Bloc C2) que chaque guichet (parts C3, épargne C4,
    crédit décaissement C5, crédit remboursement C6) appelle dans la branche où IL résout
    lui-même le compte de caisse (ex. `compte_debit is None`) — jamais en tête de fonction.
    C'est cette place, pas une autre, qui exempte structurellement le prélèvement automatique
    (CR5d) : il fournit toujours `compte_source_id`/`compte_debit` explicitement et n'atteint
    jamais cette branche, donc jamais cet appel — pas un cas spécial ajouté après coup, une
    conséquence de où le contrôle est posé.

    Prend `caissier_id: uuid.UUID | None`, pas `UtilisateurCourant` : `decaisser()` et
    `rembourser()` portent `par: uuid.UUID | None` (jamais un `UtilisateurCourant` complet en
    mode 'caisse') ; élargir leur signature publique pour ce seul besoin aurait été un
    changement plus large que nécessaire. `None` -> AUCUNE session ne peut lui correspondre
    (`caissier_id` n'est jamais NULL en base) : un décaissement/remboursement sans acteur
    identifié est refusé au même titre qu'une session réellement absente — un mouvement de
    caisse anonyme n'a pas de sens (en production, le routeur fournit toujours
    `courant.user_id`, jamais `None`).

    `compte_caisse_id` de la session retournée est déjà ANCRÉ (voir `ouvrir_session`) — jamais
    recalculé ici, même si le rattachement du poste a changé depuis l'ouverture.

    Aucune session ouverte -> `AucuneSessionOuverteError` : pas un objet manquant (donc pas
    404), un état préalable non rempli — même famille que SessionDejaOuverteError /
    SessionDejaFermeeError, laissée au routeur appelant de traduire en 422."""
    session = db.execute(
        select(CaisseSession).where(
            CaisseSession.caissier_id == caissier_id, CaisseSession.status == "ouverte"
        )
    ).scalar_one_or_none()
    if session is None:
        raise AucuneSessionOuverteError(
            "Aucune session de caisse ouverte : ouvrez votre session avant d'effectuer cette "
            "opération."
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
    motif: str | None = None,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> ResultatFermeture:
    """Ferme la session de L'ACTEUR : calcule et FIGE le solde théorique et l'écart, à cet
    instant précis. NE BLOQUE JAMAIS sur l'écart (CA2) — un caissier ne reste jamais coincé au
    guichet. NE POSE AUCUNE ÉCRITURE (CA3).

    `motif` (CA2) : optionnel si `abs(ecart) <= seuil_tolerance`, OBLIGATOIRE au-delà —
    `MotifRequisError` sinon, AVANT toute mutation (refus propre, rien n'est écrit). Le seuil est
    lu à CET instant (`seuil_tolerance`), jamais mis en cache : un changement de seuil par le
    comptable s'applique immédiatement aux fermetures suivantes."""
    session = _charger_session_de_lacteur(db, courant, session_id, verrou=True)
    if session.status != "ouverte":
        raise SessionDejaFermeeError(
            f"Cette session est déjà fermée depuis le {session.closed_at}."
        )

    maintenant = db.execute(text("SELECT NOW()")).scalar_one()
    theorique = calculer_solde_theorique(db, session, jusqu_a=maintenant)
    ecart = montant_reel - theorique

    seuil = seuil_tolerance(db)
    motif_propre = motif.strip() if motif else None
    if abs(ecart) > seuil and not motif_propre:
        raise MotifRequisError(
            f"Écart de {abs(ecart)} F, au-delà du seuil de tolérance de {seuil} F : un motif "
            "est requis pour fermer cette session."
        )

    avant = {"status": session.status}
    session.montant_reel_cloture = montant_reel
    session.solde_theorique_cloture = theorique
    session.ecart = ecart
    session.motif_ecart = motif_propre
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
            "motif_ecart": motif_propre,
        },
    )
    return ResultatFermeture(session=session, solde_theorique=theorique, ecart=ecart)


def valider_ecart(
    db: Session,
    courant: UtilisateurCourant,
    session_id: uuid.UUID,
    *,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> CaisseSession:
    """Valide A POSTERIORI l'écart d'une session FERMÉE, au-delà du seuil de tolérance (CA2) —
    une TRACE consultable, JAMAIS un blocage de la fermeture (déjà actée). CA3 (migration
    0044) : pose AUSSI la pièce de régularisation (`ecart_operations.poser_ecriture_ecart`),
    dans la MÊME transaction que la trace — si le compte de l'écart n'est pas rattaché, TOUT
    est refusé, y compris la trace de validation elle-même (transaction unique : jamais une
    session marquée validée sans son écriture posée, et réciproquement).

    Périmètre vérifié ICI, au niveau de l'OBJET (IDOR) — `caisse.session.valider` est déjà
    exigé par le routeur, mais un responsable ne valide que les sessions de SON périmètre.
    Hors périmètre ou inexistante -> `SessionIntrouvableError` (404, jamais 403). Déjà validée
    -> `SessionDejaValideeError`. Écart sous le seuil -> `EcartNonSignificatifError` (rien à
    valider — ce n'est pas un état auquel l'écran devrait pouvoir mener, mais un appel direct à
    l'API ne doit pas pouvoir valider n'importe quoi). Compte de l'écart non rattaché ->
    `RattachementEcartManquantError` (paramétrage incomplet, refus propre)."""
    session = db.execute(
        select(CaisseSession)
        .where(CaisseSession.id == session_id, courant.condition_perimetre(CaisseSession.agency_id))
        .with_for_update()
    ).scalar_one_or_none()
    if session is None:
        raise SessionIntrouvableError()
    if session.valide_le is not None:
        raise SessionDejaValideeError(
            f"L'écart de cette session a déjà été validé le {session.valide_le}."
        )
    seuil = seuil_tolerance(db)
    if not session_a_valider(session, seuil):
        raise EcartNonSignificatifError(
            "Cette session n'a pas d'écart au-delà du seuil de tolérance : rien à valider."
        )

    # CA3 : la pièce D'ABORD — si le compte de l'écart manque, RIEN ne doit être marqué validé
    # (voir docstring). `poser_ecriture_ecart` lève RattachementEcartManquantError le cas
    # échéant, propagée telle quelle : aucune mutation de la session n'a encore eu lieu ici.
    config = db.execute(select(CaisseParametres).limit(1)).scalar_one_or_none()
    if config is None:
        raise RattachementEcartManquantError(
            "le seuil de tolérance de caisse n'est pas paramétré : contactez le comptable "
            "avant de valider cette session."
        )
    piece = poser_ecriture_ecart(db, session, config, par=courant.user_id, contexte=contexte)

    maintenant = db.execute(text("SELECT NOW()")).scalar_one()
    session.valide_le = maintenant
    session.valide_par = courant.user_id
    session.updated_by = courant.user_id
    db.flush()

    ecrire_audit(
        db,
        action="caisse.session.ecart_valide",
        contexte=contexte,
        acteur_id=courant.user_id,
        resource_type=RESSOURCE,
        resource_id=session.id,
        agency_id=session.agency_id,
        old_values={"valide_le": None},
        new_values={
            "valide_le": str(maintenant),
            "ecart": session.ecart,
            "entry_number": piece.entry_number if piece else None,
        },
    )
    return session


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


def _condition_lecture(courant: UtilisateurCourant) -> ColumnElement[bool]:
    """La condition de visibilité en LECTURE (lire_session / lister_sessions_manquantes) :
    TOUJOURS ses propres sessions ; en plus, celles du périmètre si caisse.session.read.autres
    est détenue. Point UNIQUE — si demain un troisième public apparaît, c'est ici qu'il se
    branche, pas recopié dans chaque fonction."""
    if PERMISSION_LECTURE_AUTRES not in courant.permissions:
        return CaisseSession.caissier_id == courant.user_id
    return or_(
        CaisseSession.caissier_id == courant.user_id,
        courant.condition_perimetre(CaisseSession.agency_id),
    )


def lire_session(db: Session, courant: UtilisateurCourant, session_id: uuid.UUID) -> CaisseSession:
    """Lit UNE session : TOUJOURS la sienne ; une autre SEULEMENT avec caisse.session.read.autres
    ET dans le périmètre de l'acteur. Hors périmètre ou inexistante -> SessionIntrouvableError
    (404, jamais 403 : IDOR, on ne révèle pas qu'une session hors de portée existe)."""
    session = db.execute(
        select(CaisseSession).where(
            CaisseSession.id == session_id, _condition_lecture(courant)
        )
    ).scalar_one_or_none()
    if session is None:
        raise SessionIntrouvableError()
    return session


@dataclass(frozen=True)
class LigneSessionManquante:
    """Une session fermée avec un MANQUANT (écart < 0) — identité déjà résolue, comme
    `audit.consultation.LigneAudit`. Jamais l'excédent : hors périmètre de la lettre de demande
    d'explication (décision explicite, à confirmer séparément si un jour souhaité)."""

    id: uuid.UUID
    caissier_id: uuid.UUID
    caissier_nom: str
    agency_id: uuid.UUID
    agency_nom: str
    compte_caisse_number: str
    fonds_initial: int
    opened_at: datetime
    closed_at: datetime
    montant_reel_cloture: int
    solde_theorique_cloture: int
    ecart: int


@dataclass(frozen=True)
class PageSessionsManquantes:
    lignes: Sequence[LigneSessionManquante]
    total: int
    page: int
    taille: int


def lister_sessions_manquantes(
    db: Session,
    courant: UtilisateurCourant,
    *,
    page: int = 1,
    taille: int = TAILLE_PAGE_DEFAUT,
) -> PageSessionsManquantes:
    """Sessions FERMÉES avec un manquant, dans le périmètre de lecture de l'acteur (voir
    `_condition_lecture`) — c'est ce qui permet de retrouver une lettre de demande
    d'explication plus tard, sans dépendre d'un lien reçu au moment de la fermeture (le
    caissier retrouve les SIENNES ; un responsable/audit/direction, celles de son périmètre).
    Triée par fermeture la plus RÉCENTE d'abord."""
    taille = max(1, min(taille, TAILLE_PAGE_MAX))
    page = max(1, page)

    conditions = (
        CaisseSession.status == "fermee",
        CaisseSession.ecart < 0,
        _condition_lecture(courant),
    )

    total = db.execute(
        select(func.count()).select_from(CaisseSession).where(*conditions)
    ).scalar_one()

    lignes = db.execute(
        select(
            CaisseSession.id,
            CaisseSession.caissier_id,
            func.concat_ws(" ", User.first_name, User.last_name).label("caissier_nom"),
            CaisseSession.agency_id,
            Agency.name.label("agency_nom"),
            Account.account_number.label("compte_caisse_number"),
            CaisseSession.fonds_initial,
            CaisseSession.opened_at,
            CaisseSession.closed_at,
            CaisseSession.montant_reel_cloture,
            CaisseSession.solde_theorique_cloture,
            CaisseSession.ecart,
        )
        .select_from(CaisseSession)
        .join(User, User.id == CaisseSession.caissier_id)
        .join(Agency, Agency.id == CaisseSession.agency_id)
        .join(Account, Account.id == CaisseSession.compte_caisse_id)
        .where(*conditions)
        .order_by(CaisseSession.closed_at.desc())
        .offset((page - 1) * taille)
        .limit(taille)
    ).all()

    return PageSessionsManquantes(
        lignes=[LigneSessionManquante(**ligne._mapping) for ligne in lignes],
        total=total,
        page=page,
        taille=taille,
    )


@dataclass(frozen=True)
class LigneSessionAValider:
    """Une session fermée dont l'écart dépasse le seuil de tolérance (CA2), pas encore
    validée — manquant ET excédent comptent ici (contrairement à `LigneSessionManquante`,
    réservée à la lettre de demande d'explication)."""

    id: uuid.UUID
    caissier_id: uuid.UUID
    caissier_nom: str
    agency_id: uuid.UUID
    agency_nom: str
    compte_caisse_number: str
    fonds_initial: int
    opened_at: datetime
    closed_at: datetime
    montant_reel_cloture: int
    solde_theorique_cloture: int
    ecart: int
    motif_ecart: str | None


@dataclass(frozen=True)
class PageSessionsAValider:
    lignes: Sequence[LigneSessionAValider]
    total: int
    page: int
    taille: int
    seuil_tolerance: int


def lister_sessions_a_valider(
    db: Session,
    courant: UtilisateurCourant,
    *,
    page: int = 1,
    taille: int = TAILLE_PAGE_DEFAUT,
) -> PageSessionsAValider:
    """Sessions FERMÉES avec un écart AU-DELÀ DU SEUIL, non encore validées, dans le périmètre
    de lecture de l'acteur (voir `_condition_lecture`) — la file d'attente du responsable.
    Le statut « à valider » n'existe dans AUCUNE colonne : il est reconstruit ici par le MÊME
    calcul que `session_a_valider` (traduit en filtre SQL), pour qu'un changement de seuil par
    le comptable s'applique immédiatement à cette liste, sans purge ni recalcul de rien.
    Triée par fermeture la plus RÉCENTE d'abord."""
    taille = max(1, min(taille, TAILLE_PAGE_MAX))
    page = max(1, page)
    seuil = seuil_tolerance(db)

    conditions = (
        CaisseSession.status == "fermee",
        func.abs(CaisseSession.ecart) > seuil,
        CaisseSession.valide_le.is_(None),
        _condition_lecture(courant),
    )

    total = db.execute(
        select(func.count()).select_from(CaisseSession).where(*conditions)
    ).scalar_one()

    lignes = db.execute(
        select(
            CaisseSession.id,
            CaisseSession.caissier_id,
            func.concat_ws(" ", User.first_name, User.last_name).label("caissier_nom"),
            CaisseSession.agency_id,
            Agency.name.label("agency_nom"),
            Account.account_number.label("compte_caisse_number"),
            CaisseSession.fonds_initial,
            CaisseSession.opened_at,
            CaisseSession.closed_at,
            CaisseSession.montant_reel_cloture,
            CaisseSession.solde_theorique_cloture,
            CaisseSession.ecart,
            CaisseSession.motif_ecart,
        )
        .select_from(CaisseSession)
        .join(User, User.id == CaisseSession.caissier_id)
        .join(Agency, Agency.id == CaisseSession.agency_id)
        .join(Account, Account.id == CaisseSession.compte_caisse_id)
        .where(*conditions)
        .order_by(CaisseSession.closed_at.desc())
        .offset((page - 1) * taille)
        .limit(taille)
    ).all()

    return PageSessionsAValider(
        lignes=[LigneSessionAValider(**ligne._mapping) for ligne in lignes],
        total=total,
        page=page,
        taille=taille,
        seuil_tolerance=seuil,
    )
