"""Reçus de caisse / épargne / dépôt à terme et bon de dépôt à terme : construction des demandes
d'archivage à partir du RÉSULTAT d'une opération déjà validée.

Appelé par les routeurs APRÈS le commit de l'opération. Tout est dérivé du résultat et de la base —
numéro de reçu = numéro de la pièce comptable, date = journée comptable de la pièce (jamais
l'horloge), nom du caissier et du membre lus en base. Aucune règle métier dans les gabarits : les
lignes, les libellés de montant et l'exigence de signature se décident ICI, par type.

Le flux de guichet n'est JAMAIS bloqué : toute erreur — même dans la construction des données — est
journalisée et rend `None` ; le routeur répond alors sans référence de document, et l'écran retombe
sur son repli.
"""

import logging
import uuid
from datetime import date
from typing import Any

from sqlalchemy.orm import Session

from app.modules.audit.service import CONTEXTE_VIDE, ContexteRequete
from app.modules.documents import service
from app.modules.documents.libelles import LIBELLES
from app.modules.documents.models import Archive
from app.modules.documents.rendu import formater_jour
from app.modules.documents.schemas import DocumentRef
from app.modules.epargne import consultation
from app.modules.epargne.guichet import (
    ResultatDenouement,
    ResultatOperation,
    ResultatOuvertureDepotATerme,
)
from app.modules.epargne.models import SavingsAccount
from app.modules.epargne.terme import MODE_ECHEANCE, interets_du_contrat
from app.modules.security.autorisation import UtilisateurCourant
from app.modules.security.models import User

journal = logging.getLogger(__name__)

MODULE_CAISSE = "caisse"
MODULE_EPARGNE = "epargne"
TYPE_VERSEMENT = "recu_versement"
TYPE_RETRAIT = "recu_retrait"
TYPE_PLACEMENT = "recu_placement_dat"
TYPE_DENOUEMENT = "recu_denouement_dat"
TYPE_RETRAIT_ANTICIPE = "recu_retrait_anticipe_dat"
TYPE_BON = "bon_dat"
OBJET_COMPTE = "epargne.compte"
ESPACE_INSECABLE = chr(0xA0)  # espace insécable (U+00A0) : milliers et « F » ne se coupent pas
POINTS_DE_BASE_PAR_POURCENT = 100


def francs(montant: int) -> str:
    """« 1 000 000 F » : séparateur de milliers et « F » insécables, jamais de décimale."""
    return f"{montant:,}".replace(",", ESPACE_INSECABLE) + f"{ESPACE_INSECABLE}F"


def pourcentage(points_de_base: int) -> str:
    """Points de base -> pourcentage lisible : 500 -> « 5 % », 350 -> « 3,5 % »."""
    valeur = f"{points_de_base / POINTS_DE_BASE_PAR_POURCENT:g}".replace(".", ",")
    return f"{valeur}{ESPACE_INSECABLE}%"


def _nom_acteur(db: Session, user_id: uuid.UUID) -> str:
    """« Nom Prénom » (ordre unifié du produit), l'identifiant à défaut."""
    user = db.get(User, user_id)
    if user is None:
        return "—"
    complet = f"{user.last_name} {user.first_name}".strip()
    return complet or user.username


def _ligne(libelle: str, valeur: str) -> dict[str, str]:
    return {"libelle": libelle, "valeur": valeur}


def _demande_recu(
    db: Session,
    courant: UtilisateurCourant,
    compte: SavingsAccount,
    *,
    type_document: str,
    module: str,
    prefixe_nom: str,
    titre: str,
    piece: str | None,
    journee: date,
    libelle_montant: str,
    montant: int,
    lignes: list[dict[str, str]],
    solde: int | None,
    signature_requise: bool,
) -> service.DemandeDocument:
    piece_affichee = piece or "SANS-PIECE"
    contexte: dict[str, Any] = {
        "titre": titre,
        "reference": LIBELLES["numero_recu"].format(numero=piece_affichee),
        "signature_client_requise": signature_requise,
        "caissier": _nom_acteur(db, courant.user_id),
        "membre": consultation.nom_du_membre(db, compte.tier_id),
        "compte": compte.account_number,
        "lignes": lignes,
        "libelle_montant": libelle_montant,
        "montant": montant,
        "montant_affiche": francs(montant),
        "solde_affiche": francs(solde) if solde is not None else None,
    }
    return service.DemandeDocument(
        type=type_document,
        module=module,
        cle_naturelle=f"{compte.account_number}:{piece_affichee}",
        journee=journee,
        agency_id=compte.agency_id,
        nom=f"{prefixe_nom}_{compte.account_number}_{piece_affichee}",
        contexte=contexte,
        numero_piece=piece,
        objet_lie_type=OBJET_COMPTE,
        objet_lie_id=compte.id,
        acteur_id=courant.user_id,
    )


def _generer(
    db: Session, demande: service.DemandeDocument, contexte_requete: ContexteRequete
) -> Archive:
    return service.generer_apres_commit(db, demande, contexte_requete=contexte_requete)


def archiver_recu_operation(
    db: Session,
    courant: UtilisateurCourant,
    compte_id: uuid.UUID,
    resultat: ResultatOperation,
    *,
    versement: bool,
    montant: int,
    contexte_requete: ContexteRequete = CONTEXTE_VIDE,
) -> Archive | None:
    """Reçu d'un versement (signature facultative) ou d'un retrait (signature obligatoire)."""
    try:
        compte = db.get(SavingsAccount, compte_id)
        if compte is None:
            return None
        demande = _demande_recu(
            db, courant, compte,
            type_document=TYPE_VERSEMENT if versement else TYPE_RETRAIT,
            module=MODULE_CAISSE,
            prefixe_nom="recu_versement" if versement else "recu_retrait",
            titre=LIBELLES["titre_versement" if versement else "titre_retrait"],
            piece=resultat.entry_number,
            journee=resultat.date_comptable,
            libelle_montant=LIBELLES["montant_verse" if versement else "montant_retire"],
            montant=montant,
            lignes=[],
            solde=resultat.nouveau_solde,
            signature_requise=not versement,
        )
        return _generer(db, demande, contexte_requete)
    except Exception:
        journal.exception("Construction du reçu impossible (compte %s)", compte_id)
        return None


def archiver_placement(
    db: Session,
    courant: UtilisateurCourant,
    resultat: ResultatOuvertureDepotATerme,
    *,
    contexte_requete: ContexteRequete = CONTEXTE_VIDE,
) -> tuple[Archive | None, Archive | None]:
    """Au placement d'un dépôt à terme : le REÇU du capital encaissé ET le BON (le contrat).
    Chacun s'archive indépendamment : l'échec de l'un ne prive pas du second."""
    compte = resultat.compte
    conditions = resultat.conditions
    piece = resultat.versement.entry_number
    journee = resultat.versement.date_comptable
    recu: Archive | None = None
    bon: Archive | None = None
    try:
        interets = interets_du_contrat(conditions)
        demande_recu = _demande_recu(
            db, courant, compte,
            type_document=TYPE_PLACEMENT,
            module=MODULE_EPARGNE,
            prefixe_nom="recu_placement_dat",
            titre=LIBELLES["titre_placement"],
            piece=piece,
            journee=journee,
            libelle_montant=LIBELLES["capital_encaisse"],
            montant=conditions.principal,
            lignes=[
                _ligne(
                    LIBELLES["duree"],
                    LIBELLES["duree_valeur"].format(mois=conditions.term_months),
                ),
                _ligne(LIBELLES["echeance"], formater_jour(conditions.maturity_date)),
                _ligne(LIBELLES["interets_attendus"], francs(interets)),
            ],
            solde=None,
            signature_requise=False,
        )
        recu = _generer(db, demande_recu, contexte_requete)
    except Exception:
        journal.exception("Construction du reçu de placement impossible (%s)", compte.id)
    try:
        bon = _generer(
            db, _demande_bon(db, courant, resultat, interets_du_contrat(conditions)),
            contexte_requete,
        )
    except Exception:
        journal.exception("Construction du bon de dépôt à terme impossible (%s)", compte.id)
    return recu, bon


def _demande_bon(
    db: Session,
    courant: UtilisateurCourant,
    resultat: ResultatOuvertureDepotATerme,
    interets: int,
) -> service.DemandeDocument:
    compte = resultat.compte
    c = resultat.conditions
    penalite = pourcentage(c.penalite_bp)
    contexte: dict[str, Any] = {
        "titre": LIBELLES["titre_bon"],
        "reference": LIBELLES["numero_bon"].format(numero=compte.account_number),
        "signature_client_requise": True,
        "caissier": _nom_acteur(db, courant.user_id),
        "membre": consultation.nom_du_membre(db, compte.tier_id),
        "compte": compte.account_number,
        "lignes": [
            _ligne(LIBELLES["depart"], formater_jour(c.term_start_date)),
            _ligne(LIBELLES["echeance"], formater_jour(c.maturity_date)),
            _ligne(LIBELLES["duree"], LIBELLES["duree_valeur"].format(mois=c.term_months)),
            _ligne(LIBELLES["taux_annuel"], pourcentage(c.taux_bp)),
            _ligne(LIBELLES["interets_attendus"], francs(interets)),
            _ligne(LIBELLES["penalite"], LIBELLES["penalite_valeur"].format(pourcent=penalite)),
        ],
        "montant": c.principal,
        "montant_affiche": francs(c.principal),
        "clause_echeance": LIBELLES["clause_echeance"],
        "clause_retrait_anticipe": LIBELLES["clause_retrait_anticipe"].format(pourcent=penalite),
    }
    return service.DemandeDocument(
        type=TYPE_BON,
        module=MODULE_EPARGNE,
        cle_naturelle=compte.account_number,
        journee=c.term_start_date,
        agency_id=compte.agency_id,
        nom=f"bon_dat_{compte.account_number}",
        contexte=contexte,
        numero_piece=resultat.versement.entry_number,
        objet_lie_type=OBJET_COMPTE,
        objet_lie_id=compte.id,
        acteur_id=courant.user_id,
    )


def archiver_solde_dat(
    db: Session,
    courant: UtilisateurCourant,
    compte_id: uuid.UUID,
    resultat: ResultatDenouement,
    *,
    contexte_requete: ContexteRequete = CONTEXTE_VIDE,
) -> Archive | None:
    """Reçu d'un dénouement à l'échéance ou d'un retrait anticipé : le détail du calcul, le total
    remis, la signature du client OBLIGATOIRE."""
    try:
        compte = db.get(SavingsAccount, compte_id)
        if compte is None:
            return None
        echeance = resultat.mode == MODE_ECHEANCE
        lignes = [_ligne(LIBELLES["capital"], francs(resultat.capital))]
        if echeance:
            lignes.append(_ligne(LIBELLES["interets"], francs(resultat.interets_nets)))
        else:
            lignes.append(_ligne(LIBELLES["interets_courus"], francs(resultat.interets_courus)))
            if resultat.penalite > 0:
                lignes.append(_ligne(LIBELLES["penalite"], francs(resultat.penalite)))
            lignes.append(_ligne(LIBELLES["interets_nets"], francs(resultat.interets_nets)))
        demande = _demande_recu(
            db, courant, compte,
            type_document=TYPE_DENOUEMENT if echeance else TYPE_RETRAIT_ANTICIPE,
            module=MODULE_EPARGNE,
            prefixe_nom="recu_denouement_dat" if echeance else "recu_retrait_anticipe_dat",
            titre=LIBELLES["titre_denouement" if echeance else "titre_retrait_anticipe"],
            piece=resultat.entry_number,
            journee=resultat.date_comptable,
            libelle_montant=LIBELLES["total_remis"],
            montant=resultat.total_paye,
            lignes=lignes,
            solde=None,
            signature_requise=True,
        )
        return _generer(db, demande, contexte_requete)
    except Exception:
        journal.exception("Construction du reçu de solde de DAT impossible (%s)", compte_id)
        return None


def reference_de(archive: Archive | None) -> DocumentRef | None:
    """La référence jointe au résultat d'une opération. Un document en ÉCHEC est référencé aussi
    (statut `echec`) : l'écran a besoin de son identifiant pour proposer « Régénérer ». `None`
    seulement si rien n'a pu être tracé du tout."""
    if archive is None:
        return None
    return DocumentRef(id=archive.id, nom=service.nom_de_fichier(archive), statut=archive.statut)
