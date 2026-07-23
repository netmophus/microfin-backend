"""Création des tiers (T1c) — les 3 types, la double traçabilité, le cloisonnement en écriture.

CINQ RÈGLES, dans l'esprit du service d'écriture des utilisateurs (4c) :

1. LE NUMÉRO EST ALLOUÉ DANS LA TRANSACTION DE CRÉATION. `prochain_numero` s'exécute ici,
   sous le verrou de ligne du NumberingService (T1b). Si la création échoue, l'incrément est
   annulé avec elle : pas de trou dans la numérotation.

2. DOUBLE TRACE. Un `lifecycle_event` 'created' écrit DANS la transaction (métier), puis
   `ecrire_audit()` EN DERNIER, juste avant le commit (D5) — le trigger de chaînage prend un
   verrou consultatif, écrire l'audit tôt inverserait l'ordre des verrous.

3. LE CLOISONNEMENT MORD AUSSI À L'ÉCRITURE. Un utilisateur cloisonné ne peut créer que dans
   SON agence (celle du claim), JAMAIS dans une autre — même en forçant primary_agency_id
   dans la requête : `_resoudre_agence` dérive l'agence du claim et refuse tout forçage. Sans
   cela, un chargé de clientèle créerait une fiche ailleurs et en perdrait la main aussitôt
   (elle sortirait de son périmètre de lecture). Une portée réseau, elle, DOIT préciser
   l'agence : « toutes les agences » n'est pas une agence de rattachement.

4. NAISSANCE EN 'prospect'. Le statut par défaut de la table ; l'activation (statut 'actif')
   viendra avec le KYC (T3). Aucune opération transactionnelle n'est possible sur un prospect.

5. AUCUN SECRET DANS L'AUDIT. Les fiches tiers n'en portent pas ; l'état auditable ne contient
   que des faits (numéro, type, statut, agence, nom d'affichage).
"""

import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.modules.audit.service import ContexteRequete, ecrire_audit
from app.modules.security.autorisation import UtilisateurCourant
from app.modules.tiers.models import (
    Contact,
    GroupProfile,
    IndividualProfile,
    LegalEntityProfile,
    LifecycleEvent,
    Tier,
)
from app.modules.tiers.numbering import prefixe_pour_type, prochain_numero
from app.modules.tiers.schemas import (
    CreationGroupement,
    CreationIndividu,
    CreationPersonneMorale,
)
from app.modules.tiers.telephone import TelephoneInvalideError, normaliser

RESSOURCE = "tier"
EVENEMENT_CREATED = "created"


class ActionTier:
    """Actions d'audit du module Tiers. Format module.action, comme l'auth et les users."""

    CREATED = "tier.created"


# --- erreurs ---------------------------------------------------------------------------


class AgenceHorsPerimetreError(Exception):
    """Un cloisonné a visé une agence qui n'est pas la sienne (forçage du champ). -> 422."""


class AgenceRequiseError(Exception):
    """Une portée réseau n'a pas précisé l'agence de rattachement. -> 422."""


# --- agence de rattachement ------------------------------------------------------------


def _resoudre_agence(courant: UtilisateurCourant, agence_demandee: uuid.UUID | None) -> uuid.UUID:
    """Détermine l'agence de la fiche, en faisant mordre le cloisonnement à l'écriture.

    - portée réseau : peut viser n'importe quelle agence, mais DOIT en désigner une ;
    - cloisonné : rattaché à SON agence. Un champ qui désigne une AUTRE agence est un forçage,
      refusé — jamais silencieusement contourné pour attacher à la bonne, pour que l'appelant
      apprenne que sa requête était invalide.
    """
    if courant.voit_tout:
        if agence_demandee is None:
            raise AgenceRequiseError()
        return agence_demandee
    if agence_demandee is not None and agence_demandee != courant.agency_id:
        raise AgenceHorsPerimetreError()
    if courant.agency_id is None:
        # Cloisonné sans agence courante : il ne voit rien, il ne peut donc rien créer.
        raise AgenceHorsPerimetreError()
    return courant.agency_id


def _etat_auditable(tier: Tier, nom_affichage: str) -> dict[str, Any]:
    """Photographie auditable d'une fiche. Que des faits, aucun secret."""
    return {
        "tier_number": tier.tier_number,
        "tier_type": tier.tier_type,
        "status": tier.status,
        "primary_agency_id": str(tier.primary_agency_id),
        "nom": nom_affichage,
    }


def _telephone_initial(
    db: Session, courant: UtilisateurCourant, tier: Tier, brut: str | None
) -> None:
    """Le téléphone saisi à la création devient un CONTACT téléphone principal (T2b) — plus jamais
    tier.primary_phone. Normalisation best-effort par forçage : à la création, un numéro imparfait
    ne doit pas bloquer l'ouverture de la fiche (l'agent le corrigera dans l'onglet coordonnées).
    Un numéro inexploitable (charabia) est simplement ignoré, sans échec."""
    if brut is None or not brut.strip():
        return
    try:
        resultat = normaliser(brut, forcer=True)
    except TelephoneInvalideError:
        return
    db.add(
        Contact(
            tier_id=tier.id,
            contact_type="phone",
            contact_subtype="mobile",
            phone_raw=brut.strip(),
            phone_number=resultat.e164,
            phone_country_code=resultat.country_code,
            phone_normalized=resultat.normalise,
            is_primary=True,
            created_by=courant.user_id,
            updated_by=courant.user_id,
        )
    )
    db.flush()


def _finaliser(
    db: Session,
    courant: UtilisateurCourant,
    tier: Tier,
    nom: str,
    contexte: ContexteRequete,
    telephone: str | None = None,
) -> Tier:
    """Insère la fiche, écrit l'événement de cycle de vie DANS la transaction, audite en dernier."""
    db.add(tier)
    db.flush()  # obtient tier.id et pose tier_type via le discriminateur polymorphe

    _telephone_initial(db, courant, tier, telephone)

    db.add(
        LifecycleEvent(
            tier_id=tier.id,
            event_type=EVENEMENT_CREATED,
            previous_status=None,
            new_status=tier.status,
            performed_by=courant.user_id,
        )
    )
    db.flush()  # l'écriture MÉTIER est complète avant l'audit (D5)

    ecrire_audit(
        db,
        action=ActionTier.CREATED,
        contexte=contexte,
        acteur_id=courant.user_id,
        resource_type=RESSOURCE,
        resource_id=tier.id,
        agency_id=courant.agency_id,
        new_values=_etat_auditable(tier, nom),
    )
    db.commit()
    return tier


# --- création par type -----------------------------------------------------------------


def creer_individu(
    db: Session, courant: UtilisateurCourant, corps: CreationIndividu, contexte: ContexteRequete
) -> Tier:
    agence = _resoudre_agence(courant, corps.primary_agency_id)
    tier = IndividualProfile(
        **corps.model_dump(exclude={"primary_agency_id", "primary_phone"}),
        tier_number=prochain_numero(db, prefixe_pour_type("individual")),
        primary_agency_id=agence,
        created_by=courant.user_id,
        updated_by=courant.user_id,
    )
    return _finaliser(
        db, courant, tier, f"{corps.last_name} {corps.first_name}", contexte, corps.primary_phone
    )


def creer_personne_morale(
    db: Session,
    courant: UtilisateurCourant,
    corps: CreationPersonneMorale,
    contexte: ContexteRequete,
) -> Tier:
    agence = _resoudre_agence(courant, corps.primary_agency_id)
    tier = LegalEntityProfile(
        **corps.model_dump(exclude={"primary_agency_id", "primary_phone"}),
        tier_number=prochain_numero(db, prefixe_pour_type("legal_entity")),
        primary_agency_id=agence,
        created_by=courant.user_id,
        updated_by=courant.user_id,
    )
    return _finaliser(db, courant, tier, corps.legal_name, contexte, corps.primary_phone)


def creer_groupement(
    db: Session, courant: UtilisateurCourant, corps: CreationGroupement, contexte: ContexteRequete
) -> Tier:
    agence = _resoudre_agence(courant, corps.primary_agency_id)
    tier = GroupProfile(
        **corps.model_dump(exclude={"primary_agency_id", "primary_phone"}),
        tier_number=prochain_numero(db, prefixe_pour_type("group")),
        primary_agency_id=agence,
        created_by=courant.user_id,
        updated_by=courant.user_id,
    )
    return _finaliser(db, courant, tier, corps.group_name, contexte, corps.primary_phone)
