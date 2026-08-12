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
zéro. Les deux sont des PARAMÈTRES PRODUIT provisoires (l'expert dira les valeurs).

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

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.modules.audit.service import CONTEXTE_VIDE, ContexteRequete, ecrire_audit
from app.modules.caisse.service import resoudre_session_active
from app.modules.epargne import service
from app.modules.epargne.models import Product, SavingsAccount, SavingsMovement
from app.modules.epargne.operations import TYPE_DEPOT, TYPE_RETRAIT, poser_ecriture_operation
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


class ActionsAudit:
    DEPOT = "epargne.depot"
    RETRAIT = "epargne.retrait"


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


def _enregistrer_mouvement(
    db: Session,
    compte: SavingsAccount,
    *,
    sens: str,
    montant: int,
    balance_after: int,
    operation_type: str,
    journal_entry_id: uuid.UUID,
    par: uuid.UUID | None,
) -> None:
    """Écrit le mouvement append-only, relié à sa pièce comptable."""
    db.add(
        SavingsMovement(
            account_id=compte.id,
            sens=sens,
            amount=montant,
            balance_after=balance_after,
            operation_type=operation_type,
            journal_entry_id=journal_entry_id,
            created_by=par,
        )
    )
    db.flush()


def _operer(
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
) -> ResultatOperation:
    """Tronc commun dépôt/retrait : verrou, contrôles, pièce + mouvement + solde, audit, commit.

    Bloc C4 : dépôt ET retrait sont tous deux des mouvements de caisse (D 5721/C 3111 ou
    l'inverse) — exige une session de caisse OUVERTE pour l'acteur, et c'est le compte ANCRÉ de
    CETTE session qui reçoit l'écriture, jamais celui de l'agence."""
    compte_caisse_id = resoudre_session_active(db, courant.user_id).compte_caisse_id
    compte = _charger_compte_verrou(db, courant, compte_id)
    _controler(db, compte, montant)

    if signe < 0:  # retrait : contrôle du plancher
        produit = db.get(Product, compte.product_id)
        assert produit is not None  # FK garantit l'existence
        disponible = compte.balance - produit.min_balance + produit.decouvert_autorise
        if montant > disponible:
            # Détail (solde minimum / découvert) SEULEMENT s'il fait diverger le disponible du
            # solde brut ; sinon « disponible = solde », inutile à dire.
            raison = ""
            if disponible != compte.balance:
                raisons = []
                if produit.min_balance > 0:
                    raisons.append(
                        f"un solde minimum de {_fcfa(produit.min_balance)} doit rester "
                        "sur le compte"
                    )
                if produit.decouvert_autorise > 0:
                    raisons.append(
                        f"un découvert de {_fcfa(produit.decouvert_autorise)} est autorisé"
                    )
                raison = f" ({' et '.join(raisons)})"
            raise SoldeInsuffisantError(
                f"Retrait impossible : le solde disponible est de {_fcfa(disponible)}{raison}, "
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
    db.commit()
    return ResultatOperation(compte.account_number, nouveau_solde, piece.entry_number)


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
