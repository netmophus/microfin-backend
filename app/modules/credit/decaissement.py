"""Crédit CR3 — décaissement : LA première écriture comptable du module (D crédit / C caisse),
l'échéancier persisté (CR2 appliqué), et l'ancrage du routage membre/client.

TRANSACTION UNIQUE : la pièce comptable, les échéances et le passage à 'decaisse' vivent dans
la même session, sans commit intermédiaire (ecritures.creer_brouillon/valider ne font que
flush — voir comptabilite/ecritures.py). Si generer_echeancier() lève après que la pièce a été
posée, l'appelant (le router) rollback : RIEN ne persiste, ni écriture sans échéancier, ni
échéancier sans statut décaissé.

GATE KYC, TROISIÈME ET DERNIER TEMPS (après la création et l'approbation, migration 0032) :
revérifie le tiers ACTIF à l'instant du décaissement — le plus critique des trois, puisque
c'est ici que l'argent sort réellement de la caisse. Pas d'asymétrie cette fois (voir 0033) :
un seul sens possible (approuve -> decaisse), toujours vérifié. Le trigger 0033 double ce
contrôle en base, dernier rempart.

ROUTAGE ANCRÉ (miroir PS3/épargne) : le compte de crédit (membre ou client, selon le produit)
est résolu UNE FOIS ici et stocké dans compte_credit_id — jamais re-routé ensuite, même si le
statut membre/client du tiers change après coup.
"""

import calendar
import uuid
from datetime import date

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.modules.audit.service import CONTEXTE_VIDE, ContexteRequete, ecrire_audit
from app.modules.comptabilite.models import JournalEntry
from app.modules.comptabilite.schemas_ecriture import poser_depuis_schema
from app.modules.credit.demandes import (
    RESSOURCE,
    CreditError,
    ProduitIntrouvableError,
    TierNonActifError,
    _statut_tier,
)
from app.modules.credit.echeancier import generer_echeancier
from app.modules.credit.models import Application, Installment, Product
from app.modules.parameters.models import Agency

CODE_DECAISSEMENT = "credit.decaissement"


class DemandeNonApprouveeError(CreditError):
    """Seule une demande APPROUVÉE peut être décaissée — jamais deux fois."""


class RattachementManquantError(CreditError):
    """Le produit ou l'agence n'a pas le compte nécessaire rattaché (plan comptable)."""


def _ajouter_periode(depart: date, periodicite: str) -> date:
    """Avance `depart` d'UNE période calendaire. Stdlib pure (calendar.monthrange), pas de
    nouvelle dépendance. Cale sur le dernier jour du mois cible si le jour d'origine n'y
    existe pas (31/01 + 1 mois -> 28/02 ou 29/02)."""
    mois_a_ajouter = {"mensuelle": 1, "trimestrielle": 3, "annuelle": 12}[periodicite]
    mois_total = depart.month - 1 + mois_a_ajouter
    annee = depart.year + mois_total // 12
    mois = mois_total % 12 + 1
    dernier_jour_du_mois = calendar.monthrange(annee, mois)[1]
    jour = min(depart.day, dernier_jour_du_mois)
    return date(annee, mois, jour)


def decaisser(
    db: Session,
    demande: Application,
    *,
    par: uuid.UUID | None,
    entry_date: date | None = None,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> Application:
    """Décaisse une demande APPROUVÉE. Pose la pièce comptable, génère et persiste
    l'échéancier (CR2), ancre le compte de crédit, fait passer la demande à 'decaisse'.
    Refuse si la demande n'est pas approuvée, si le tiers n'est plus actif, si le produit
    est devenu indisponible, si un rattachement comptable manque, ou si les paramètres
    produisent un échéancier impossible (EcheancierImpossibleError, non capturée ici — elle
    remonte telle quelle, la transaction reste non commitée)."""
    if demande.status != "approuve":
        raise DemandeNonApprouveeError(
            f"Cette demande ({demande.application_number}) n'est pas approuvée "
            f"(statut={demande.status}) : elle ne peut pas être décaissée."
        )

    statut_tier = _statut_tier(db, demande.tier_id)
    if statut_tier != "actif":
        raise TierNonActifError(
            "Ce tiers n'est plus actif : le décaissement est refusé."
        )

    produit = db.get(Product, demande.product_id)
    if produit is None or not produit.is_active:
        raise ProduitIntrouvableError("Produit de crédit inexistant ou devenu indisponible.")

    est_membre = db.execute(
        text("SELECT is_member FROM tiers.tiers WHERE id = :t"), {"t": demande.tier_id}
    ).scalar_one()
    # ROUTAGE ANCRÉ : membre -> compte membre ; client -> compte client SI configuré, sinon
    # repli sur le compte membre (comportement épargne, tant que le compte client n'est pas
    # rattaché). Figé ici, jamais re-routé ensuite.
    collectif = (
        produit.compte_credit_membre_id
        if (est_membre or produit.compte_credit_client_id is None)
        else produit.compte_credit_client_id
    )
    if collectif is None:
        raise RattachementManquantError(
            "ce produit de crédit n'a pas de compte de crédit rattaché (plan comptable) "
            "pour ce tiers"
        )

    compte_caisse = db.execute(
        select(Agency.compte_caisse_id).where(Agency.id == demande.agency_id)
    ).scalar_one()
    if compte_caisse is None:
        raise RattachementManquantError(
            "l'agence de cette demande n'a pas de compte de caisse rattaché"
        )

    jour = entry_date
    if jour is None:
        jour = db.execute(text("SELECT CURRENT_DATE")).scalar_one()

    def resoudre(role: str) -> uuid.UUID:
        if role == "CREDIT":
            return collectif
        if role == "CAISSE":
            return compte_caisse
        raise RattachementManquantError(f"rôle « {role} » inconnu dans le modèle d'écriture")

    piece: JournalEntry = poser_depuis_schema(
        db,
        code=CODE_DECAISSEMENT,
        montant=demande.montant_decide,
        resoudre_role=resoudre,
        entry_date=jour,
        par=par,
        description=f"Décaissement crédit {demande.application_number}",
        contexte=contexte,
    )

    # CR2, pur : peut lever EcheancierImpossibleError. La pièce ci-dessus n'a été que flush,
    # pas commit — si ça lève, l'appelant rollback et rien ne persiste (voir docstring module).
    echeances = generer_echeancier(
        montant=demande.montant_decide,
        taux_bp=produit.taux_bp,
        duree_echeances=demande.duree_echeances,
        periodicite=produit.periodicite,
        methode_amortissement=produit.methode_amortissement,
        regle_arrondi=produit.regle_arrondi,
    )

    echeance_date = jour
    for echeance in echeances:
        echeance_date = _ajouter_periode(echeance_date, produit.periodicite)
        db.add(
            Installment(
                application_id=demande.id,
                numero=echeance.numero,
                due_date=echeance_date,
                capital=echeance.capital,
                interets=echeance.interets,
                total=echeance.total,
                capital_restant_du=echeance.capital_restant_du,
            )
        )

    avant = {"status": demande.status}
    demande.status = "decaisse"
    demande.disbursed_at = db.execute(text("SELECT NOW()")).scalar_one()
    demande.disbursed_by = par
    demande.compte_credit_id = collectif
    demande.updated_by = par
    db.flush()

    ecrire_audit(
        db,
        action="credit.demande.decaissee",
        contexte=contexte,
        acteur_id=par,
        resource_type=RESSOURCE,
        resource_id=demande.id,
        agency_id=demande.agency_id,
        old_values=avant,
        new_values={
            "status": "decaisse",
            "montant_decide": demande.montant_decide,
            "entry_number": piece.entry_number,
            "nb_echeances": len(echeances),
        },
    )
    return demande
