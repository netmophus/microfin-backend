"""Crédit CR4/CR5b — remboursements : encaisser une échéance, D CAISSE / C CREDIT / C
PRODUITS_INTERETS. Paiement PARTIEL (CR5b, migration 0037) : un ou plusieurs versements
peuvent régler une même échéance, jusqu'à solde.

PAS DE GATE KYC ICI, à la différence du décaissement : encaisser de l'argent qui RENTRE ne
présente aucun risque (même logique que le refus toujours possible en CR1). Un tiers suspendu
peut rembourser.

PIÈCE CONSTRUITE DIRECTEMENT (ecritures.creer_brouillon/valider), PAS via le moteur générique
poser_depuis_schema : ce dernier applique un même montant à toutes les lignes d'un modèle à
nombre de lignes fixe — un remboursement a des montants DIFFÉRENTS par ligne (capital ≠
intérêts) et un nombre de lignes VARIABLE (la ligne CREDIT ou la ligne PRODUITS_INTERETS peut
être OMISE si ce versement ne couvre que l'autre part, le moteur d'écriture refuse tout montant
<= 0). Décision : ne pas complexifier un moteur partagé par 3 modules pour un cas
structurellement différent (voir migration 0034).

VENTILATION INTÉRÊTS D'ABORD (défaut PROVISOIRE, convention bancaire courante — à valider,
voir docs/conformite-credit.md). Déduite de `montant_paye` (avant CE versement), AUCUNE colonne
dédiée : les intérêts déjà couverts sont `min(montant_paye_avant, echeance.interets)`, ce
versement couvre le reliquat d'intérêts en priorité, puis le capital.

PLAFOND = solde_du (`echeance.total - echeance.montant_paye`), PAS `echeance.total` : un
versement qui dépasse ce qui reste dû est refusé, même s'il reste inférieur au total d'origine.
GUICHET (CR6d) : envoie TOUJOURS solde_du en entier (paiement volontaire partiel EXCLU au
comptoir — décision produit, pas une limite technique). Le partiel sert le prélèvement
automatique (CR5d, à venir) qui encaisse ce qu'il trouve quand le solde du compte est
insuffisant.

TRANSACTION UNIQUE : la pièce, le passage de statut et le registre Repayment vivent dans la
même session, sans commit intermédiaire (ecritures.creer_brouillon/valider ne font que flush).
Si une étape lève après que la pièce a été posée, l'appelant rollback : rien ne persiste.
"""

import uuid
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.modules.audit.service import CONTEXTE_VIDE, ContexteRequete, ecrire_audit
from app.modules.comptabilite import ecritures
from app.modules.comptabilite.ecritures import LigneSaisie
from app.modules.comptabilite.models import Journal, JournalEntry
from app.modules.credit.decaissement import RattachementManquantError
from app.modules.credit.demandes import RESSOURCE, CreditError
from app.modules.credit.models import Application, Installment, Product, Repayment
from app.modules.parameters.models import Agency

CODE_JOURNAL = "CA"  # journal de caisse — même journal que le décaissement


class AucuneEcheanceAReglerError(CreditError):
    """Ce crédit n'est pas décaissé, ou toutes ses échéances sont déjà payées."""


class MontantIncorrectError(CreditError):
    """Le montant versé dépasse le solde restant dû de la prochaine échéance."""


@dataclass(frozen=True)
class ResultatRemboursement:
    """CE versement — pas l'échéance entière : `montant`/`montant_capital`/`montant_interets`
    décrivent uniquement ce que CE paiement a couvert (un versement partiel n'est qu'une partie
    du total dû). `paid_at` est l'instant de CE versement, distinct de `echeance.paid_at` qui
    ne se pose que lorsque l'échéance est intégralement soldée."""

    echeance: Installment
    montant: int
    montant_capital: int
    montant_interets: int
    paid_at: datetime
    solde_du: int
    echeance_soldee: bool


def prochaine_echeance(db: Session, application_id: uuid.UUID) -> Installment | None:
    """La prochaine échéance NON SOLDÉE d'un crédit (à échoir OU partiellement payée), ou None
    si tout est réglé. `status != 'paye'`, PAS `status == 'a_echoir'` (CR5b) — une échéance
    partiellement payée n'est ni l'un ni l'autre au sens strict, elle doit rester trouvée.
    Publique : réutilisée par consultation.rechercher_remboursables (guichet CR6d) pour
    afficher le SOLDE exact à régler avant tout appel serveur — jamais un montant deviné."""
    return db.execute(
        select(Installment)
        .where(Installment.application_id == application_id, Installment.status != "paye")
        .order_by(Installment.numero)
        .limit(1)
    ).scalar_one_or_none()


def rembourser(
    db: Session,
    demande: Application,
    *,
    montant: int,
    par: uuid.UUID | None,
    entry_date: date | None = None,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> ResultatRemboursement:
    """Encaisse un versement sur la PROCHAINE échéance non soldée d'un crédit décaissé — jusqu'à
    concurrence de son solde restant dû (PAS son total d'origine : une échéance déjà
    partiellement payée peut recevoir un versement complémentaire plus petit que son total).

    Refuse si le crédit n'est pas décaissé ou déjà entièrement soldé (aucune échéance non
    'paye'), si le montant dépasse le solde restant dû (message actionnable, nommant CE solde),
    ou si un rattachement comptable manque (compte de caisse ou, si ce versement couvre des
    intérêts, compte produits d'intérêts du produit)."""
    if demande.status != "decaisse":
        raise AucuneEcheanceAReglerError(
            f"Cette demande ({demande.application_number}) n'est pas décaissée : "
            "aucune échéance à régler."
        )

    echeance = prochaine_echeance(db, demande.id)
    if echeance is None:
        raise AucuneEcheanceAReglerError(
            f"Ce crédit ({demande.application_number}) est déjà entièrement soldé : "
            "aucune échéance à régler."
        )

    montant_paye_avant = echeance.montant_paye
    solde_du = echeance.total - montant_paye_avant
    if montant > solde_du:
        raise MontantIncorrectError(
            f"Le solde restant de cette échéance est de {solde_du} F, "
            f"vous avez saisi {montant} F."
        )

    # Ventilation intérêts d'abord (PROVISOIRE) : déduite de montant_paye_avant, aucune colonne
    # dédiée — voir docstring module.
    interets_deja_couverts = min(montant_paye_avant, echeance.interets)
    interets_restants = echeance.interets - interets_deja_couverts
    part_interets = min(montant, interets_restants)
    part_capital = montant - part_interets

    compte_caisse = db.execute(
        select(Agency.compte_caisse_id).where(Agency.id == demande.agency_id)
    ).scalar_one()
    if compte_caisse is None:
        raise RattachementManquantError(
            "l'agence de cette demande n'a pas de compte de caisse rattaché"
        )

    journal_id = db.execute(
        select(Journal.id).where(Journal.code == CODE_JOURNAL)
    ).scalar_one()

    lignes = [
        LigneSaisie(
            account_id=compte_caisse,
            side="D",
            amount=montant,
            label=f"Remboursement crédit {demande.application_number} #{echeance.numero}",
        ),
    ]
    if part_capital > 0:
        lignes.append(
            LigneSaisie(
                account_id=demande.compte_credit_id,
                side="C",
                amount=part_capital,
                label=(
                    f"Remboursement crédit {demande.application_number} "
                    f"#{echeance.numero} (capital)"
                ),
            )
        )
    if part_interets > 0:
        produit = db.get(Product, demande.product_id)
        if produit is None or produit.compte_produits_interets_id is None:
            raise RattachementManquantError(
                "ce produit de crédit n'a pas de compte de produits d'intérêts rattaché "
                "(plan comptable)"
            )
        lignes.append(
            LigneSaisie(
                account_id=produit.compte_produits_interets_id,
                side="C",
                amount=part_interets,
                label=(
                    f"Remboursement crédit {demande.application_number} "
                    f"#{echeance.numero} (intérêts)"
                ),
            )
        )

    jour = entry_date
    if jour is None:
        jour = db.execute(text("SELECT CURRENT_DATE")).scalar_one()

    entry: JournalEntry = ecritures.creer_brouillon(
        db,
        journal_id=journal_id,
        entry_date=jour,
        description=f"Remboursement crédit {demande.application_number} #{echeance.numero}",
        lignes=lignes,
        par=par,
    )
    ecritures.valider(db, entry, par, contexte=contexte)

    maintenant = db.execute(text("SELECT NOW()")).scalar_one()
    nouveau_montant_paye = montant_paye_avant + montant
    echeance_soldee = nouveau_montant_paye == echeance.total

    avant_statut = echeance.status
    echeance.montant_paye = nouveau_montant_paye
    if echeance_soldee:
        echeance.status = "paye"
        echeance.paid_at = maintenant
        echeance.paid_by = par
    else:
        echeance.status = "partiellement_paye"
    db.flush()

    db.add(
        Repayment(
            installment_id=echeance.id,
            application_id=demande.id,
            montant_capital=part_capital,
            montant_interets=part_interets,
            montant_total=montant,
            entry_id=entry.id,
            paid_by=par,
        )
    )
    db.flush()

    ecrire_audit(
        db,
        action="credit.echeance.payee",
        contexte=contexte,
        acteur_id=par,
        resource_type=RESSOURCE,
        resource_id=demande.id,
        agency_id=demande.agency_id,
        old_values={"status": avant_statut, "montant_paye": montant_paye_avant},
        new_values={
            "numero": echeance.numero,
            "montant_capital": part_capital,
            "montant_interets": part_interets,
            "montant_total": montant,
            "montant_paye": nouveau_montant_paye,
            "status": echeance.status,
            "entry_number": entry.entry_number,
        },
    )
    return ResultatRemboursement(
        echeance=echeance,
        montant=montant,
        montant_capital=part_capital,
        montant_interets=part_interets,
        paid_at=maintenant,
        solde_du=echeance.total - nouveau_montant_paye,
        echeance_soldee=echeance_soldee,
    )
