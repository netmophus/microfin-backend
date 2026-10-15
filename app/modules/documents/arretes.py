"""Arrêté de caisse : état de la session à sa fermeture, en PDF archivé.

Généré APRÈS le commit de la fermeture (jamais dans la transaction), UNE SEULE FOIS par session :
l'archive est identifiée par la session (clé naturelle), un rejeu rend le document existant. Daté
et classé par la JOURNÉE COMPTABLE de la session ; les heures réelles d'ouverture et de fermeture
n'y figurent qu'à titre d'audit, converties dans le fuseau d'affichage de l'institution.

Les chiffres viennent de la même source que l'écran : le solde théorique et l'écart sont ceux FIGÉS
à la fermeture, les entrées et sorties sont recalculées par `calculer_mouvements` (la fonction
dont `calculer_solde_theorique` se sert), sur la même borne. Le flux de caisse n'est jamais bloqué :
toute erreur est journalisée et rend `None`.
"""

import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.modules.audit.service import CONTEXTE_VIDE, ContexteRequete
from app.modules.caisse.models import CaisseSession
from app.modules.caisse.schemas import SessionCaisse
from app.modules.caisse.service import calculer_mouvements
from app.modules.documents import service
from app.modules.documents.lettres import OBJET_SESSION, SANS_EXEMPLAIRE_NOMME
from app.modules.documents.libelles import LIBELLES
from app.modules.documents.models import STATUT_OK, Archive
from app.modules.documents.recus import MODULE_CAISSE, francs, nom_acteur
from app.modules.documents.rendu import formater_jour

journal = logging.getLogger(__name__)

TYPE_ARRETE = "arrete_caisse"
PREFIXE_ARRETE = "AC"
LARGEUR_RANG = 6

_ALLOUER_RANG = text(
    """
    INSERT INTO documents.numerotation (prefixe, annee, last_value)
    VALUES (:prefixe, :annee, 1)
    ON CONFLICT (prefixe, annee)
    DO UPDATE SET last_value = documents.numerotation.last_value + 1
    RETURNING last_value
    """
)


def _heure_reelle(instant: datetime) -> str:
    """« JJ/MM/AAAA HH:MM » à l'heure locale de l'institution (audit, jamais la journée)."""
    local = instant.astimezone(ZoneInfo(settings.FUSEAU_HORAIRE_DOCUMENTS))
    return f"{formater_jour(local.date())} {local.hour:02d}:{local.minute:02d}"


def dernier_arrete(db: Session, cle: str) -> Archive | None:
    """Le document le plus récent de cette session (`ok` ou `echec`), `None` s'il n'y en a pas."""
    return db.execute(
        select(Archive)
        .where(Archive.type == TYPE_ARRETE, Archive.cle_naturelle == cle)
        .order_by(Archive.cree_le.desc())
        .limit(1)
    ).scalar_one_or_none()


def _numero_de_piece(db: Session, annee: int) -> str:
    """Alloue le prochain rang de l'année et le VALIDE aussitôt : le numéro est ensuite porté par
    la demande, donc par l'index (y compris une ligne `echec`, que la régénération réutilise) —
    jamais deux arrêtés au même numéro, même si la génération échoue."""
    rang: int = db.execute(_ALLOUER_RANG, {"prefixe": PREFIXE_ARRETE, "annee": annee}).scalar_one()
    db.commit()
    return f"{PREFIXE_ARRETE}-{annee}-{rang:0{LARGEUR_RANG}d}"


def _contexte(
    numero: str, caissier: str, session: SessionCaisse, entrees: int, sorties: int
) -> dict[str, Any]:
    theorique = session.solde_theorique_cloture or 0
    ecart = session.ecart or 0
    return {
        "titre": LIBELLES["titre_arrete"],
        "reference": LIBELLES["numero_arrete"].format(numero=numero),
        "exemplaires": SANS_EXEMPLAIRE_NOMME,
        "caissier": caissier,
        "agence_session": session.agency_nom,
        "compte_caisse": session.compte_caisse_number,
        "journee_session": formater_jour(session.journee_comptable),
        "ouverture_reelle": _heure_reelle(session.opened_at),
        "fermeture_reelle": _heure_reelle(session.closed_at) if session.closed_at else "—",
        "fonds_initial_affiche": francs(session.fonds_initial),
        "entrees_affiche": francs(entrees),
        "sorties_affiche": francs(sorties),
        "solde_theorique_affiche": francs(theorique),
        "solde_theorique": theorique,
        "montant_compte_affiche": francs(session.montant_reel_cloture or 0),
        "ecart_affiche": francs(ecart),
        "ecart": ecart,
        "motif_ecart": session.motif_ecart,
    }


def archiver_arrete(
    db: Session,
    session: SessionCaisse,
    *,
    contexte_requete: ContexteRequete = CONTEXTE_VIDE,
) -> Archive | None:
    """Archive l'arrêté d'une session FERMÉE (à appeler après le commit de la fermeture). Rend le
    document existant si la session en a déjà un ; `None` si la session n'est pas fermée ou si la
    construction échoue (journalisé : la fermeture, elle, est déjà validée)."""
    try:
        if session.status != "fermee" or session.closed_at is None:
            return None
        cle = str(session.id)
        precedent = dernier_arrete(db, cle)
        if precedent is not None and precedent.statut == STATUT_OK:
            return precedent
        orm = db.get(CaisseSession, session.id)
        if orm is None:
            return None
        entrees, sorties = calculer_mouvements(db, orm, jusqu_a=session.closed_at)
        journee = session.journee_comptable
        # Un échec antérieur porte déjà son numéro : on le garde, on n'en consomme pas un second.
        numero = (
            precedent.numero_piece
            if precedent is not None and precedent.numero_piece
            else _numero_de_piece(db, journee.year)
        )
        demande = service.DemandeDocument(
            type=TYPE_ARRETE,
            module=MODULE_CAISSE,
            cle_naturelle=cle,
            journee=journee,
            agency_id=session.agency_id,
            nom=f"arrete_caisse_{session.compte_caisse_number}_{journee.isoformat()}_{numero}",
            contexte=_contexte(
                numero, nom_acteur(db, session.caissier_id), session, entrees, sorties
            ),
            numero_piece=numero,
            objet_lie_type=OBJET_SESSION,
            objet_lie_id=session.id,
            acteur_id=session.caissier_id,
        )
        return service.generer_apres_commit(db, demande, contexte_requete=contexte_requete)
    except Exception:
        journal.exception("Construction de l'arrêté de caisse impossible (session %s)", session.id)
        return None
