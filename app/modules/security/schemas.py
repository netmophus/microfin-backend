"""Schémas Pydantic d'entrée/sortie de l'API d'authentification (bloc 4).

RÈGLE DE SÉCURITÉ ABSOLUE : aucun schéma de SORTIE ne dérive d'un modèle ORM ni ne dump
« tout l'objet ». Chaque champ qui sort est listé explicitement ici. Ainsi password_hash,
refresh_token_hash, secret_encrypted — et le refresh token lui-même — ne peuvent PAS fuiter
par accident : ils ne figurent nulle part dans un schéma de sortie.

TRANSPORT (décidé bloc 4, cf. [[points-deploiement-imf]]) : l'access token part dans le
CORPS (le SPA le garde en mémoire et l'envoie en Authorization: Bearer) ; le refresh token
NE FIGURE JAMAIS dans le corps — il est livré dans un cookie httpOnly SameSite=Strict par le
routeur. C'est pourquoi TokenResponse ne porte pas de refresh_token.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field, SecretStr


class LoginRequest(BaseModel):
    """Entrée de POST /auth/login."""

    identifiant: str = Field(min_length=1, description="username OU email")
    # SecretStr : le mot de passe ne s'imprime pas (repr masqué). Il ne sera jamais
    # journalisé si une requête ou une exception de validation est loggée.
    mot_de_passe: SecretStr
    # C6 — agence courante voulue. Omise → l'agence de rattachement. Le service refuse
    # (et audite) si elle n'est pas habilitée.
    agence_demandee: uuid.UUID | None = None


class TokenResponse(BaseModel):
    """Sortie de /auth/login et /auth/refresh. N'expose QUE l'access token.

    Le refresh token n'est PAS ici : il est délivré par cookie httpOnly. token_type et
    expires_in permettent au client de piloter le Bearer et son renouvellement.
    """

    access_token: str
    token_type: str = "bearer"
    # Durée de validité de l'access token, en secondes (15 min).
    expires_in: int


# --- annuaire des utilisateurs (bloc 4b) --------------------------------------------
#
# Même règle absolue qu'en haut de fichier, et elle porte tout son poids ici : la table
# security.users contient password_hash, failed_attempts, lockout_count et
# last_login_ip. AUCUN de ces champs n'est listé ci-dessous, donc aucun ne peut sortir,
# même si un jour quelqu'un passait un objet ORM à ces schémas. La sécurité ne repose pas
# sur la vigilance de l'appelant : elle repose sur le fait que le champ n'existe pas ici.


class AgenceBreve(BaseModel):
    """Agence réduite à ce qu'un écran d'annuaire affiche."""

    id: uuid.UUID
    code: str
    name: str


class RoleBref(BaseModel):
    """Rôle : le code pour la logique, le libellé pour l'humain."""

    code: str
    name: str


class UtilisateurListeItem(BaseModel):
    """Une ligne de tableau. Volontairement PLUS PAUVRE que la fiche.

    Pas de rôles ici : la liste ne les affiche pas, et les charger coûterait une requête
    par ligne. On ne paie pas ce qu'on n'affiche pas. Filtrer PAR rôle reste possible
    (paramètre role), ce qui est une autre question que les exposer.

    Pas de téléphone non plus : une donnée personnelle n'a pas à voyager dans un listing
    quand elle n'est utile que sur la fiche.
    """

    id: uuid.UUID
    matricule: str
    username: str
    email: str
    last_name: str
    first_name: str
    agence: AgenceBreve | None
    is_active: bool
    is_locked: bool


class UtilisateurFiche(BaseModel):
    """Fiche détaillée. Tout ce qui sort est listé ici, un champ à la fois.

    Les SESSIONS actives n'y figurent pas : elles relèvent de sessions.read, une permission
    distincte de users.read. Les mêler ferait fuiter, à qui ne détient que users.read, des
    données qu'une autre permission est censée garder — et rendrait la matrice mensongère.
    """

    id: uuid.UUID
    matricule: str
    username: str
    email: str
    phone: str | None
    last_name: str
    first_name: str
    agence_principale: AgenceBreve | None
    # Habilitations réseau (C6) : les agences où cet utilisateur peut travailler, en plus
    # de son rattachement.
    agences_habilitees: list[AgenceBreve]
    roles: list[RoleBref]
    is_active: bool
    is_locked: bool
    locked_until: datetime | None
    must_change_password: bool
    created_at: datetime
    updated_at: datetime


class PageUtilisateurs(BaseModel):
    """Page d'annuaire. total permet au front d'afficher un compteur et de sauter aux pages.

    total est calculé SOUS LES MÊMES conditions que les lignes, périmètre d'agence compris.
    Un total plus large que ce que l'appelant peut voir serait une fuite : il révélerait
    l'effectif des autres agences sans en montrer une seule ligne.
    """

    lignes: list[UtilisateurListeItem]
    total: int
    page: int
    taille: int
