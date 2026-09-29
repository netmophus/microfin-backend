"""Chantier coffre-fort/caisses, sous-chantier 2, Lot 1 : transferts de fonds entre niveaux de
caisse ADJACENTS (coffre↔principale, principale↔secondaire) — mécanique ENVOI/RÉCEPTION, compte
de liaison, double regard. Voir docstring de la migration 0047 pour le détail des CHECK (dernier
rempart) que ce module respecte en premier, avec un message clair.

ADJACENCE : donnée statique (`ADJACENCES`), pas une table — même patron que `TRANSITIONS` dans
`tiers/cycle_de_vie.py`. Coffre↔secondaire direct n'existe pas.

COMPTES ANCRÉS À L'INITIATION : `compte_source_id`/`compte_destination_id` résolus une fois, ici,
jamais recalculés ensuite (même discipline que `CaisseSession.compte_caisse_id`).

CONTRÔLE À L'OBJET, PAR NIVEAU (sous-chantier 3, Lot A — modèle B de responsabilité) :
`_verifier_autorise_sur_niveau` est LE point d'ancrage unique, appelé aux deux mêmes endroits
depuis le début (`initier_transfert` pour la source, `receptionner_transfert` pour la
destination) — jamais un troisième point, jamais dupliqué :
  - SECONDAIRE : un POSTE est le compte d'une session CaisseSession active —
    `resoudre_session_active`/`calculer_solde_theorique` filtrent sur
    `journal_entries.created_by = caissier de la session`. Pour que le solde théorique du
    caissier reste exact SANS toucher à ce calcul existant (CA1, déjà testé), ce module exige
    que la personne qui envoie ou réceptionne soit LE CAISSIER TITULAIRE de la session
    actuellement ouverte sur CE poste — jamais un responsable à sa place. `created_by` de
    l'écriture posée est donc TOUJOURS l'acteur agissant sur son propre compte, une conséquence
    de ce contrôle, pas un cas particulier ajouté après coup. INCHANGÉ depuis le Lot 1.
  - PRINCIPALE : responsabilité NOMINATIVE — l'acteur doit être LE caissier principal DÉSIGNÉ de
    cette agence (`caisse.caissiers_principaux`, migration 0048). Aucune désignation -> refus
    propre (`CaissierPrincipalNonDesigneError`), jamais un caissier deviné.
  - COFFRE : responsabilité de RÔLE, pas nominative — l'acteur doit détenir `caisse.coffre.gerer`
    ET son agence courante doit être CELLE du transfert, une ÉGALITÉ STRICTE, délibérément PAS
    `condition_perimetre` : un rôle réseau (direction, audit) ne doit pas pouvoir manipuler le
    coffre d'une agence qu'il ne dirige pas — voir n'est pas agir, décision actée explicitement.

DOUBLE REGARD : receveur != envoyeur, vérifié ICI (message clair) — le CHECK
`double_regard_envoyeur_receveur` de la migration est le dernier rempart si ce contrôle était
contourné.

PONT COMPTABLE (option B, compte de liaison, actée) :
  - `initier_transfert` pose l'écriture d'ENVOI (D TRANSIT / C SOURCE, montant envoyé) via le
    moteur générique `poser_depuis_schema` (montant uniforme sur les 2 lignes : cas normal).
  - `receptionner_transfert` pose l'écriture de RÉCEPTION. Sans écart (compté = envoyé), montant
    uniforme -> `poser_depuis_schema` aurait suffi, mais la géométrie diffère dès qu'un écart
    existe (DESTINATION et TRANSIT portent alors des montants différents, plus une 3e ligne
    ECART) : ce module construit les lignes lui-même et pose la pièce via le moteur BAS NIVEAU
    (`ecritures.creer_brouillon`/`ecritures.valider` — le même que `poser_depuis_schema` appelle
    en interne), qui porte tous les garde-fous réels (équilibre, exercice ouvert, immuabilité).

REFUS PROPRE SI PARAMÉTRAGE INCOMPLET (compte de transit ou compte d'écart de transfert non
rattaché) : vérifié AVANT toute écriture, rien n'est créé — même discipline que
`ecart_operations.poser_ecriture_ecart`.

LECTURE (Lot 2) : `lire_transfert` rend l'objet ORM brut, périmètre déjà vérifié (le routeur
résout les noms, même partage de responsabilité que `caisse/service.py::lire_session` /
`router.py::_vers_schema`). `lister_transferts` résout les noms ICI, en un seul aller (même
patron que `service.py::lister_sessions_manquantes`) — retourne des lignes déjà lisibles, jamais
un UUID nu. Cloisonné à l'agence dans les deux cas (`condition_perimetre`), jamais
`perimetre.reseau` : un transfert reste local à une agence (aucun rôle de ce sous-chantier ne
porte cette portée)."""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import cast

from sqlalchemy import case, func, or_, select, text
from sqlalchemy.orm import Session, aliased

from app.modules.audit.service import CONTEXTE_VIDE, ContexteRequete, ecrire_audit
from app.modules.caisse.models import (
    CaisseParametres,
    CaisseSession,
    CaissierPrincipal,
    NiveauCaisse,
    Poste,
    Transfert,
)
from app.modules.caisse.service import TAILLE_PAGE_DEFAUT, TAILLE_PAGE_MAX
from app.modules.comptabilite import ecritures
from app.modules.comptabilite.ecritures import LigneSaisie
from app.modules.comptabilite.models import Account, EntrySchema
from app.modules.comptabilite.schemas_ecriture import (
    SchemaIntrouvableError,
    SchemaSansJournalError,
    poser_depuis_schema,
)
from app.modules.parameters.models import Agency
from app.modules.security.autorisation import UtilisateurCourant
from app.modules.security.models import User

RESSOURCE = "caisse.transfert"

CODE_ENVOI = "caisse.transfert.envoi"
CODE_RECEPTION = "caisse.transfert.reception"

NIVEAU_COFFRE = "coffre"
NIVEAU_PRINCIPALE = "principale"
NIVEAU_SECONDAIRE = "secondaire"

# Sous-chantier 3 : responsabilité de RÔLE sur le coffre — RESPONSABLE_AGENCE, SON agence
# uniquement (égalité stricte, voir _verifier_responsable_coffre).
PERMISSION_COFFRE = "caisse.coffre.gerer"

# Adjacence statique — donnée, pas une table (même patron que TRANSITIONS dans
# tiers/cycle_de_vie.py). Coffre<->secondaire direct n'existe pas.
ADJACENCES: frozenset[tuple[str, str]] = frozenset(
    {
        (NIVEAU_COFFRE, NIVEAU_PRINCIPALE),
        (NIVEAU_PRINCIPALE, NIVEAU_COFFRE),
        (NIVEAU_PRINCIPALE, NIVEAU_SECONDAIRE),
        (NIVEAU_SECONDAIRE, NIVEAU_PRINCIPALE),
    }
)


class TransfertError(Exception):
    """Base des erreurs métier de ce module."""


class AdjacenceInvalideError(TransfertError):
    """Les deux niveaux ne sont pas adjacents (ex. coffre -> secondaire direct)."""

    def __init__(self, niveau_source: str, niveau_destination: str) -> None:
        super().__init__(
            f"Un transfert ne peut se faire qu'entre niveaux adjacents : « {niveau_source} » "
            f"et « {niveau_destination} » ne le sont pas (coffre↔principale, "
            "principale↔secondaire uniquement)."
        )


class PosteRequisError(TransfertError):
    """Le niveau « secondaire » exige un poste précis."""

    def __init__(self) -> None:
        super().__init__("le niveau « secondaire » exige de préciser un poste de caisse.")


class PosteInattenduError(TransfertError):
    """Un poste a été soumis pour un niveau qui n'en prend pas (coffre/principale)."""

    def __init__(self) -> None:
        super().__init__(
            "un poste de caisse n'est attendu que pour le niveau « secondaire »."
        )


class PosteIntrouvableError(TransfertError):
    """Poste inexistant, inactif, ou hors de l'agence du transfert."""


class CompteNonParametreError(TransfertError):
    """Le niveau visé n'a pas de compte de caisse rattaché (paramétrage incomplet)."""

    def __init__(self, niveau: str) -> None:
        super().__init__(
            f"le niveau « {niveau} » n'a pas de compte de caisse rattaché : contactez le "
            "comptable avant d'effectuer ce transfert."
        )


class SessionCaissierRequiseError(TransfertError):
    """Côté secondaire, seul le caissier titulaire de la session ouverte sur ce poste agit."""

    def __init__(self) -> None:
        super().__init__(
            "seul le caissier ayant une session de caisse ouverte sur ce poste peut envoyer "
            "ou réceptionner un transfert sur son compte."
        )


class CaissierPrincipalNonDesigneError(TransfertError):
    """Aucun caissier principal désigné pour cette agence — paramétrage incomplet, transitoire."""

    def __init__(self) -> None:
        super().__init__(
            "aucun caissier principal n'est désigné pour cette agence : contactez le "
            "responsable d'agence avant d'effectuer ce mouvement."
        )


class CaissierPrincipalRequisError(TransfertError):
    """Un caissier principal est désigné, mais l'acteur n'est pas cette personne."""

    def __init__(self) -> None:
        super().__init__(
            "seul le caissier principal désigné de cette agence peut envoyer ou réceptionner "
            "un transfert sur la caisse principale."
        )


class ResponsableCoffreRequisError(TransfertError):
    """L'acteur n'a pas la responsabilité du coffre de cette agence."""

    def __init__(self) -> None:
        super().__init__(
            "seul le responsable de CETTE agence peut envoyer ou réceptionner un transfert sur "
            "le coffre."
        )


class MontantInvalideError(TransfertError):
    """Montant nul, négatif, ou non entier."""


class CompteTransitNonParametreError(TransfertError):
    """Le compte de liaison (transit) n'est pas rattaché."""

    def __init__(self) -> None:
        super().__init__(
            "le compte de liaison des transferts n'est pas paramétré : contactez le comptable."
        )


class CompteEcartTransfertNonParametreError(TransfertError):
    """Le compte de l'écart de transfert (manquant ou excédent) n'est pas rattaché."""

    def __init__(self, nature: str) -> None:
        super().__init__(
            f"le compte de l'écart de transfert ({nature}) n'est pas paramétré : contactez le "
            "comptable avant de valider cette réception."
        )


class TransfertIntrouvableError(TransfertError):
    """Transfert inexistant, ou hors périmètre de l'acteur. -> 404, jamais 403 (IDOR)."""


class TransfertDejaReceptionneError(TransfertError):
    """Ce transfert a déjà été réceptionné — on ne le réceptionne pas deux fois."""

    def __init__(self, receptionne_le: object) -> None:
        super().__init__(f"ce transfert a déjà été réceptionné le {receptionne_le}.")


class DoubleRegardError(TransfertError):
    """Le receveur ne peut pas être l'envoyeur du même transfert."""

    def __init__(self) -> None:
        super().__init__(
            "vous ne pouvez pas réceptionner un transfert que vous avez vous-même envoyé : "
            "un autre agent doit confirmer la réception."
        )


def _lire_parametres(db: Session) -> CaisseParametres | None:
    return db.execute(select(CaisseParametres).limit(1)).scalar_one_or_none()


def _resoudre_compte_et_poste(
    db: Session, *, agency_id: uuid.UUID, niveau: str, poste_id: uuid.UUID | None
) -> tuple[uuid.UUID, uuid.UUID | None]:
    """Résout (compte_id, poste_id) pour CE niveau, dans CETTE agence. Lève si le niveau n'est
    pas paramétré, ou si le poste soumis est absent/incohérent avec ce que le niveau exige."""
    if niveau == NIVEAU_SECONDAIRE:
        if poste_id is None:
            raise PosteRequisError()
        poste = db.execute(
            select(Poste).where(
                Poste.id == poste_id, Poste.agency_id == agency_id, Poste.is_active.is_(True)
            )
        ).scalar_one_or_none()
        if poste is None:
            raise PosteIntrouvableError()
        if poste.compte_caisse_id is None:
            raise CompteNonParametreError(niveau)
        return poste.compte_caisse_id, poste.id

    if poste_id is not None:
        raise PosteInattenduError()
    ligne = db.execute(
        select(NiveauCaisse).where(
            NiveauCaisse.agency_id == agency_id, NiveauCaisse.niveau == niveau
        )
    ).scalar_one_or_none()
    if ligne is None or ligne.compte_caisse_id is None:
        raise CompteNonParametreError(niveau)
    return ligne.compte_caisse_id, None


def _verifier_caissier_titulaire(
    db: Session, courant: UtilisateurCourant, poste_id: uuid.UUID
) -> None:
    """Côté secondaire : l'acteur doit être le caissier ayant actuellement une session ouverte
    sur CE poste — jamais un responsable à sa place (voir docstring module)."""
    session = db.execute(
        select(CaisseSession).where(
            CaisseSession.poste_id == poste_id, CaisseSession.status == "ouverte"
        )
    ).scalar_one_or_none()
    if session is None or session.caissier_id != courant.user_id:
        raise SessionCaissierRequiseError()


def _verifier_caissier_principal(
    db: Session, courant: UtilisateurCourant, agency_id: uuid.UUID
) -> None:
    """Côté principale : l'acteur doit être LE caissier principal DÉSIGNÉ de cette agence
    (responsabilité nominative — voir `caissiers_principaux.py`). Aucune désignation -> refus
    propre, jamais un caissier deviné."""
    designation = db.execute(
        select(CaissierPrincipal).where(CaissierPrincipal.agency_id == agency_id)
    ).scalar_one_or_none()
    if designation is None:
        raise CaissierPrincipalNonDesigneError()
    if designation.user_id != courant.user_id:
        raise CaissierPrincipalRequisError()


def _verifier_responsable_coffre(courant: UtilisateurCourant, agency_id: uuid.UUID) -> None:
    """Côté coffre : responsabilité de RÔLE (RESPONSABLE_AGENCE), pas nominative — permission
    `caisse.coffre.gerer` ET agence courante STRICTEMENT égale à celle du transfert (jamais
    `condition_perimetre`, qui tolère `voit_tout` : un rôle réseau ne dirige pas cette agence au
    quotidien, voir n'est pas agir — décision actée explicitement)."""
    if PERMISSION_COFFRE not in courant.permissions or courant.agency_id != agency_id:
        raise ResponsableCoffreRequisError()


def _verifier_autorise_sur_niveau(
    db: Session,
    courant: UtilisateurCourant,
    *,
    agency_id: uuid.UUID,
    niveau: str,
    poste_id: uuid.UUID | None,
) -> None:
    """LE point d'ancrage unique du contrôle à l'objet par niveau — appelé aux deux mêmes
    endroits depuis le Lot 1 (source à l'initiation, destination à la réception), jamais un
    troisième point. Voir docstring module pour le détail des trois régimes."""
    if niveau == NIVEAU_SECONDAIRE:
        assert poste_id is not None
        _verifier_caissier_titulaire(db, courant, poste_id)
    elif niveau == NIVEAU_PRINCIPALE:
        _verifier_caissier_principal(db, courant, agency_id)
    elif niveau == NIVEAU_COFFRE:
        _verifier_responsable_coffre(courant, agency_id)


def _maintenant(db: Session) -> datetime:
    return cast(datetime, db.execute(text("SELECT NOW()")).scalar_one())


def _jour(db: Session) -> date:
    return cast(date, db.execute(text("SELECT CURRENT_DATE")).scalar_one())


def _agence_courante(courant: UtilisateurCourant) -> uuid.UUID:
    """Cloisonné sans agence courante : il ne peut initier ni réceptionner aucun transfert —
    même discipline que `tiers/service.py::_agence_de_creation`."""
    if courant.agency_id is None:
        raise TransfertError("aucune agence courante : reconnectez-vous.")
    return courant.agency_id


def initier_transfert(
    db: Session,
    courant: UtilisateurCourant,
    *,
    niveau_source: str,
    niveau_destination: str,
    poste_id: uuid.UUID | None,
    montant_envoye: int,
    motif: str,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> Transfert:
    """Initie un transfert POUR L'AGENCE COURANTE de l'acteur — jamais une agence soumise par le
    client (même discipline que `postes.creer`). Pose l'écriture d'envoi (D TRANSIT / C SOURCE)
    dans la MÊME opération : un transfert n'existe jamais sans son écriture déjà posée.

    `poste_id` : le poste concerné, requis SEULEMENT si `niveau_source` OU `niveau_destination`
    vaut « secondaire » (l'adjacence garantit qu'au plus un des deux l'est)."""
    if (niveau_source, niveau_destination) not in ADJACENCES:
        raise AdjacenceInvalideError(niveau_source, niveau_destination)
    if montant_envoye <= 0:
        raise MontantInvalideError("le montant envoyé doit être strictement positif.")

    agency_id = _agence_courante(courant)
    poste_source = poste_id if niveau_source == NIVEAU_SECONDAIRE else None
    poste_destination = poste_id if niveau_destination == NIVEAU_SECONDAIRE else None

    compte_source_id, poste_source_id = _resoudre_compte_et_poste(
        db, agency_id=agency_id, niveau=niveau_source, poste_id=poste_source
    )
    compte_destination_id, poste_destination_id = _resoudre_compte_et_poste(
        db, agency_id=agency_id, niveau=niveau_destination, poste_id=poste_destination
    )

    _verifier_autorise_sur_niveau(
        db, courant, agency_id=agency_id, niveau=niveau_source, poste_id=poste_source_id
    )

    config = _lire_parametres(db)
    if config is None or config.compte_transit_id is None:
        raise CompteTransitNonParametreError()
    compte_transit_id = config.compte_transit_id

    def _resoudre_role(role: str) -> uuid.UUID:
        if role == "TRANSIT":
            return compte_transit_id
        if role == "SOURCE":
            return compte_source_id
        raise TransfertError(f"rôle « {role} » inconnu pour l'envoi d'un transfert")

    piece_envoi = poser_depuis_schema(
        db,
        code=CODE_ENVOI,
        montant=montant_envoye,
        resoudre_role=_resoudre_role,
        entry_date=_jour(db),
        par=courant.user_id,
        description=f"Transfert de fonds — envoi ({niveau_source} -> {niveau_destination})",
        contexte=contexte,
    )

    transfert = Transfert(
        id=uuid.uuid4(),
        agency_id=agency_id,
        niveau_source=niveau_source,
        niveau_destination=niveau_destination,
        compte_source_id=compte_source_id,
        compte_destination_id=compte_destination_id,
        poste_source_id=poste_source_id,
        poste_destination_id=poste_destination_id,
        montant_envoye=montant_envoye,
        motif=motif,
        envoye_par=courant.user_id,
        journal_entry_envoi_id=piece_envoi.id,
        created_by=courant.user_id,
        updated_by=courant.user_id,
    )
    db.add(transfert)
    db.flush()

    ecrire_audit(
        db,
        action="caisse.transfert.envoye",
        contexte=contexte,
        acteur_id=courant.user_id,
        resource_type=RESSOURCE,
        resource_id=transfert.id,
        agency_id=agency_id,
        new_values={
            "niveau_source": niveau_source,
            "niveau_destination": niveau_destination,
            "montant_envoye": montant_envoye,
            "motif": motif,
        },
    )
    return transfert


def _charger_transfert_pour_reception(
    db: Session, courant: UtilisateurCourant, transfert_id: uuid.UUID
) -> Transfert:
    transfert = db.execute(
        select(Transfert)
        .where(Transfert.id == transfert_id, courant.condition_perimetre(Transfert.agency_id))
        .with_for_update()
    ).scalar_one_or_none()
    if transfert is None:
        raise TransfertIntrouvableError()
    return transfert


def receptionner_transfert(
    db: Session,
    courant: UtilisateurCourant,
    transfert_id: uuid.UUID,
    *,
    montant_compte: int,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> Transfert:
    """Réceptionne un transfert « en transit », dans le périmètre de l'acteur (agence). Pose
    l'écriture de réception (D DESTINATION / C TRANSIT, + ligne ECART si compté != envoyé) —
    AVANT toute mutation du transfert, transaction unique (si l'écriture ne se pose pas, le
    transfert reste « en transit », rien n'est écrit à moitié)."""
    if montant_compte < 0:
        raise MontantInvalideError("le montant compté ne peut pas être négatif.")

    transfert = _charger_transfert_pour_reception(db, courant, transfert_id)
    if transfert.statut != "en_transit":
        raise TransfertDejaReceptionneError(transfert.receptionne_le)
    if courant.user_id == transfert.envoye_par:
        raise DoubleRegardError()
    _verifier_autorise_sur_niveau(
        db,
        courant,
        agency_id=transfert.agency_id,
        niveau=transfert.niveau_destination,
        poste_id=transfert.poste_destination_id,
    )

    config = _lire_parametres(db)
    if config is None or config.compte_transit_id is None:
        raise CompteTransitNonParametreError()
    compte_transit_id = config.compte_transit_id

    diff = montant_compte - transfert.montant_envoye
    lignes: list[LigneSaisie] = [
        LigneSaisie(
            account_id=transfert.compte_destination_id,
            side="D",
            amount=montant_compte,
            label="Transfert (destination)",
        ),
        LigneSaisie(
            account_id=compte_transit_id,
            side="C",
            amount=transfert.montant_envoye,
            label="Transfert (compte de liaison)",
        ),
    ]
    if diff != 0:
        manquant = diff < 0
        nature = "manquant" if manquant else "excédent"
        compte_ecart_id = (
            config.compte_ecart_transfert_manquant_id
            if manquant
            else config.compte_ecart_transfert_excedent_id
        )
        if compte_ecart_id is None:
            raise CompteEcartTransfertNonParametreError(nature)
        lignes.append(
            LigneSaisie(
                account_id=compte_ecart_id,
                side="D" if manquant else "C",
                amount=abs(diff),
                label=f"Transfert (écart {nature})",
            )
        )

    schema = db.execute(
        select(EntrySchema).where(EntrySchema.code == CODE_RECEPTION, EntrySchema.is_active)
    ).scalar_one_or_none()
    if schema is None:
        raise SchemaIntrouvableError(f"aucun modèle d'écriture actif pour « {CODE_RECEPTION} »")
    if schema.journal_id is None:
        raise SchemaSansJournalError(f"le modèle « {CODE_RECEPTION} » n'a pas de journal rattaché")

    entry = ecritures.creer_brouillon(
        db,
        journal_id=schema.journal_id,
        entry_date=_jour(db),
        description=(
            f"Transfert de fonds — réception ({transfert.niveau_source} -> "
            f"{transfert.niveau_destination})"
        ),
        lignes=lignes,
        par=courant.user_id,
    )
    ecritures.valider(db, entry, courant.user_id, contexte=contexte)

    avant = {"statut": transfert.statut}
    maintenant = _maintenant(db)
    transfert.montant_compte = montant_compte
    transfert.statut = "receptionne"
    transfert.receptionne_par = courant.user_id
    transfert.receptionne_le = maintenant
    transfert.journal_entry_reception_id = entry.id
    transfert.updated_by = courant.user_id
    db.flush()

    ecrire_audit(
        db,
        action="caisse.transfert.receptionne",
        contexte=contexte,
        acteur_id=courant.user_id,
        resource_type=RESSOURCE,
        resource_id=transfert.id,
        agency_id=transfert.agency_id,
        old_values=avant,
        new_values={
            "statut": "receptionne",
            "montant_compte": montant_compte,
            "ecart": diff,
            "entry_number": entry.entry_number,
        },
    )
    return transfert


def lire_transfert(db: Session, courant: UtilisateurCourant, transfert_id: uuid.UUID) -> Transfert:
    """Un transfert, dans le périmètre de l'acteur (agence) — hors périmètre ou inexistant ->
    TransfertIntrouvableError (404, jamais 403 : IDOR, on ne révèle pas qu'un transfert d'une
    autre agence existe). Rend l'objet ORM brut ; le routeur résout les noms (même partage de
    responsabilité que `service.py::lire_session` / `router.py::_vers_schema`)."""
    transfert = db.execute(
        select(Transfert).where(
            Transfert.id == transfert_id, courant.condition_perimetre(Transfert.agency_id)
        )
    ).scalar_one_or_none()
    if transfert is None:
        raise TransfertIntrouvableError()
    return transfert


@dataclass(frozen=True)
class LigneTransfert:
    """Une ligne de `lister_transferts` — comptes et identités DÉJÀ résolus en clair, jamais un
    UUID nu à l'écran. `receptionne_par_nom`/`receptionne_le`/`montant_compte` restent `None`
    tant que `statut = 'en_transit'` (état légitime, pas une erreur)."""

    id: uuid.UUID
    agency_id: uuid.UUID
    agency_nom: str
    niveau_source: str
    niveau_destination: str
    compte_source_number: str
    compte_destination_number: str
    montant_envoye: int
    montant_compte: int | None
    statut: str
    envoye_par_nom: str
    envoye_le: datetime
    receptionne_par_nom: str | None
    receptionne_le: datetime | None
    motif: str


@dataclass(frozen=True)
class PageTransferts:
    lignes: Sequence[LigneTransfert]
    total: int
    page: int
    taille: int


def lister_transferts(
    db: Session,
    courant: UtilisateurCourant,
    *,
    statut: str | None = "en_transit",
    niveau: str | None = None,
    agency_id: uuid.UUID | None = None,
    page: int = 1,
    taille: int = TAILLE_PAGE_DEFAUT,
) -> PageTransferts:
    """Transferts dans le périmètre de l'acteur — PAR DÉFAUT ceux « en transit » (la file
    d'attente à réceptionner), `statut=None` pour voir aussi les réceptionnés. `niveau` filtre
    sur SOURCE OU DESTINATION (un caissier veut voir les mouvements qui touchent SON poste, peu
    importe le sens). `agency_id` s'AJOUTE au cloisonnement, ne le remplace jamais : hors
    périmètre, il ne fait que renvoyer zéro ligne, jamais une erreur.

    Triée par envoi le plus RÉCENT d'abord (même convention que les listes de sessions)."""
    taille = max(1, min(taille, TAILLE_PAGE_MAX))
    page = max(1, page)

    conditions = [courant.condition_perimetre(Transfert.agency_id)]
    if statut is not None:
        conditions.append(Transfert.statut == statut)
    if niveau is not None:
        conditions.append(
            or_(Transfert.niveau_source == niveau, Transfert.niveau_destination == niveau)
        )
    if agency_id is not None:
        conditions.append(Transfert.agency_id == agency_id)

    total = db.execute(select(func.count()).select_from(Transfert).where(*conditions)).scalar_one()

    compte_source = aliased(Account)
    compte_destination = aliased(Account)
    envoyeur = aliased(User)
    receveur = aliased(User)
    nom_envoyeur = func.concat_ws(" ", envoyeur.first_name, envoyeur.last_name)
    # CASE explicite plutôt que concat_ws seul : concat_ws(' ', NULL, NULL) rend '' (chaîne
    # vide), jamais NULL — un transfert « en transit » doit afficher None, pas un nom vide.
    nom_receveur = case(
        (Transfert.receptionne_par.is_(None), None),
        else_=func.concat_ws(" ", receveur.first_name, receveur.last_name),
    )

    lignes = db.execute(
        select(
            Transfert.id,
            Transfert.agency_id,
            Agency.name.label("agency_nom"),
            Transfert.niveau_source,
            Transfert.niveau_destination,
            compte_source.account_number.label("compte_source_number"),
            compte_destination.account_number.label("compte_destination_number"),
            Transfert.montant_envoye,
            Transfert.montant_compte,
            Transfert.statut,
            nom_envoyeur.label("envoye_par_nom"),
            Transfert.envoye_le,
            nom_receveur.label("receptionne_par_nom"),
            Transfert.receptionne_le,
            Transfert.motif,
        )
        .select_from(Transfert)
        .join(Agency, Agency.id == Transfert.agency_id)
        .join(compte_source, compte_source.id == Transfert.compte_source_id)
        .join(compte_destination, compte_destination.id == Transfert.compte_destination_id)
        .join(envoyeur, envoyeur.id == Transfert.envoye_par)
        .outerjoin(receveur, receveur.id == Transfert.receptionne_par)
        .where(*conditions)
        .order_by(Transfert.envoye_le.desc())
        .offset((page - 1) * taille)
        .limit(taille)
    ).all()

    return PageTransferts(
        lignes=[LigneTransfert(**ligne._mapping) for ligne in lignes],
        total=total,
        page=page,
        taille=taille,
    )
