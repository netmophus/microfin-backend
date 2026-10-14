"""Reçus de parts sociales : souscription au comptant, libération, remboursement.

Appelé par le routeur Tiers APRÈS le commit de l'opération. Le montant, la valeur de la part et
l'agence sont relus sur le mouvement de parts rattaché à la pièce : le reçu reflète ce qui a été
écrit, pas la configuration du moment. Comme les autres reçus, jamais bloquant : toute erreur est
journalisée et rend `None`.

Le remboursement est une SORTIE d'espèces : la signature du client est obligatoire.
"""

import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.audit.service import CONTEXTE_VIDE, ContexteRequete
from app.modules.comptabilite.models import JournalEntry
from app.modules.documents import service
from app.modules.documents.libelles import LIBELLES
from app.modules.documents.models import Archive
from app.modules.documents.recus import francs, nom_acteur
from app.modules.epargne import consultation
from app.modules.security.autorisation import UtilisateurCourant
from app.modules.tiers.models import ShareSubscription
from app.modules.tiers.parts import ResultatParts

journal = logging.getLogger(__name__)

MODULE_TIERS = "tiers"
OBJET_TIERS = "tiers.tier"
SOUSCRIPTION_COMPTANT = "souscription_comptant"
LIBERATION = "liberation"
REMBOURSEMENT = "remboursement"

# type de mouvement -> (type de document, titre, libellé du montant, signature obligatoire)
_PAR_OPERATION: dict[str, tuple[str, str, str, bool]] = {
    SOUSCRIPTION_COMPTANT: (
        "recu_parts_souscription", "titre_parts_souscription", "montant_souscrit", False,
    ),
    LIBERATION: ("recu_parts_liberation", "titre_parts_liberation", "montant_libere", False),
    REMBOURSEMENT: (
        "recu_parts_remboursement", "titre_parts_remboursement", "montant_rembourse", True,
    ),
}


def archiver_recu_parts(
    db: Session,
    courant: UtilisateurCourant,
    tier_id: uuid.UUID,
    resultat: ResultatParts,
    *,
    operation: str,
    contexte_requete: ContexteRequete = CONTEXTE_VIDE,
) -> Archive | None:
    """Reçu d'une opération de parts d'espèces (`operation` : clé de `_PAR_OPERATION`)."""
    try:
        type_document, cle_titre, cle_montant, signature = _PAR_OPERATION[operation]
        mouvement = db.execute(
            select(ShareSubscription)
            .join(JournalEntry, JournalEntry.id == ShareSubscription.journal_entry_id)
            .where(
                ShareSubscription.tier_id == tier_id,
                JournalEntry.entry_number == resultat.entry_number,
            )
        ).scalar_one_or_none()
        if mouvement is None:
            return None
        piece = resultat.entry_number or "SANS-PIECE"
        caissier = nom_acteur(db, courant.user_id)
        contexte: dict[str, Any] = {
            "titre": LIBELLES[cle_titre],
            "reference": LIBELLES["numero_recu"].format(numero=piece),
            "signature_client_requise": signature,
            "caissier": caissier,
            "membre": consultation.nom_du_membre(db, tier_id),
            "compte": None,
            "lignes": [
                {"libelle": LIBELLES["parts_nombre"], "valeur": str(mouvement.shares_count)},
                {
                    "libelle": LIBELLES["parts_valeur_unitaire"],
                    "valeur": francs(mouvement.unit_value),
                },
                {
                    "libelle": LIBELLES["parts_liberees_apres"],
                    "valeur": str(resultat.shares_liberees),
                },
                {
                    "libelle": LIBELLES["parts_non_liberees_apres"],
                    "valeur": str(resultat.shares_non_liberees),
                },
            ],
            "libelle_montant": LIBELLES[cle_montant],
            "montant": mouvement.amount,
            "montant_affiche": francs(mouvement.amount),
            "solde_affiche": None,
        }
        demande = service.DemandeDocument(
            type=type_document,
            module=MODULE_TIERS,
            cle_naturelle=f"{tier_id}:{piece}",
            journee=resultat.date_comptable,
            agency_id=mouvement.agency_id,
            nom=f"{type_document}_{piece}",
            contexte=contexte,
            numero_piece=resultat.entry_number,
            objet_lie_type=OBJET_TIERS,
            objet_lie_id=tier_id,
            acteur_id=courant.user_id,
        )
        return service.generer_apres_commit(db, demande, contexte_requete=contexte_requete)
    except Exception:
        journal.exception("Construction du reçu de parts impossible (tiers %s)", tier_id)
        return None
