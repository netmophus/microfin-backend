"""Lettre de demande d'explication d'un manquant de caisse, en PDF archivé.

Générée À LA DEMANDE (par le responsable) puis archivée UNE SEULE FOIS : l'archive est identifiée
par la session (clé naturelle) ; toute demande suivante rend le même document. Jamais produite
pour un écart nul ou un excédent (hors périmètre, décision explicite du métier).

La journée du document est celle de la SESSION (stockée à l'ouverture), jamais l'horloge. Les
heures réelles d'ouverture et de fermeture n'y figurent pas : aucun fuseau horaire n'est
paramétré côté serveur, et la journée comptable suffit à identifier la session.
"""

from typing import Any

from sqlalchemy.orm import Session

from app.modules.audit.service import CONTEXTE_VIDE, ContexteRequete
from app.modules.caisse.schemas import SessionCaisse
from app.modules.documents import service
from app.modules.documents.libelles import LIBELLES
from app.modules.documents.models import Archive
from app.modules.documents.recus import francs, nom_acteur
from app.modules.documents.rendu import formater_jour

MODULE_CAISSE = "caisse"
TYPE_LETTRE = "lettre_explication_ecart"
OBJET_SESSION = "caisse.session"
SANS_EXEMPLAIRE_NOMME = ("",)  # un seul exemplaire, sans étiquette « client / souche »
LONGUEUR_REFERENCE_SESSION = 8


class SessionSansManquantError(Exception):
    """Pas de lettre pour une session ouverte, sans écart ou en excédent."""


def a_un_manquant(session: SessionCaisse) -> bool:
    return session.status == "fermee" and session.ecart is not None and session.ecart < 0


def archiver_lettre(
    db: Session,
    session: SessionCaisse,
    *,
    contexte_requete: ContexteRequete = CONTEXTE_VIDE,
) -> Archive:
    """Rend l'archive de la lettre de cette session (la génère si elle n'existe pas encore).
    Le document appartient au caissier concerné (`acteur_id`) : il peut relire SA lettre."""
    if not a_un_manquant(session):
        raise SessionSansManquantError()
    manquant = francs(abs(session.ecart or 0))
    journee = session.journee_comptable
    contexte: dict[str, Any] = {
        "titre": LIBELLES["titre_lettre"],
        "reference": LIBELLES["numero_lettre"].format(
            numero=str(session.id)[:LONGUEUR_REFERENCE_SESSION].upper()
        ),
        "exemplaires": SANS_EXEMPLAIRE_NOMME,
        "caissier": nom_acteur(db, session.caissier_id),
        "agence_session": session.agency_nom,
        "compte_caisse": session.compte_caisse_number,
        "fonds_initial_affiche": francs(session.fonds_initial),
        "solde_theorique_affiche": francs(session.solde_theorique_cloture or 0),
        "montant_compte_affiche": francs(session.montant_reel_cloture or 0),
        "manquant_affiche": manquant,
        "texte_formel": LIBELLES["lettre_texte_formel"].format(
            montant=manquant, date=formater_jour(journee)
        ),
    }
    demande = service.DemandeDocument(
        type=TYPE_LETTRE,
        module=MODULE_CAISSE,
        cle_naturelle=str(session.id),
        journee=journee,
        agency_id=session.agency_id,
        nom=f"lettre_explication_{session.compte_caisse_number}_{journee.isoformat()}",
        contexte=contexte,
        objet_lie_type=OBJET_SESSION,
        objet_lie_id=session.id,
        acteur_id=session.caissier_id,
    )
    return service.generer_apres_commit(db, demande, contexte_requete=contexte_requete)
