"""Crédit CR4 — remboursements : encaisser une échéance, D CAISSE / C CREDIT / C PRODUITS_INTERETS.

PAS DE GATE KYC ICI, à la différence du décaissement : encaisser de l'argent qui RENTRE ne
présente aucun risque (même logique que le refus toujours possible en CR1). Un tiers suspendu
peut rembourser.

PIÈCE CONSTRUITE DIRECTEMENT (ecritures.creer_brouillon/valider), PAS via le moteur générique
poser_depuis_schema : ce dernier applique un même montant à toutes les lignes d'un modèle à
nombre de lignes fixe — un remboursement a des montants DIFFÉRENTS par ligne (capital ≠
intérêts) et un nombre de lignes VARIABLE (la ligne PRODUITS_INTERETS est OMISE quand
interets=0, le moteur d'écriture refuse tout montant <= 0). Décision : ne pas complexifier un
moteur partagé par 3 modules pour un cas structurellement différent (voir migration 0034).

PÉRIMÈTRE v1 : une échéance à la fois, montant EXACT — pas de partiel, pas de groupé. Le
montant qui ne correspond pas au total attendu est refusé avec le montant attendu, lisible par
un caissier au guichet (même discipline que les refus du guichet Épargne).

TRANSACTION UNIQUE : la pièce, le passage 'a_echoir' -> 'paye' et le registre Repayment vivent
dans la même session, sans commit intermédiaire (ecritures.creer_brouillon/valider ne font que
flush). Si une étape lève après que la pièce a été posée, l'appelant rollback : rien ne persiste.
"""

import uuid
from datetime import date

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
    """Le montant versé ne correspond pas exactement au total de la prochaine échéance."""


def _prochaine_echeance(db: Session, application_id: uuid.UUID) -> Installment | None:
    return db.execute(
        select(Installment)
        .where(Installment.application_id == application_id, Installment.status == "a_echoir")
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
) -> Installment:
    """Règle la PROCHAINE échéance impayée d'un crédit décaissé, pour son montant EXACT.

    Refuse si le crédit n'est pas décaissé ou déjà entièrement soldé (aucune échéance
    'a_echoir'), si le montant ne correspond pas exactement au total attendu (message
    actionnable), ou si un rattachement comptable manque (compte de caisse ou, si l'échéance
    porte des intérêts, compte produits d'intérêts du produit)."""
    if demande.status != "decaisse":
        raise AucuneEcheanceAReglerError(
            f"Cette demande ({demande.application_number}) n'est pas décaissée : "
            "aucune échéance à régler."
        )

    echeance = _prochaine_echeance(db, demande.id)
    if echeance is None:
        raise AucuneEcheanceAReglerError(
            f"Ce crédit ({demande.application_number}) est déjà entièrement soldé : "
            "aucune échéance à régler."
        )

    if montant != echeance.total:
        raise MontantIncorrectError(
            f"Cette échéance est de {echeance.total} F, vous avez saisi {montant} F."
        )

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
        LigneSaisie(
            account_id=demande.compte_credit_id,
            side="C",
            amount=echeance.capital,
            label=f"Remboursement crédit {demande.application_number} #{echeance.numero} (capital)",
        ),
    ]
    if echeance.interets > 0:
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
                amount=echeance.interets,
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

    echeance.status = "paye"
    echeance.paid_at = db.execute(text("SELECT NOW()")).scalar_one()
    echeance.paid_by = par
    db.flush()

    db.add(
        Repayment(
            installment_id=echeance.id,
            application_id=demande.id,
            montant_capital=echeance.capital,
            montant_interets=echeance.interets,
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
        new_values={
            "numero": echeance.numero,
            "capital": echeance.capital,
            "interets": echeance.interets,
            "montant_total": montant,
            "entry_number": entry.entry_number,
        },
    )
    return echeance
