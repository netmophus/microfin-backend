"""Écritures sur les utilisateurs (bloc 4c) — création, modification, état, mot de passe.

Séparé de utilisateurs.py (lecture) parce que les invariants n'ont rien à voir : lire, c'est
filtrer ; écrire, c'est verrouiller, contrôler, auditer. Mais le PÉRIMÈTRE, lui, est
strictement le même — importé de la lecture, jamais réécrit.

LES CINQ RÈGLES DE CE FICHIER

1. ON N'ÉCRIT PAS SUR CE QU'ON NE VOIT PAS. Toute cible est chargée par _charger_cible, qui
   applique condition_visibilite de la LECTURE. Hors périmètre -> None -> 404, jamais 403 :
   sinon un responsable saurait qu'un compte existe ailleurs, et pourrait cartographier les
   autres agences en sondant des identifiants.

2. LA CIBLE N'EST PAS MOI, sur les actes qui pourraient servir à se soustraire à un
   contrôle : se désactiver, se supprimer, se déverrouiller, réinitialiser son propre mot de
   passe. Contrôlé DANS LE SERVICE (§6) et non à la route : une future commande CLI ou un
   job passeraient à côté d'un contrôle posé sur l'API.

3. VERROU SUR LA CIBLE. FOR UPDATE avant toute modification d'état : deux administrateurs
   qui agissent en même temps sur le même compte doivent se sérialiser, sinon l'un écrase
   l'autre et l'audit raconte une histoire fausse.

4. RÉVOQUER CE QU'ON FERME. Désactiver, supprimer ou réinitialiser un mot de passe révoque
   toutes les sessions. Sans cela, la brique d'autorisation ne relisant pas l'état du
   compte, un compte fermé garderait jusqu'à 8 h d'accès par son refresh token : la
   désactivation ne désactiverait rien d'utile.

5. AUDITER EN DERNIER. L'audit part juste avant le commit (verrou consultatif du chaînage),
   avec l'ACTEUR en user_id et la CIBLE en resource_id, et sans le moindre secret.

CE QUI N'EST PAS ICI : rôles et agences habilitées (roles.assign, users.manage_agencies) et
réinitialisation 2FA — bloc 4d.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.audit.service import ContexteRequete, ecrire_audit
from app.modules.security.auth import revoquer_les_sessions
from app.modules.security.autorisation import UtilisateurCourant
from app.modules.security.models import User
from app.modules.security.mots_de_passe import (
    ResultatGeneration,
    ecrire_mot_de_passe,
    generer_mot_de_passe,
)
from app.modules.security.utilisateurs import condition_visibilite

RESSOURCE = "user"


class ActionUtilisateur:
    """Actions d'audit du périmètre utilisateurs. Format module.action, comme l'auth."""

    CREATED = "user.created"
    UPDATED = "user.updated"
    DEACTIVATED = "user.deactivated"
    ACTIVATED = "user.activated"
    DELETED = "user.deleted"
    UNLOCKED = "user.unlocked"
    PASSWORD_RESET = "user.password_reset"


# --- erreurs ---------------------------------------------------------------------------


class CibleIntrouvableError(Exception):
    """La cible n'existe pas, est supprimée, OU est hors périmètre. Indistinctement -> 404."""


class ActionSurSoiMemeError(Exception):
    """Un utilisateur tente sur son propre compte un acte qui lui permettrait d'échapper à
    un contrôle. 403 : il sait déjà qu'il existe, il n'y a rien à lui cacher."""


class PorteeReseauRequiseError(Exception):
    """Acte réservé à qui détient perimetre.reseau (suppression, mutation d'agence)."""


class AgenceHorsPerimetreError(Exception):
    """L'agence demandée pour le compte n'est pas dans le périmètre de l'acteur."""


class IdentifiantDejaUtiliseError(Exception):
    """matricule / email / username déjà porté par un compte VIVANT.

    Le champ en collision est nommé : l'unicité est institutionnelle, tous les employés
    sont collègues, et un message générique rendrait l'outil inutilisable. La fuite —
    apprendre qu'un identifiant existe quelque part dans l'IMF — est assumée (4c).
    """

    def __init__(self, champ: str) -> None:
        super().__init__(f"Identifiant déjà utilisé : {champ}.")
        self.champ = champ


# --- helpers ----------------------------------------------------------------------------


def _charger_cible(db: Session, courant: UtilisateurCourant, user_id: uuid.UUID) -> User:
    """Charge la cible SOUS VERROU, dans le périmètre de l'acteur. Sinon CibleIntrouvable.

    with_for_update() : deux administrateurs qui agissent simultanément sur le même compte
    se sérialisent. Sans lui, deux modifications concurrentes se recouvriraient et l'audit
    conserverait un old_values qui n'a jamais été l'état réel.
    """
    cible = db.execute(
        select(User).where(User.id == user_id, condition_visibilite(courant)).with_for_update()
    ).scalar_one_or_none()
    if cible is None:
        raise CibleIntrouvableError()
    return cible


def _interdire_sur_soi_meme(courant: UtilisateurCourant, cible: User) -> None:
    if cible.id == courant.user_id:
        raise ActionSurSoiMemeError()


def _exiger_portee_reseau(courant: UtilisateurCourant) -> None:
    if not courant.voit_tout:
        raise PorteeReseauRequiseError()


def _verifier_agence_dans_le_perimetre(
    db: Session, courant: UtilisateurCourant, agence_id: uuid.UUID | None
) -> None:
    """L'agence de rattachement choisie doit être dans le périmètre de l'ACTEUR.

    LE PIÈGE DE LA CRÉATION. Sans ce contrôle, un responsable d'agence créerait un compte
    rattaché ailleurs — et en perdrait la main à la seconde même, puisque le compte
    sortirait aussitôt de son périmètre de lecture. Compte orphelin, invisible de son
    créateur, que seul un porteur de la portée réseau pourrait rattraper.

    Rattacher à AUCUNE agence est un cas de portée réseau : un compte sans agence échappe
    au cloisonnement de tout responsable.
    """
    if courant.voit_tout:
        return
    if agence_id is None:
        raise AgenceHorsPerimetreError()
    # Réutilise la définition « relève de l'agence X » de la lecture, appliquée à l'acteur.
    if courant.agency_id != agence_id:
        raise AgenceHorsPerimetreError()


def _verifier_identifiants_libres(
    db: Session,
    *,
    matricule: str,
    email: str,
    username: str,
    sauf_id: uuid.UUID | None = None,
) -> None:
    """Contrôle applicatif AVANT l'INSERT, pour un message utile plutôt qu'un 500.

    L'index partiel de la 0006 reste l'autorité — deux créations concurrentes peuvent
    passer ici puis se heurter en base. Ce contrôle sert le message, pas la garantie ;
    l'appelant doit donc aussi traiter l'IntegrityError.

    deleted_at IS NULL est essentiel : sans lui, un compte SUPPRIMÉ ferait refuser à tort
    une création, alors que la 0006 a justement libéré son identifiant.
    """
    for champ, valeur in (("matricule", matricule), ("email", email), ("username", username)):
        requete = select(User.id).where(getattr(User, champ) == valeur, User.deleted_at.is_(None))
        if sauf_id is not None:
            requete = requete.where(User.id != sauf_id)
        if db.execute(requete).first() is not None:
            raise IdentifiantDejaUtiliseError(champ)


def _etat_auditable(user: User) -> dict[str, Any]:
    """Photographie auditable d'un compte. AUCUN hash, AUCUN secret — que des faits."""
    return {
        "matricule": user.matricule,
        "email": user.email,
        "username": user.username,
        "last_name": user.last_name,
        "first_name": user.first_name,
        "phone": user.phone,
        "primary_agency_id": str(user.primary_agency_id) if user.primary_agency_id else None,
        "is_active": user.is_active,
    }


# --- création ----------------------------------------------------------------------------


@dataclass(frozen=True)
class NouvelUtilisateur:
    matricule: str
    email: str
    username: str
    last_name: str
    first_name: str
    primary_agency_id: uuid.UUID | None
    phone: str | None = None


@dataclass(frozen=True)
class ResultatCreation:
    """Le compte créé et son mot de passe provisoire EN CLAIR.

    `mot_de_passe_provisoire` ne doit être lu qu'une fois, pour la réponse HTTP de création.
    Il n'est écrit nulle part : ni en base (seul le hash), ni dans l'audit (le service
    refuse activement les clés sensibles), ni dans un log.
    """

    utilisateur: User
    mot_de_passe_provisoire: str


def creer(
    db: Session,
    courant: UtilisateurCourant,
    nouveau: NouvelUtilisateur,
    contexte: ContexteRequete,
) -> ResultatCreation:
    """Crée un compte avec un mot de passe PROVISOIRE généré, à renouveler à la 1re connexion.

    Le mot de passe n'est jamais choisi par l'administrateur : il connaîtrait celui de son
    employé, et pourrait agir sous son nom sans laisser de trace distinguable. Il n'est pas
    davantage envoyé par courriel — pas de service mail, et le canal n'est pas sûr. Il est
    affiché UNE FOIS au créateur, qui le transmet de vive voix ou sur papier.
    """
    _verifier_agence_dans_le_perimetre(db, courant, nouveau.primary_agency_id)
    _verifier_identifiants_libres(
        db,
        matricule=nouveau.matricule,
        email=nouveau.email,
        username=nouveau.username,
    )

    genere: ResultatGeneration = generer_mot_de_passe()
    cible = User(
        matricule=nouveau.matricule,
        email=nouveau.email,
        username=nouveau.username,
        password_hash=genere.hash,
        last_name=nouveau.last_name,
        first_name=nouveau.first_name,
        phone=nouveau.phone,
        primary_agency_id=nouveau.primary_agency_id,
        must_change_password=True,
        created_by=courant.user_id,
        updated_by=courant.user_id,
    )
    db.add(cible)
    db.flush()

    ecrire_audit(
        db,
        action=ActionUtilisateur.CREATED,
        contexte=contexte,
        acteur_id=courant.user_id,
        resource_type=RESSOURCE,
        resource_id=cible.id,
        agency_id=courant.agency_id,
        new_values=_etat_auditable(cible),
    )
    db.commit()
    return ResultatCreation(utilisateur=cible, mot_de_passe_provisoire=genere.clair)


# --- modification -------------------------------------------------------------------------


# Champs modifiables par PATCH. primary_agency_id en fait partie mais exige la portée
# réseau (cf. modifier) : une mutation d'agence déplace un compte hors du périmètre de son
# responsable, ce n'est pas une correction de fiche.
CHAMPS_MODIFIABLES = ("email", "phone", "last_name", "first_name", "primary_agency_id")


def modifier(
    db: Session,
    courant: UtilisateurCourant,
    user_id: uuid.UUID,
    modifications: dict[str, Any],
    contexte: ContexteRequete,
) -> User:
    """Modification partielle. Seuls les champs FOURNIS sont touchés.

    Modifier son propre compte reste permis ici : corriger son téléphone n'est pas se
    soustraire à un contrôle. Les actes qui le seraient (désactivation, suppression,
    déverrouillage, réinitialisation) ont leurs fonctions dédiées, qui les refusent.
    """
    cible = _charger_cible(db, courant, user_id)
    inconnus = set(modifications) - set(CHAMPS_MODIFIABLES)
    if inconnus:
        raise ValueError(f"Champs non modifiables : {sorted(inconnus)}")

    if "primary_agency_id" in modifications:
        # Une mutation sort le compte du périmètre de son responsable actuel : acte de
        # réseau, pas de correction de fiche.
        _exiger_portee_reseau(courant)

    if {"email", "matricule", "username"} & set(modifications):
        _verifier_identifiants_libres(
            db,
            matricule=modifications.get("matricule", cible.matricule),
            email=modifications.get("email", cible.email),
            username=modifications.get("username", cible.username),
            sauf_id=cible.id,
        )

    avant = _etat_auditable(cible)
    for champ, valeur in modifications.items():
        setattr(cible, champ, valeur)
    cible.updated_by = courant.user_id
    db.flush()
    apres = _etat_auditable(cible)

    # N'auditer QUE ce qui a bougé : un journal qui répète l'état complet à chaque retouche
    # devient illisible, et le lecteur ne distingue plus le changement du décor.
    changes = {champ for champ in avant if avant[champ] != apres[champ]}
    if changes:
        ecrire_audit(
            db,
            action=ActionUtilisateur.UPDATED,
            contexte=contexte,
            acteur_id=courant.user_id,
            resource_type=RESSOURCE,
            resource_id=cible.id,
            agency_id=courant.agency_id,
            old_values={champ: avant[champ] for champ in changes},
            new_values={champ: apres[champ] for champ in changes},
        )
    db.commit()
    return cible


# --- état du compte -------------------------------------------------------------------------


def desactiver(
    db: Session, courant: UtilisateurCourant, user_id: uuid.UUID, contexte: ContexteRequete
) -> User:
    """Désactive un compte ET RÉVOQUE SES SESSIONS.

    La révocation n'est pas un complément : c'est ce qui donne son effet à la désactivation.
    La brique d'autorisation ne relit pas l'état du compte, donc sans révocation le compte
    désactivé continuerait de rafraîchir ses jetons jusqu'à 8 h.
    """
    cible = _charger_cible(db, courant, user_id)
    _interdire_sur_soi_meme(courant, cible)

    cible.is_active = False
    cible.updated_by = courant.user_id
    db.flush()
    revoquees = revoquer_les_sessions(db, cible.id)

    ecrire_audit(
        db,
        action=ActionUtilisateur.DEACTIVATED,
        contexte=contexte,
        acteur_id=courant.user_id,
        resource_type=RESSOURCE,
        resource_id=cible.id,
        agency_id=courant.agency_id,
        old_values={"is_active": True},
        new_values={"is_active": False, "sessions_revoquees": revoquees},
    )
    db.commit()
    return cible


def activer(
    db: Session, courant: UtilisateurCourant, user_id: uuid.UUID, contexte: ContexteRequete
) -> User:
    """Réactive un compte. Ne restaure AUCUNE session : l'utilisateur se reconnecte."""
    cible = _charger_cible(db, courant, user_id)

    cible.is_active = True
    cible.updated_by = courant.user_id
    db.flush()

    ecrire_audit(
        db,
        action=ActionUtilisateur.ACTIVATED,
        contexte=contexte,
        acteur_id=courant.user_id,
        resource_type=RESSOURCE,
        resource_id=cible.id,
        agency_id=courant.agency_id,
        old_values={"is_active": False},
        new_values={"is_active": True},
    )
    db.commit()
    return cible


def supprimer(
    db: Session, courant: UtilisateurCourant, user_id: uuid.UUID, contexte: ContexteRequete
) -> None:
    """Suppression LOGIQUE (deleted_at) — acte lourd, réservé à la portée réseau.

    Un responsable d'agence désactive ; il ne supprime pas. La suppression fait sortir le
    compte de l'annuaire et libère ses identifiants (0006) : c'est une décision
    institutionnelle, pas un geste d'exploitation courante.

    Les sessions sont révoquées, pour la même raison qu'à la désactivation.
    """
    _exiger_portee_reseau(courant)
    cible = _charger_cible(db, courant, user_id)
    _interdire_sur_soi_meme(courant, cible)

    avant = _etat_auditable(cible)
    cible.deleted_at = datetime.now(UTC)
    cible.is_active = False
    cible.updated_by = courant.user_id
    db.flush()
    revoquees = revoquer_les_sessions(db, cible.id)

    ecrire_audit(
        db,
        action=ActionUtilisateur.DELETED,
        contexte=contexte,
        acteur_id=courant.user_id,
        resource_type=RESSOURCE,
        resource_id=cible.id,
        agency_id=courant.agency_id,
        # L'état complet AVANT : après suppression, la fiche sort de l'annuaire, et le
        # journal devient la seule trace lisible de ce qu'était ce compte.
        old_values=avant,
        new_values={"deleted": True, "sessions_revoquees": revoquees},
    )
    db.commit()


def deverrouiller(
    db: Session, courant: UtilisateurCourant, user_id: uuid.UUID, contexte: ContexteRequete
) -> User:
    """Lève le verrou C7 : is_locked, locked_until, failed_attempts.

    lockout_count n'est PAS remis à zéro : déverrouiller n'est pas absoudre l'historique.
    La progression 15/30/60/120 doit rester en place, sinon un compte pilonné serait remis
    au palier le plus doux à chaque intervention d'un administrateur complaisant ou pressé.
    C7 la réinitialise seule après 24 h sans nouvel incident.

    Interdit sur soi-même : lever son propre verrou annulerait le verrouillage progressif
    pour quiconque détient users.unlock.
    """
    cible = _charger_cible(db, courant, user_id)
    _interdire_sur_soi_meme(courant, cible)

    avant = {
        "is_locked": cible.is_locked,
        "locked_until": cible.locked_until.isoformat() if cible.locked_until else None,
        "failed_attempts": cible.failed_attempts,
    }
    cible.is_locked = False
    cible.locked_until = None
    cible.failed_attempts = 0
    cible.updated_by = courant.user_id
    db.flush()

    ecrire_audit(
        db,
        action=ActionUtilisateur.UNLOCKED,
        contexte=contexte,
        acteur_id=courant.user_id,
        resource_type=RESSOURCE,
        resource_id=cible.id,
        agency_id=courant.agency_id,
        old_values=avant,
        # lockout_count est journalisé pour montrer qu'il SURVIT au déverrouillage.
        new_values={"is_locked": False, "lockout_count": cible.lockout_count},
    )
    db.commit()
    return cible


def reinitialiser_mot_de_passe(
    db: Session, courant: UtilisateurCourant, user_id: uuid.UUID, contexte: ContexteRequete
) -> ResultatCreation:
    """Génère un mot de passe provisoire, révoque les sessions, exige le renouvellement.

    RÉVOQUER EST ESSENTIEL ICI. On réinitialise souvent parce qu'un compte est suspect ou
    qu'un mot de passe a fuité : laisser vivre les sessions existantes viderait le geste de
    tout effet, l'intrus gardant son accès par refresh token.

    Interdit sur soi-même : /auth/change-password est la voie propre pour son propre compte,
    et elle exige de prouver l'ancien mot de passe. Passer par la réinitialisation
    permettrait à un porteur de jeton volé de contourner cette preuve.
    """
    cible = _charger_cible(db, courant, user_id)
    _interdire_sur_soi_meme(courant, cible)

    genere = generer_mot_de_passe()
    ecrire_mot_de_passe(db, cible, genere.hash, doit_changer=True)
    cible.updated_by = courant.user_id
    db.flush()
    revoquees = revoquer_les_sessions(db, cible.id)

    ecrire_audit(
        db,
        action=ActionUtilisateur.PASSWORD_RESET,
        contexte=contexte,
        acteur_id=courant.user_id,
        resource_type=RESSOURCE,
        resource_id=cible.id,
        agency_id=courant.agency_id,
        # Le fait est journalisé, jamais la valeur — ni claire, ni hachée.
        new_values={"must_change_password": True, "sessions_revoquees": revoquees},
    )
    db.commit()
    return ResultatCreation(utilisateur=cible, mot_de_passe_provisoire=genere.clair)
