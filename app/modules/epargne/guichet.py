"""Opérations de guichet : dépôt et retrait — le cœur transactionnel du module.

LA TRANSACTION UNIQUE (non négociable). Un dépôt/retrait fait, dans UNE seule transaction, UN
seul commit à la fin :
  1. verrou de ligne sur le compte (SELECT ... FOR UPDATE) — sérialise la concurrence ;
  2. relit le solde SOUS le verrou ;
  3. contrôles (compte actif, membre actif, montant > 0, plancher pour un retrait) ;
  4. pose la PIÈCE comptable équilibrée (poser_ecriture_operation) — le GÉNÉRAL ;
  5. écrit le MOUVEMENT (append-only), relié à la pièce par journal_entry_id — l'AUXILIAIRE ;
  6. met à jour le solde-cache = balance_after ;
  7. audit de l'acte métier ;
  8. commit.
Si l'une des étapes 4-7 lève, RIEN n'est committé : pas de solde bougé sans pièce, pas de pièce
sans mouvement. Le mouvement et la pièce sont posés ENSEMBLE.

VERROU (2) : le FOR UPDATE sérialise deux opérations concurrentes sur le même compte, comme la
numérotation des tiers. Le second attend le premier et lit le solde RÉEL.

PLANCHER (3) : un retrait est refusé si `montant > solde - min_balance + decouvert_autorise`.
Épargne à vue standard : min_balance = decouvert_autorise = 0, donc le solde ne descend pas sous
zéro. Les deux sont des PARAMÈTRES PRODUIT provisoires (l'expert dira les valeurs). `min_balance`
EST le « solde plancher » du produit (écran Produits d'épargne). La clôture d'un compte
(`fermer_compte`) n'est pas un retrait : elle ramène le solde à 0 sans être soumise au plancher.

PREMIER DÉPÔT (3) : `produit.depot_initial_minimum` (0 = aucune contrainte) s'applique au PREMIER
dépôt d'un compte jamais mouvementé — l'ouverture reste à solde 0. Refus avant toute écriture.

CLOISONNEMENT (4) : le compte est chargé DANS le périmètre de l'acteur (le caissier n'opère que
sur son agence) ; hors périmètre -> 404 (n'existe pas de son point de vue), jamais 403.

SESSION DE CAISSE (Bloc C4) : dépôt et retrait exigent une session OUVERTE pour l'acteur — la
CAISSE créditée/débitée est celle de SA session (compte ANCRÉ à l'ouverture), jamais celle de
l'agence. Sans session ouverte, refus (AucuneSessionOuverteError -> 422) : le tiroir doit être
ouvert avant tout mouvement. `fermer_compte` (restitution du solde à la clôture) est un acte de
GESTION, pas de guichet — pas concerné, ne passe pas par `_operer`.
"""

import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.modules.audit.service import CONTEXTE_VIDE, ContexteRequete, ecrire_audit
from app.modules.caisse.service import resoudre_session_active
from app.modules.comptabilite.journee import date_comptable_obligatoire
from app.modules.epargne import service
from app.modules.epargne.models import InteretCalcul, Product, SavingsAccount, SavingsMovement
from app.modules.epargne.operations import (
    TYPE_DENOUEMENT,
    TYPE_DEPOT,
    TYPE_INTERET,
    TYPE_RETRAIT,
    TYPE_RETRAIT_ANTICIPE,
    poser_ecriture_operation,
)
from app.modules.epargne.terme import (
    MODE_ANTICIPE,
    MODE_ECHEANCE,
    ConditionsTerme,
    DecompositionDenouement,
    conditions_du_contrat,
    decomposer_denouement,
)
from app.modules.security.autorisation import UtilisateurCourant


def _fcfa(montant: int) -> str:
    """Formate un montant en francs, lisible par un caissier (et son client) : « 7 000 F »."""
    return f"{montant:,} F".replace(",", " ")


class OperationError(Exception):
    """Base des refus d'opération de guichet."""


class CompteIntrouvableError(OperationError):
    """Compte inexistant ou hors périmètre de l'acteur. -> 404."""


class CompteClotureError(OperationError):
    """Le compte est fermé : plus aucune opération."""


class MontantInvalideError(OperationError):
    """Montant nul ou négatif."""


class OperationGeleeError(OperationError):
    """Le membre n'est pas actif (suspendu) : opérations gelées (mesure conservatoire)."""


class SoldeInsuffisantError(OperationError):
    """Le retrait dépasse le disponible (solde - plancher + découvert autorisé)."""


class PremierDepotInsuffisantError(OperationError):
    """Le premier dépôt d'un compte jamais mouvementé est sous le minimum du produit."""


class CompteATermeError(OperationError):
    """Dépôt ou retrait ordinaire sur un dépôt à terme : il est bloqué jusqu'à l'échéance."""


class EcheanceNonAtteinteError(OperationError):
    """Dénouement demandé avant l'échéance : seul le retrait anticipé est possible."""


class EcheanceAtteinteError(OperationError):
    """Retrait anticipé demandé à partir de l'échéance : le dénouement est sans pénalité."""


class ActionsAudit:
    DEPOT = "epargne.depot"
    RETRAIT = "epargne.retrait"
    DAT_DENOUEMENT = "epargne.dat.denouement"
    DAT_RETRAIT_ANTICIPE = "epargne.dat.retrait_anticipe"


@dataclass(frozen=True)
class ResultatOperation:
    """Ce que l'opération rend (reçu de guichet)."""

    account_number: str
    nouveau_solde: int
    entry_number: str | None


def _charger_compte_verrou(
    db: Session, courant: UtilisateurCourant, compte_id: uuid.UUID
) -> SavingsAccount:
    """Charge le compte SOUS VERROU (FOR UPDATE), dans le périmètre de l'acteur, ou lève 404.

    populate_existing : sous le verrou, on relit les valeurs COURANTES (solde, statut) et pas une
    copie périmée en session. C'est la garantie de concurrence — décider sur le solde RÉEL.
    """
    compte = db.execute(
        select(SavingsAccount)
        .where(
            SavingsAccount.id == compte_id,
            courant.condition_perimetre(SavingsAccount.agency_id),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if compte is None:
        raise CompteIntrouvableError()
    return compte


def _controler(db: Session, compte: SavingsAccount, montant: int) -> None:
    """Contrôles communs au dépôt et au retrait (hors plancher, propre au retrait).

    Messages en langage HUMAIN (un client peut lire par-dessus l'épaule) : ni symbole, ni nom de
    variable, ni code de statut brut.
    """
    if compte.status != "actif":
        raise CompteClotureError("Ce compte est fermé : aucune opération possible.")
    if montant <= 0:
        raise MontantInvalideError("Montant invalide : saisissez un montant supérieur à zéro.")
    statut_membre = db.execute(
        text("SELECT status FROM tiers.tiers WHERE id = :t"), {"t": compte.tier_id}
    ).scalar_one_or_none()
    if statut_membre != "actif":
        raise OperationGeleeError("Opérations gelées : le membre est suspendu.")


def _controler_premier_depot(
    db: Session, compte: SavingsAccount, produit: Product, montant: int
) -> None:
    """Refuse un premier dépôt sous `depot_initial_minimum`. « Premier » = aucun mouvement
    enregistré sur ce compte (lu sous le verrou de ligne du compte : deux premiers dépôts
    concurrents ne peuvent pas passer tous les deux)."""
    if produit.depot_initial_minimum <= 0 or montant >= produit.depot_initial_minimum:
        return
    deja_mouvemente = db.execute(
        select(SavingsMovement.id).where(SavingsMovement.account_id == compte.id).limit(1)
    ).first()
    if deja_mouvemente is None:
        raise PremierDepotInsuffisantError(
            "Premier dépôt insuffisant : ce produit exige au moins "
            f"{_fcfa(produit.depot_initial_minimum)} à l'ouverture."
        )


def _enregistrer_mouvement(
    db: Session,
    compte: SavingsAccount,
    *,
    sens: str,
    montant: int,
    balance_after: int,
    operation_type: str,
    journal_entry_id: uuid.UUID,
    date_valeur: date,
    par: uuid.UUID | None,
    horodatage_reel: bool = False,
) -> None:
    """Écrit le mouvement append-only, relié à sa pièce comptable (dont il prend la date).

    `horodatage_reel` : deux mouvements d'une même transaction (intérêts puis sortie d'un dépôt à
    terme) auraient le même `created_at` (NOW() est l'heure de la transaction), donc un ordre
    d'affichage au hasard de l'UUID. `clock_timestamp()` donne l'instant réel de l'insertion."""
    horodatage = {"created_at": func.clock_timestamp()} if horodatage_reel else {}
    db.add(
        SavingsMovement(
            account_id=compte.id,
            sens=sens,
            amount=montant,
            balance_after=balance_after,
            operation_type=operation_type,
            journal_entry_id=journal_entry_id,
            date_valeur=date_valeur,
            created_by=par,
            **horodatage,
        )
    )
    db.flush()


def _operer_sans_commit(
    db: Session,
    courant: UtilisateurCourant,
    compte_id: uuid.UUID,
    montant: int,
    *,
    code_operation: str,
    sens: str,
    signe: int,  # +1 dépôt, -1 retrait
    action_audit: str,
    contexte: ContexteRequete,
    placement_initial: bool = False,
) -> ResultatOperation:
    """Tronc commun dépôt/retrait : verrou, contrôles, pièce + mouvement + solde, audit, commit.

    Bloc C4 : dépôt ET retrait sont tous deux des mouvements de caisse (D 5721/C 3111 ou
    l'inverse) — exige une session de caisse OUVERTE pour l'acteur, et c'est le compte ANCRÉ de
    CETTE session qui reçoit l'écriture, jamais celui de l'agence."""
    compte_caisse_id = resoudre_session_active(db, courant.user_id).compte_caisse_id
    compte = _charger_compte_verrou(db, courant, compte_id)
    _controler(db, compte, montant)
    if compte.maturity_date is not None and not placement_initial:
        raise CompteATermeError(
            "Ce compte est un dépôt à terme, bloqué jusqu'au "
            f"{compte.maturity_date.strftime('%d/%m/%Y')} : ni dépôt ni retrait ordinaire. "
            "Utilisez le dénouement à l'échéance ou le retrait anticipé."
        )

    produit = db.get(Product, compte.product_id)
    assert produit is not None  # FK garantit l'existence

    if signe > 0:  # dépôt : le minimum ne vaut que pour le PREMIER, sur un compte jamais mouvementé
        _controler_premier_depot(db, compte, produit, montant)

    if signe < 0:  # retrait : contrôle du plancher
        disponible = compte.balance - produit.min_balance + produit.decouvert_autorise
        if montant > disponible:
            if produit.min_balance > 0:
                raise SoldeInsuffisantError(
                    "Retrait refusé : le solde ne peut pas descendre sous "
                    f"{_fcfa(produit.min_balance)} sur ce produit."
                )
            raise SoldeInsuffisantError(
                f"Retrait impossible : le solde disponible est de {_fcfa(disponible)}, "
                f"le retrait demandé de {_fcfa(montant)}."
            )

    # GÉNÉRAL : la pièce comptable équilibrée (peut lever si rattachement manquant -> refus propre).
    piece = poser_ecriture_operation(
        db, compte, code_operation, montant, courant.user_id,
        compte_caisse_id=compte_caisse_id, contexte=contexte,
    )

    nouveau_solde = compte.balance + signe * montant
    # AUXILIAIRE : le mouvement, relié à la pièce, dans la MÊME transaction.
    _enregistrer_mouvement(
        db,
        compte,
        sens=sens,
        montant=montant,
        balance_after=nouveau_solde,
        operation_type=code_operation.split(".")[-1],
        journal_entry_id=piece.id,
        date_valeur=piece.entry_date,
        par=courant.user_id,
    )
    compte.balance = nouveau_solde
    compte.updated_by = courant.user_id
    db.flush()

    ecrire_audit(
        db,
        action=action_audit,
        contexte=contexte,
        acteur_id=courant.user_id,
        resource_type="epargne.account",
        resource_id=compte.id,
        agency_id=compte.agency_id,
        new_values={
            "account_number": compte.account_number,
            "montant": montant,
            "nouveau_solde": nouveau_solde,
            "piece": piece.entry_number,
        },
    )
    return ResultatOperation(compte.account_number, nouveau_solde, piece.entry_number)


def _operer(
    db: Session,
    courant: UtilisateurCourant,
    compte_id: uuid.UUID,
    montant: int,
    *,
    code_operation: str,
    sens: str,
    signe: int,
    action_audit: str,
    contexte: ContexteRequete,
) -> ResultatOperation:
    """`_operer_sans_commit` + l'UNIQUE commit de la transaction de guichet."""
    resultat = _operer_sans_commit(
        db, courant, compte_id, montant,
        code_operation=code_operation, sens=sens, signe=signe,
        action_audit=action_audit, contexte=contexte,
    )
    db.commit()
    return resultat


@dataclass(frozen=True)
class ResultatOuvertureDepotATerme:
    """Reçu d'ouverture : le compte, le reçu du versement et les conditions figées."""

    compte: SavingsAccount
    versement: ResultatOperation
    conditions: ConditionsTerme


def ouvrir_depot_a_terme(
    db: Session,
    courant: UtilisateurCourant,
    *,
    tier_id: uuid.UUID,
    agency_id: uuid.UUID,
    product_id: uuid.UUID,
    term_months: int,
    montant: int,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> ResultatOuvertureDepotATerme:
    """Ouvre un dépôt à terme ET place le capital dans UNE transaction : jamais de compte à terme
    vide. Durée choisie ici (dans les bornes du produit), échéance dérivée de la journée
    comptable, conditions de calcul figées au contrat. Le capital entre en caisse comme un dépôt
    (session de caisse ouverte exigée). Un seul commit, à la fin ; tout refus annule l'ensemble."""
    resoudre_session_active(db, courant.user_id)
    _, conditions = conditions_du_contrat(db, product_id, term_months, montant)
    compte = service.ouvrir_compte(
        db,
        tier_id=tier_id,
        product_id=product_id,
        agency_id=agency_id,
        par=courant.user_id,
        conditions_terme=conditions,
        contexte=contexte,
    )
    versement = _operer_sans_commit(
        db, courant, compte.id, montant,
        code_operation=TYPE_DEPOT, sens="credit", signe=+1,
        action_audit=ActionsAudit.DEPOT, contexte=contexte, placement_initial=True,
    )
    db.commit()
    return ResultatOuvertureDepotATerme(compte, versement, conditions)


# --- Dépôt à terme : dénouement à l'échéance et retrait anticipé -----------------------

# Clé d'unicité (compte, période) : un dépôt à terme ne se solde qu'une fois.
PERIODE_DENOUEMENT = "DENOUEMENT"
METHODE_TERME = "terme"


@dataclass(frozen=True)
class ResultatDenouement:
    """Reçu de dénouement ou de retrait anticipé."""

    account_number: str
    mode: str
    capital: int
    interets_courus: int
    penalite: int
    interets_nets: int
    total_paye: int
    entry_number: str | None
    message: str


def _decomposer(compte: SavingsAccount, journee: date) -> DecompositionDenouement:
    """Décompose le solde d'un DAT avec les paramètres FIGÉS au contrat du compte."""
    assert compte.principal is not None  # un compte à terme porte ses conditions (trigger 0065)
    assert compte.term_start_date is not None and compte.maturity_date is not None
    assert compte.taux_bp_contrat is not None and compte.penalite_bp_contrat is not None
    assert compte.base_jours_contrat is not None and compte.regle_arrondi_contrat is not None
    return decomposer_denouement(
        principal=compte.principal,
        debut=compte.term_start_date,
        echeance=compte.maturity_date,
        taux_bp=compte.taux_bp_contrat,
        penalite_bp=compte.penalite_bp_contrat,
        base_jours=compte.base_jours_contrat,
        arrondi=compte.regle_arrondi_contrat,
        journee=journee,
    )


def apercu_denouement(
    db: Session, courant: UtilisateurCourant, compte_id: uuid.UUID
) -> DecompositionDenouement:
    """Ce que recevrait le client si son dépôt à terme était soldé à la journée comptable en cours
    (échéance ou anticipé, selon la journée). Lecture seule, sans verrou ; même calcul que
    l'exécution. Un compte hors périmètre -> `CompteIntrouvableError` (404)."""
    compte = db.execute(
        select(SavingsAccount).where(
            SavingsAccount.id == compte_id,
            courant.condition_perimetre(SavingsAccount.agency_id),
        )
    ).scalar_one_or_none()
    if compte is None:
        raise CompteIntrouvableError()
    if compte.status != "actif":
        raise CompteClotureError("Ce compte est fermé : aucune opération possible.")
    if compte.maturity_date is None:
        raise CompteATermeError("Ce compte n'est pas un dépôt à terme.")
    return _decomposer(compte, date_comptable_obligatoire(db))


def _solder_depot_a_terme(
    db: Session,
    courant: UtilisateurCourant,
    compte_id: uuid.UUID,
    *,
    mode_demande: str,
    contexte: ContexteRequete,
) -> ResultatDenouement:
    """Solde un dépôt à terme en UNE transaction : intérêts (D 60252 / C 252xxx) si > 0, puis
    sortie de caisse du total (D 252xxx / C caisse), mouvements, archivage rejouable, clôture
    définitive du compte, audit. Même discipline que `_operer_sans_commit` : verrou, contrôles,
    pièce + mouvement ensemble, un seul commit. Le mode demandé doit correspondre à la journée
    comptable (à l'échéance ou avant) : on ne dénoue pas en avance, on ne paie pas de pénalité
    à l'échéance."""
    compte_caisse_id = resoudre_session_active(db, courant.user_id).compte_caisse_id
    compte = _charger_compte_verrou(db, courant, compte_id)
    _controler(db, compte, compte.balance)
    if compte.maturity_date is None:
        raise CompteATermeError("Ce compte n'est pas un dépôt à terme.")

    journee = date_comptable_obligatoire(db)
    d = _decomposer(compte, journee)
    echeance = compte.maturity_date.strftime("%d/%m/%Y")
    if mode_demande == MODE_ECHEANCE and d.mode == MODE_ANTICIPE:
        raise EcheanceNonAtteinteError(
            f"Ce dépôt à terme n'arrive à échéance que le {echeance}. Avant cette date, seul le "
            "retrait anticipé (avec pénalité) est possible."
        )
    if mode_demande == MODE_ANTICIPE and d.mode == MODE_ECHEANCE:
        raise EcheanceAtteinteError(
            f"L'échéance de ce dépôt à terme ({echeance}) est atteinte : utilisez le dénouement "
            "à l'échéance, sans pénalité."
        )

    piece_interets = None
    if d.interets_nets > 0:
        piece_interets = poser_ecriture_operation(
            db, compte, TYPE_INTERET, d.interets_nets, courant.user_id, contexte=contexte
        )
        _enregistrer_mouvement(
            db, compte, sens="credit", montant=d.interets_nets,
            balance_after=compte.balance + d.interets_nets, operation_type="interet",
            journal_entry_id=piece_interets.id, date_valeur=piece_interets.entry_date,
            par=courant.user_id, horodatage_reel=True,
        )
        compte.balance += d.interets_nets

    code_operation = TYPE_DENOUEMENT if d.mode == MODE_ECHEANCE else TYPE_RETRAIT_ANTICIPE
    piece = poser_ecriture_operation(
        db, compte, code_operation, d.total_paye, courant.user_id,
        compte_caisse_id=compte_caisse_id, contexte=contexte,
    )
    _enregistrer_mouvement(
        db, compte, sens="debit", montant=d.total_paye, balance_after=0,
        operation_type=code_operation.split(".")[-1], journal_entry_id=piece.id,
        date_valeur=piece.entry_date, par=courant.user_id, horodatage_reel=True,
    )
    compte.balance = 0

    db.add(
        InteretCalcul(
            account_id=compte.id,
            periode=PERIODE_DENOUEMENT,
            base_solde=d.capital,
            methode=METHODE_TERME,
            taux_bp=compte.taux_bp_contrat,
            base_jours=compte.base_jours_contrat,
            jours=d.jours,
            montant=d.interets_nets,
            detail={
                "mode": d.mode,
                "debut": compte.term_start_date.isoformat() if compte.term_start_date else None,
                "echeance": compte.maturity_date.isoformat(),
                "journee": journee.isoformat(),
                "arrondi": compte.regle_arrondi_contrat,
                "interets_courus": d.interets_courus,
                "penalite_bp": compte.penalite_bp_contrat,
                "penalite": d.penalite,
                "interets_nets": d.interets_nets,
            },
            journal_entry_id=piece_interets.id if piece_interets else None,
            computed_by=courant.user_id,
        )
    )
    compte.status = "cloture"
    compte.closed_by = courant.user_id
    compte.closure_reason = (
        "Dénouement à l'échéance" if d.mode == MODE_ECHEANCE else "Retrait anticipé"
    )
    compte.updated_by = courant.user_id
    compte.closed_at = db.execute(text("SELECT NOW()")).scalar_one()
    db.flush()

    ecrire_audit(
        db,
        action=(
            ActionsAudit.DAT_DENOUEMENT
            if d.mode == MODE_ECHEANCE
            else ActionsAudit.DAT_RETRAIT_ANTICIPE
        ),
        contexte=contexte,
        acteur_id=courant.user_id,
        resource_type="epargne.account",
        resource_id=compte.id,
        agency_id=compte.agency_id,
        old_values={"status": "actif"},
        new_values={
            "status": "cloture",
            "account_number": compte.account_number,
            "capital": d.capital,
            "interets_courus": d.interets_courus,
            "penalite": d.penalite,
            "total_paye": d.total_paye,
            "piece": piece.entry_number,
        },
    )
    db.commit()
    return ResultatDenouement(
        account_number=compte.account_number,
        mode=d.mode,
        capital=d.capital,
        interets_courus=d.interets_courus,
        penalite=d.penalite,
        interets_nets=d.interets_nets,
        total_paye=d.total_paye,
        entry_number=piece.entry_number,
        message=d.message,
    )


def denouer_depot_a_terme(
    db: Session,
    courant: UtilisateurCourant,
    compte_id: uuid.UUID,
    *,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> ResultatDenouement:
    """Dénouement à l'échéance : capital + intérêts de toute la durée, en espèces. Acte de caisse
    normal. Refusé avant l'échéance (la journée comptable fait foi, jamais l'horloge)."""
    return _solder_depot_a_terme(
        db, courant, compte_id, mode_demande=MODE_ECHEANCE, contexte=contexte
    )


def retirer_par_anticipation(
    db: Session,
    courant: UtilisateurCourant,
    compte_id: uuid.UUID,
    *,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> ResultatDenouement:
    """Retrait anticipé : capital + intérêts courus au prorata, diminués de la pénalité figée au
    contrat. Le capital n'est jamais entamé. Refusé à partir de l'échéance."""
    return _solder_depot_a_terme(
        db, courant, compte_id, mode_demande=MODE_ANTICIPE, contexte=contexte
    )


def deposer(
    db: Session,
    courant: UtilisateurCourant,
    compte_id: uuid.UUID,
    montant: int,
    *,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> ResultatOperation:
    """Dépôt : la caisse entre, la dette envers le membre monte. D 5721 / C 3111."""
    return _operer(
        db, courant, compte_id, montant,
        code_operation=TYPE_DEPOT, sens="credit", signe=+1,
        action_audit=ActionsAudit.DEPOT, contexte=contexte,
    )


def retirer(
    db: Session,
    courant: UtilisateurCourant,
    compte_id: uuid.UUID,
    montant: int,
    *,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> ResultatOperation:
    """Retrait : la dette baisse, la caisse sort. D 3111 / C 5721. Refusé sous le plancher."""
    return _operer(
        db, courant, compte_id, montant,
        code_operation=TYPE_RETRAIT, sens="debit", signe=-1,
        action_audit=ActionsAudit.RETRAIT, contexte=contexte,
    )


def fermer_compte(
    db: Session,
    courant: UtilisateurCourant,
    compte_id: uuid.UUID,
    *,
    motif: str | None = None,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> SavingsAccount:
    """Ferme un compte — acte de GESTION (responsable), pas de guichet. Réutilise le verrou et le
    périmètre. La restitution + l'écriture de clôture + le passage en 'cloture' se font dans UNE
    transaction (cf. service.cloturer_compte). Un compte hors périmètre -> 404."""
    compte = _charger_compte_verrou(db, courant, compte_id)
    service.cloturer_compte(db, compte, courant.user_id, motif=motif, contexte=contexte)
    db.commit()
    return compte
