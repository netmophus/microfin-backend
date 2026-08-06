"""Crédit CR5d — prélèvement automatique des échéances (migration 0039).

DEUX temps, DEUX fonctions :

  1. `configurer_prelevement` — choisit EXPLICITEMENT le compte epargne.accounts à débiter,
     sur un crédit DÉJÀ décaissé (pas seulement au décaissement — un tiers peut ouvrir son
     compte de prélèvement après coup). Mêmes contrôles que le décaissement crédité sur compte
     (voir epargne.operations.charger_compte_pour_credit_externe) : le compte doit appartenir
     au même tiers, être ACTIF, et n'être PAS un dépôt à terme (DAT) — un DAT n'a aucun
     mécanisme de blocage dans ce module, le prélever romprait le blocage prévu à terme, même
     raison que pour un crédit direct sur compte. SANS périmètre agence : appelé par un
     opérateur (CLI), pas un acteur JWT scopé — voir docstring de la fonction.

  2. `executer_prelevement` — traitement de masse, CHAQUE DOSSIER COMMITTÉ SÉPARÉMENT (même
     patron que epargne.interets.verser_interets et credit.reclassification), pour la commande
     CLI `prelever-echeances --date-echeance AAAA-MM-JJ`. Idempotent : `prelevement_tentatives`
     (UNIQUE installment_id/date_tentative) empêche un double prélèvement le même jour, mais
     laisse la porte ouverte les jours suivants pour une échéance encore due.

RÉUTILISE `credit.remboursement.rembourser()` avec un montant PARTIEL (capacité CR5b) au lieu
d'une logique de ventilation séparée — voir sa docstring, section CR5d. Le débit du compte
d'épargne du tiers (mouvement + solde) est enregistré À PART par
`epargne.operations.enregistrer_debit_externe`, miroir exact de ce que `decaissement.py` fait
déjà dans l'autre sens (crédit direct sur compte) : ce module est le SECOND point d'entrée
« epargne-aware » du module crédit, `decaissement.py` étant le premier.

SOLDE INSUFFISANT (décision CR5d) : prélève ce qui est disponible
(balance - min_balance + decouvert_autorise, JAMAIS en dessous), jamais plus — le reliquat
reste dû, retentable dès le lendemain (nouvelle date_tentative)."""

import uuid
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.audit.service import CONTEXTE_VIDE, ContexteRequete, ecrire_audit
from app.modules.credit.decaissement import RattachementManquantError
from app.modules.credit.demandes import RESSOURCE, CreditError
from app.modules.credit.models import Application, Installment, PrelevementTentative
from app.modules.credit.remboursement import prochaine_echeance, rembourser
from app.modules.epargne.models import Product as SavingsProduct
from app.modules.epargne.models import SavingsAccount
from app.modules.epargne.operations import enregistrer_debit_externe, resoudre_compte_collectif

CODE_JOURNAL_PRELEVEMENT = "OD"  # virement comptable interne — aucun argent physique ne bouge


class DemandeNonDecaisseeError(CreditError):
    """Seul un crédit DÉCAISSÉ peut avoir un compte de prélèvement configuré."""


class CompteInvalideError(CreditError):
    """Le compte choisi n'existe pas, n'appartient pas au même tiers, n'est pas actif, ou est
    un dépôt à terme (DAT — même raison que pour un crédit direct sur compte)."""


class DejaTenteError(CreditError):
    """Cette échéance a déjà une tentative de prélèvement enregistrée pour cette date
    (idempotence — voir prelevement_tentatives)."""


def configurer_prelevement(
    db: Session,
    demande: Application,
    compte_epargne_id: uuid.UUID,
    *,
    par: uuid.UUID | None,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> Application:
    """Choisit le compte à débiter pour le prélèvement automatique de CE crédit — ancré tel
    quel, jamais recalculé (voir Application.compte_prelevement_id). Peut être rappelé pour
    changer de compte (ancienne valeur écrasée, tracée en audit avant/après).

    SANS périmètre agence : appelé par un opérateur (CLI aujourd'hui, pas d'écran/endpoint pour
    l'instant — décision CR5d), pas par un acteur JWT scopé à une agence. Mêmes contrôles
    métier que epargne.operations.charger_compte_pour_credit_externe (tiers propriétaire, actif,
    pas DAT), réécrits ici sans sa dépendance à UtilisateurCourant."""
    if demande.status != "decaisse":
        raise DemandeNonDecaisseeError(
            f"Cette demande ({demande.application_number}) n'est pas décaissée : "
            "aucun compte de prélèvement ne peut y être rattaché."
        )

    compte = db.execute(
        select(SavingsAccount).where(
            SavingsAccount.id == compte_epargne_id, SavingsAccount.tier_id == demande.tier_id
        )
    ).scalar_one_or_none()
    if compte is None:
        raise CompteInvalideError(
            "Ce compte n'existe pas ou n'appartient pas au titulaire de ce crédit."
        )
    if compte.status != "actif":
        raise CompteInvalideError(
            "Ce compte est fermé : il ne peut pas servir de compte de prélèvement."
        )
    # DAT exclu — MÊME RAISON qu'au décaissement crédité sur compte (voir
    # epargne.operations.charger_compte_pour_credit_externe) : un DAT n'a aucun mécanisme de
    # blocage jusqu'à échéance dans ce module, le prélever romprait le blocage prévu à terme.
    type_produit = db.execute(
        select(SavingsProduct.type).where(SavingsProduct.id == compte.product_id)
    ).scalar_one()
    if type_produit == "terme":
        raise CompteInvalideError(
            "Ce compte est un dépôt à terme : il ne peut pas servir de compte de prélèvement "
            "(le client ne pourrait pas en disposer librement)."
        )

    avant = {
        "compte_prelevement_id": (
            str(demande.compte_prelevement_id) if demande.compte_prelevement_id else None
        )
    }
    demande.compte_prelevement_id = compte.id
    demande.updated_by = par
    db.flush()

    ecrire_audit(
        db,
        action="credit.demande.prelevement_configure",
        contexte=contexte,
        acteur_id=par,
        resource_type=RESSOURCE,
        resource_id=demande.id,
        agency_id=demande.agency_id,
        old_values=avant,
        new_values={
            "compte_prelevement_id": str(compte.id),
            "account_number": compte.account_number,
        },
    )
    return demande


@dataclass
class RapportPrelevement:
    """Résultat d'une exécution du job — même esprit que RapportInterets (épargne E5) et
    RapportReclassement (CR5c)."""

    dossiers_evalues: int = 0
    prelevements: int = 0  # montant réellement encaissé > 0
    sans_disponible: int = 0  # tentative enregistrée, rien à prélever ce jour-là
    ignores_deja_tente: int = 0  # idempotence : cette échéance a déjà une tentative ce jour
    ignores_rattachement_manquant: list[str] = field(default_factory=list)
    total_preleve: int = 0


def _prelever_un(
    db: Session,
    demande: Application,
    *,
    date_echeance: date,
    par: uuid.UUID | None,
    contexte: ContexteRequete,
) -> int:
    """Prélève UN dossier, dans SA propre transaction (l'appelant committe/rollback). Rend le
    montant réellement prélevé (0 si rien n'était disponible — la tentative est quand même
    enregistrée). Lève DejaTenteError (idempotence) ou RattachementManquantError (paramétrage
    incomplet)."""
    echeance = prochaine_echeance(db, demande.id)
    if echeance is None or echeance.due_date > date_echeance:
        return 0  # rien à prélever à cette date pour ce dossier (garde défensive, déjà filtré)

    deja = db.execute(
        select(PrelevementTentative.id).where(
            PrelevementTentative.installment_id == echeance.id,
            PrelevementTentative.date_tentative == date_echeance,
        )
    ).first()
    if deja is not None:
        raise DejaTenteError(f"{demande.application_number} #{echeance.numero}")

    assert demande.compte_prelevement_id is not None  # filtré par l'appelant (executer_prelevement)
    compte_epargne = db.execute(
        select(SavingsAccount)
        .where(SavingsAccount.id == demande.compte_prelevement_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()

    montant_a_prelever = 0
    if compte_epargne is not None and compte_epargne.status == "actif":
        produit = db.get(SavingsProduct, compte_epargne.product_id)
        assert produit is not None
        # PLANCHER — même formule que le guichet épargne (guichet.retirer) : jamais en dessous.
        disponible = compte_epargne.balance - produit.min_balance + produit.decouvert_autorise
        solde_du = echeance.total - echeance.montant_paye
        montant_a_prelever = max(min(disponible, solde_du), 0)

    if montant_a_prelever > 0:
        assert compte_epargne is not None
        collectif = resoudre_compte_collectif(db, compte_epargne)
        if collectif is None:
            raise RattachementManquantError(
                "le produit du compte de prélèvement n'a pas de compte d'épargne rattaché "
                "(plan comptable)"
            )
        resultat = rembourser(
            db,
            demande,
            montant=montant_a_prelever,
            par=par,
            entry_date=date_echeance,
            contexte=contexte,
            compte_source_id=collectif,
            journal_code=CODE_JOURNAL_PRELEVEMENT,
        )
        enregistrer_debit_externe(
            db,
            compte_epargne,
            montant_a_prelever,
            operation_type="prelevement_credit",
            label=f"Prélèvement automatique crédit {demande.application_number} #{echeance.numero}",
            journal_entry_id=resultat.entry_id,
            par=par,
        )

    db.add(
        PrelevementTentative(
            installment_id=echeance.id,
            date_tentative=date_echeance,
            montant_preleve=montant_a_prelever,
            created_by=par,
        )
    )
    db.flush()
    return montant_a_prelever


def executer_prelevement(
    db: Session,
    *,
    date_echeance: date,
    par: uuid.UUID | None = None,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> RapportPrelevement:
    """Traitement de masse : prélève toutes les échéances dues au plus tard `date_echeance`,
    sur les crédits décaissés ayant un compte_prelevement_id configuré.

    CHAQUE DOSSIER EST COMMITTÉ SÉPARÉMENT (même patron que epargne.interets.verser_interets et
    credit.reclassification.executer_reclassification) : un problème sur un dossier (paramétrage
    incomplet, échéance déjà tentée ce jour) fait échouer CE dossier seul, le job continue sur
    les suivants."""
    ids = list(
        db.execute(
            select(Application.id)
            .join(Installment, Installment.application_id == Application.id)
            .where(
                Application.status == "decaisse",
                Application.compte_prelevement_id.isnot(None),
                Installment.status != "paye",
                Installment.due_date <= date_echeance,
            )
            .distinct()
        ).scalars()
    )
    rapport = RapportPrelevement()
    for application_id in ids:
        demande = db.get(Application, application_id)
        assert demande is not None
        rapport.dossiers_evalues += 1
        try:
            montant = _prelever_un(
                db, demande, date_echeance=date_echeance, par=par, contexte=contexte
            )
        except DejaTenteError:
            db.rollback()
            rapport.ignores_deja_tente += 1
            continue
        except RattachementManquantError:
            db.rollback()
            rapport.ignores_rattachement_manquant.append(demande.application_number)
            continue
        db.commit()
        if montant > 0:
            rapport.prelevements += 1
            rapport.total_preleve += montant
        else:
            rapport.sans_disponible += 1
    return rapport
