"""États comptables de la journée : journal du jour (brouillard) et balance du jour.

  - À la CLÔTURE de la journée (après le commit), ils sont générés, archivés et FIGÉS : une
    journée clôturée est définitive, ses états aussi. Idempotent (clé naturelle = journée).
  - À la DEMANDE pour la journée OUVERTE : un BROUILLON rendu à la volée, jamais archivé ni indexé
    (un brouillon n'est pas une pièce) et marqué comme tel sur chaque page.

La comptabilité est centralisée (écritures sans agence) : ces deux états couvrent tout le réseau.
Rattachés à l'agence de la personne qui clôture pour l'archive ; l'accès est donc réservé à
`compta.rapport.read` (même droit que le grand livre et la balance), pas au périmètre d'agence.

Balance du jour : balance générale ARRÊTÉE à la journée — soldes CUMULÉS depuis l'origine, pas
seulement les mouvements du jour.
"""

import logging
import uuid
from datetime import date
from typing import Any

from sqlalchemy.orm import Session

from app.modules.audit.service import CONTEXTE_VIDE, ContexteRequete, ecrire_audit
from app.modules.comptabilite import rapports
from app.modules.comptabilite.rapports import JournalDuJourResultat
from app.modules.documents import service
from app.modules.documents.libelles import LIBELLES
from app.modules.documents.models import Archive
from app.modules.documents.recus import francs
from app.modules.documents.rendu import formater_jour
from app.modules.security.autorisation import UtilisateurCourant

journal = logging.getLogger(__name__)

MODULE_COMPTABILITE = "comptabilite"
TYPE_JOURNAL = "journal_du_jour"
TYPE_BALANCE = "balance_du_jour"
TYPES_ETATS = service.TYPES_ETATS_COMPTABLES
OBJET_JOURNEE = "compta.journee"
ACTION_BROUILLON = "documents.brouillon.genere"
SANS_EXEMPLAIRE_NOMME = ("",)
PERMISSION_LECTURE_ETATS = service.PERMISSION_ETATS_COMPTABLES


class AgenceDeRattachementManquanteError(Exception):
    """L'acteur n'a aucune agence : l'archive d'un état ne saurait pas où se ranger."""


def _contexte_journal(resultat: JournalDuJourResultat, brouillon: bool) -> dict[str, Any]:
    return {
        "titre": LIBELLES["titre_journal"],
        "reference": formater_jour(resultat.jour),
        "exemplaires": SANS_EXEMPLAIRE_NOMME,
        "brouillon": brouillon,
        "nombre_ecritures": resultat.nombre_ecritures,
        "total_debit_affiche": francs(resultat.total_debit),
        "total_credit_affiche": francs(resultat.total_credit),
        "groupes": [
            {
                "code": groupe.code,
                "nom": groupe.nom,
                "total_debit_affiche": francs(groupe.total_debit),
                "total_credit_affiche": francs(groupe.total_credit),
                "ecritures": [
                    {
                        "numero": ecriture.numero or "—",
                        "libelle": ecriture.libelle,
                        "lignes": [
                            {
                                "compte": ligne.compte_numero,
                                "compte_nom": ligne.compte_nom,
                                "debit": francs(ligne.debit) if ligne.debit else "",
                                "credit": francs(ligne.credit) if ligne.credit else "",
                            }
                            for ligne in ecriture.lignes
                        ],
                    }
                    for ecriture in groupe.ecritures
                ],
            }
            for groupe in resultat.groupes
        ],
    }


def _soldes(solde_signe: int, cote_normal: str) -> tuple[int, int]:
    """(solde débiteur, solde créditeur) d'un compte, d'après son solde au sens normal."""
    est_debiteur = (solde_signe >= 0) == (cote_normal == "D")
    montant = abs(solde_signe)
    return (montant, 0) if est_debiteur else (0, montant)


def _contexte_balance(db: Session, jour: date, brouillon: bool) -> dict[str, Any]:
    resultat = rapports.balance(db, date_debut=jour, date_fin=jour, inclure_sans_mouvement=True)
    lignes: list[dict[str, str]] = []
    total_mouvement_debit = total_mouvement_credit = 0
    total_solde_debit = total_solde_credit = 0
    for ligne in resultat.lignes:
        if not (ligne.total_debit or ligne.total_credit or ligne.solde_cloture):
            continue
        solde_debit, solde_credit = _soldes(ligne.solde_cloture, ligne.compte.normal_side)
        total_mouvement_debit += ligne.total_debit
        total_mouvement_credit += ligne.total_credit
        total_solde_debit += solde_debit
        total_solde_credit += solde_credit
        lignes.append(
            {
                "compte": ligne.compte.account_number,
                "compte_nom": ligne.compte.name,
                "mouvement_debit": francs(ligne.total_debit) if ligne.total_debit else "",
                "mouvement_credit": francs(ligne.total_credit) if ligne.total_credit else "",
                "solde_debit": francs(solde_debit) if solde_debit else "",
                "solde_credit": francs(solde_credit) if solde_credit else "",
            }
        )
    return {
        "titre": LIBELLES["titre_balance"],
        "reference": formater_jour(jour),
        "exemplaires": SANS_EXEMPLAIRE_NOMME,
        "brouillon": brouillon,
        "lignes": lignes,
        "total_mouvement_debit": francs(total_mouvement_debit),
        "total_mouvement_credit": francs(total_mouvement_credit),
        "total_solde_debit": francs(total_solde_debit),
        "total_solde_credit": francs(total_solde_credit),
    }


def _contexte_de(db: Session, type_etat: str, jour: date, brouillon: bool) -> dict[str, Any]:
    if type_etat == TYPE_JOURNAL:
        return _contexte_journal(rapports.journal_du_jour(db, jour), brouillon)
    return _contexte_balance(db, jour, brouillon)


def _demande(
    db: Session,
    type_etat: str,
    jour: date,
    agency_id: uuid.UUID,
    acteur_id: uuid.UUID | None,
    *,
    brouillon: bool,
) -> service.DemandeDocument:
    return service.DemandeDocument(
        type=type_etat,
        module=MODULE_COMPTABILITE,
        cle_naturelle=jour.isoformat(),
        journee=jour,
        agency_id=agency_id,
        nom=f"{type_etat}_{jour.isoformat()}",
        contexte=_contexte_de(db, type_etat, jour, brouillon),
        objet_lie_type=OBJET_JOURNEE,
        acteur_id=acteur_id,
    )


def agence_de_rattachement(courant: UtilisateurCourant) -> uuid.UUID:
    agence = courant.agency_id or courant.primary_agency_id
    if agence is None:
        raise AgenceDeRattachementManquanteError()
    return agence


def archiver_etats_cloture(
    db: Session,
    courant: UtilisateurCourant,
    jour: date,
    *,
    contexte_requete: ContexteRequete = CONTEXTE_VIDE,
) -> list[Archive]:
    """Journal et balance de la journée qui vient d'être CLÔTURÉE, archivés et figés. À appeler
    APRÈS le commit de la clôture ; ne lève jamais (la clôture est acquise). Chacun s'archive
    indépendamment : l'échec de l'un ne prive pas du second."""
    archives: list[Archive] = []
    try:
        agence = agence_de_rattachement(courant)
    except AgenceDeRattachementManquanteError:
        journal.exception("États de clôture non archivés : l'acteur n'a pas d'agence")
        return archives
    for type_etat in TYPES_ETATS:
        try:
            demande = _demande(db, type_etat, jour, agence, courant.user_id, brouillon=False)
            archives.append(
                service.generer_apres_commit(db, demande, contexte_requete=contexte_requete)
            )
        except Exception:
            db.rollback()
            journal.exception("Construction de l'état %s impossible (%s)", type_etat, jour)
    return archives


def rendre_brouillon(
    db: Session,
    courant: UtilisateurCourant,
    type_etat: str,
    jour: date,
    *,
    contexte_requete: ContexteRequete = CONTEXTE_VIDE,
) -> tuple[bytes, str]:
    """Le PDF BROUILLON d'un état de la journée ouverte, et son nom : rendu à la volée, sans
    archive ni index. Lève si le rendu échoue (l'appelant répond par une erreur lisible). Consulter
    la situation comptable est un acte à tracer : une ligne d'audit, même sans archive."""
    agence = agence_de_rattachement(courant)
    demande = _demande(db, type_etat, jour, agence, courant.user_id, brouillon=True)
    pdf = service.rendre_sans_archiver(db, demande)
    ecrire_audit(
        db,
        action=ACTION_BROUILLON,
        contexte=contexte_requete,
        acteur_id=courant.user_id,
        resource_type=OBJET_JOURNEE,
        agency_id=agence,
        new_values={"type": type_etat, "journee_comptable": jour.isoformat()},
    )
    db.commit()
    return pdf, f"brouillon_{demande.nom}.pdf"
