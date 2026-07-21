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
    # État du COMPTE, déclaré explicitement plutôt que laissé à déduire.
    #
    # L'information existe aussi dans le claim du jeton, et un client pourrait l'y lire —
    # mais ce serait le coupler au FORMAT INTERNE du jeton. Or ce format évoluera : passage
    # à RS256, nouveaux claims, renommages. Chaque évolution devrait alors se souvenir qu'un
    # client lit dedans, et le jour où on l'oublie, le client casse en silence.
    #
    # Le jeton reste donc une boîte noire signée, et l'API déclare ce dont le client a
    # besoin. Ce champ dit au front qu'il doit conduire l'utilisateur vers l'écran de
    # renouvellement — il ne DÉCIDE rien : c'est exige() qui refuse les actions, côté
    # serveur, que le client en tienne compte ou non.
    must_change_password: bool = False


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


class ChangePasswordRequest(BaseModel):
    """Entrée de POST /auth/change-password.

    Les deux valeurs sont des SecretStr : leur repr est masqué, donc ni une exception de
    validation ni un log de requête ne peuvent les faire apparaître.

    L'ancien mot de passe est exigé bien que l'appelant soit déjà authentifié — c'est ce qui
    empêche un voleur de jeton de s'approprier définitivement le compte.
    """

    mot_de_passe_actuel: SecretStr
    nouveau_mot_de_passe: SecretStr


# --- écritures sur les utilisateurs (bloc 4c) ----------------------------------------

# Contrôle de forme DÉLIBÉRÉMENT SOMMAIRE : « quelque chose @ quelque chose . quelque
# chose », sans espace. On ne cherche pas la conformité RFC 5322 — une validation stricte
# rejette des adresses parfaitement valides (guillemets, IDN, sous-adressage), ce qui est
# pire qu'une passoire quand un agent ne peut pas saisir l'adresse réelle d'un collègue.
# La seule preuve qu'une adresse existe est un message qui arrive ; ce projet n'envoie pas
# de courriel, donc le motif attrape les fautes de frappe, rien de plus. C'est aussi ce qui
# évite d'ajouter la dépendance email-validator pour une garantie qu'elle ne donne pas.
MOTIF_EMAIL = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class CreerUtilisateurRequest(BaseModel):
    """Entrée de POST /users.

    Pas de champ « mot de passe » : il est GÉNÉRÉ. Laisser l'administrateur le choisir lui
    ferait connaître celui de son employé, donc lui permettrait d'agir sous son nom sans
    laisser de trace distinguable dans l'audit.
    """

    matricule: str = Field(min_length=1, max_length=30)
    email: str = Field(max_length=255, pattern=MOTIF_EMAIL)
    username: str = Field(min_length=3, max_length=50)
    last_name: str = Field(min_length=1, max_length=100)
    first_name: str = Field(min_length=1, max_length=100)
    phone: str | None = Field(default=None, max_length=30)
    # Doit être dans le périmètre du créateur (contrôlé par le service) : sans quoi un
    # responsable créerait un compte qu'il ne pourrait plus voir à la seconde suivante.
    primary_agency_id: uuid.UUID | None = None


class ModifierUtilisateurRequest(BaseModel):
    """Entrée de PATCH /users/{id}. Modification PARTIELLE : seuls les champs fournis bougent.

    model_fields_set distingue « absent » de « explicitement mis à null » — sans quoi
    effacer un téléphone serait impossible, ou toute requête écraserait les champs omis.

    matricule et username ne sont volontairement PAS modifiables : ce sont les clés par
    lesquelles l'audit et les écritures comptables désignent une personne. Les changer
    rendrait l'historique illisible pour un contrôleur.
    """

    email: str | None = Field(default=None, max_length=255, pattern=MOTIF_EMAIL)
    phone: str | None = Field(default=None, max_length=30)
    last_name: str | None = Field(default=None, min_length=1, max_length=100)
    first_name: str | None = Field(default=None, min_length=1, max_length=100)
    # Mutation d'agence : exige la portée réseau (contrôlé par le service).
    primary_agency_id: uuid.UUID | None = None

    def modifications(self) -> dict[str, object]:
        """Ne rend que les champs RÉELLEMENT fournis par le client."""
        return {champ: getattr(self, champ) for champ in self.model_fields_set}


class UtilisateurCreeResponse(BaseModel):
    """Sortie de POST /users et de POST /users/{id}/reset-password.

    mot_de_passe_provisoire n'apparaît QUE dans cette réponse, et une seule fois : il n'est
    stocké nulle part (seul son hash), ni journalisé, ni auditable. L'administrateur le
    transmet de vive voix ou sur papier ; l'employé le change à sa première connexion.

    C'est une chaîne nue et non un SecretStr : le masquage servirait à protéger des logs,
    or c'est précisément la valeur que cette réponse doit livrer au client.
    """

    utilisateur: UtilisateurFiche
    mot_de_passe_provisoire: str


class MonProfilResponse(BaseModel):
    """Sortie de GET /auth/me : l'identité de l'utilisateur connecté.

    Sert deux usages du frontend : afficher un nom qui SURVIT au rechargement de page (le
    jeton vit en mémoire et se perd au F5 ; ce profil se re-demande), et savoir quelles
    entrées de menu montrer — d'où la liste explicite des permissions.

    Construit champ par champ, jamais dérivé d'un modèle ORM : password_hash et les compteurs
    de sécurité de la table users ne peuvent donc pas fuiter par accident. On expose les
    PERMISSIONS (résolues depuis les rôles) et non le simple fait d'être connecté : le
    frontend en a besoin pour n'afficher que ce qui est permis. Ce n'est pas une faille — ces
    permissions sont déjà dans le jeton de l'utilisateur ; les lui rendre lisiblement ne lui
    apprend rien qu'il ne détienne. Le serveur reste seul juge (403 sur chaque route).
    """

    id: uuid.UUID
    username: str
    last_name: str
    first_name: str
    roles: list[RoleBref]
    permissions: list[str]
    # Agence COURANTE de la session (C6), None si l'utilisateur n'est rattaché à aucune.
    agence_courante: AgenceBreve | None
    # Utile au front pour rediriger vers l'écran de renouvellement dès le chargement.
    must_change_password: bool
