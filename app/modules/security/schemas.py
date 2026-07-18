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
