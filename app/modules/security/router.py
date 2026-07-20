"""Endpoints HTTP d'authentification (bloc 4). BRANCHE la logique d'auth.py, sans la réécrire.

Transport (cf. [[points-deploiement-imf]]) : l'access token part dans le corps JSON ; le
refresh token part dans un cookie httpOnly SameSite=Strict scopé sur /auth — inaccessible à
JS, donc protégé du vol par XSS, et couvert contre le CSRF par SameSite=Strict. Exige un
déploiement même domaine (front + API derrière un reverse proxy).

IP : lue via request.client.host, JAMAIS via X-Forwarded-For applicatif (falsifiable). C'est
la couche ASGI de confiance (uvicorn --forwarded-allow-ips) qui doit réécrire request.client
en vraie IP client — sans quoi l'IP auditée est celle du proxy.

Rate limiting par IP : différé (juste après ce bloc). Les endpoints sont conçus pour qu'une
dépendance FastAPI de rate-limit s'y greffe sans les réécrire.
"""

import ipaddress
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.modules.audit.service import ContexteRequete
from app.modules.security.auth import (
    MESSAGE_ECHEC_GENERIQUE,
    MESSAGE_REFRESH_REFUSE,
    CauseEchec,
    EchecAuthentificationError,
    RafraichissementError,
    authentifier,
    deconnecter,
    deconnecter_tout,
    rafraichir,
)
from app.modules.security.autorisation import (
    MESSAGE_NON_AUTHENTIFIE,
    UtilisateurCourant,
    exige_authentification,
)
from app.modules.security.jwt import DUREE_ACCES, DUREE_RAFRAICHISSEMENT
from app.modules.security.models import User
from app.modules.security.mots_de_passe import (
    MESSAGE_MOT_DE_PASSE_ACTUEL_INVALIDE,
    PROFONDEUR_HISTORIQUE,
    MotDePasseActuelInvalideError,
    MotDePasseDejaUtiliseError,
    MotDePasseInvalideError,
    changer_son_mot_de_passe,
)
from app.modules.security.schemas import ChangePasswordRequest, LoginRequest, TokenResponse

router = APIRouter(prefix="/auth", tags=["auth"])

# Cookie du refresh token. Path=/auth : envoyé à /auth/refresh, /auth/logout et
# /auth/logout-all (qui en ont besoin), jamais aux endpoints métier (qui restent en Bearer).
COOKIE_REFRESH = "refresh_token"
COOKIE_PATH = "/auth"
EXPIRES_IN_ACCESS = int(DUREE_ACCES.total_seconds())
MAX_AGE_REFRESH = int(DUREE_RAFRAICHISSEMENT.total_seconds())


def _cookie_secure() -> bool:
    """Secure dérivé de ENV : http en dev (TestClient, poste local), https partout ailleurs.

    Fail-secure : seul le littéral « dev » désactive Secure ; tout autre ENV (prod inclus)
    force le cookie à ne partir que sur https.
    """
    return settings.ENV != "dev"


def _ip_valide(hote: str | None) -> str | None:
    """Ne garde que ce qui est une vraie IP ; sinon None (la colonne INET est nullable).

    request.client.host est normalement une IP (peuplée par la couche de confiance ASGI),
    mais peut être un nom non-IP dans certains contextes (TestClient met « testclient », un
    proxy mal configuré pourrait mettre un hostname). Passer une chaîne non-IP à la colonne
    INET ferait échouer l'INSERT d'audit, donc le login — une donnée d'origine douteuse ne
    doit jamais faire tomber l'authentification. On la neutralise en None.
    """
    if hote is None:
        return None
    try:
        ipaddress.ip_address(hote)
    except ValueError:
        return None
    return hote


def _contexte(request: Request) -> ContexteRequete:
    """Construit le contexte d'audit à partir de la requête HTTP.

    ip : request.client.host validé, peuplé par la couche de confiance ASGI (voir en-tête
    du module). On ne lit JAMAIS X-Forwarded-For ici. request_id : identifiant de
    corrélation tiré à chaque requête (traçabilité de bout en bout dans l'audit).
    """
    hote = request.client.host if request.client is not None else None
    return ContexteRequete(
        ip=_ip_valide(hote),
        user_agent=request.headers.get("user-agent"),
        request_id=uuid.uuid4(),
    )


def _poser_cookie_refresh(response: Response, refresh_token: str) -> None:
    response.set_cookie(
        key=COOKIE_REFRESH,
        value=refresh_token,
        max_age=MAX_AGE_REFRESH,
        path=COOKIE_PATH,
        httponly=True,
        samesite="strict",
        secure=_cookie_secure(),
    )


def _effacer_cookie_refresh(response: Response) -> None:
    # Mêmes attributs que la pose : sinon certains navigateurs n'associent pas la
    # suppression au bon cookie.
    response.delete_cookie(
        key=COOKIE_REFRESH,
        path=COOKIE_PATH,
        httponly=True,
        samesite="strict",
        secure=_cookie_secure(),
    )


@router.post("/login", response_model=TokenResponse)
def login(
    corps: LoginRequest,
    request: Request,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
) -> TokenResponse:
    """Connexion. 200 + access token (corps) + refresh token (cookie). 401 / 423 sinon."""
    contexte = _contexte(request)
    try:
        resultat = authentifier(
            db,
            corps.identifiant,
            corps.mot_de_passe.get_secret_value(),
            agence_demandee=corps.agence_demandee,
            ip=contexte.ip,
            user_agent=contexte.user_agent,
            request_id=contexte.request_id,
        )
    except EchecAuthentificationError as echec:
        # 423 UNIQUEMENT si le verrou concerne le vrai titulaire (mot de passe correct) :
        # verrou_jusqua n'est renseigné que dans ce cas. Tout le reste → 401 générique,
        # message identique, pour ne pas révéler l'existence ou l'état d'un compte.
        if echec.cause == CauseEchec.COMPTE_VERROUILLE and echec.verrou_jusqua is not None:
            raise HTTPException(
                status_code=status.HTTP_423_LOCKED,
                detail={
                    "message": "Compte verrouillé.",
                    "verrou_jusqua": echec.verrou_jusqua.isoformat(),
                },
            ) from None
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=MESSAGE_ECHEC_GENERIQUE
        ) from None

    _poser_cookie_refresh(response, resultat.refresh_token)
    return TokenResponse(
        access_token=resultat.access_token,
        expires_in=EXPIRES_IN_ACCESS,
        must_change_password=resultat.doit_changer_mot_de_passe,
    )


@router.post("/refresh", response_model=TokenResponse)
def refresh(
    request: Request,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
) -> TokenResponse:
    """Rotation. Lit le refresh dans le cookie, renvoie un nouveau couple. 401 sinon."""
    refresh_token = request.cookies.get(COOKIE_REFRESH)
    if not refresh_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=MESSAGE_REFRESH_REFUSE)

    contexte = _contexte(request)
    try:
        resultat = rafraichir(
            db,
            refresh_token,
            ip=contexte.ip,
            user_agent=contexte.user_agent,
            request_id=contexte.request_id,
        )
    except RafraichissementError:
        # Message générique : ne jamais dire dehors « vol détecté ». Le cookie éventé
        # reste tel quel côté client ; la session est de toute façon révoquée en base.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=MESSAGE_REFRESH_REFUSE
        ) from None

    _poser_cookie_refresh(response, resultat.refresh_token)
    return TokenResponse(
        access_token=resultat.access_token,
        expires_in=EXPIRES_IN_ACCESS,
        must_change_password=resultat.doit_changer_mot_de_passe,
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, response: Response, db: Annotated[Session, Depends(get_db)]) -> None:
    """Déconnexion simple : révoque la session courante et efface le cookie. Toujours 204.

    Idempotent : sans cookie, ou avec un token périmé, on efface le cookie et on renvoie 204
    quand même — la déconnexion ne doit rien révéler ni jamais échouer.
    """
    refresh_token = request.cookies.get(COOKIE_REFRESH)
    if refresh_token:
        deconnecter(db, refresh_token)
    _effacer_cookie_refresh(response)


@router.post("/logout-all", status_code=status.HTTP_204_NO_CONTENT)
def logout_all(
    request: Request, response: Response, db: Annotated[Session, Depends(get_db)]
) -> None:
    """Déconnexion totale : révoque TOUTES les sessions de l'utilisateur (tous appareils)."""
    refresh_token = request.cookies.get(COOKIE_REFRESH)
    if refresh_token:
        deconnecter_tout(db, refresh_token)
    _effacer_cookie_refresh(response)


@router.post("/change-password", status_code=status.HTTP_204_NO_CONTENT)
def change_password(
    corps: ChangePasswordRequest,
    courant: Annotated[UtilisateurCourant, Depends(exige_authentification())],
    db: Annotated[Session, Depends(get_db)],
) -> None:
    """Changement self-service. La SEULE porte par laquelle must_change_password se lève.

    Protégée par exige_authentification et NON par exige(...) : d'une part tout utilisateur
    doit pouvoir changer son mot de passe quel que soit son rôle, d'autre part exige()
    refuse toute action tant que le renouvellement est dû — l'utiliser ici enfermerait le
    compte dehors, incapable de faire ce qu'on exige justement de lui.

    204 sans corps : rien à renvoyer, et surtout aucun jeton réémis ici. Les jetons courants
    portent encore must_change_password=true ; le client doit se reconnecter (ou
    rafraîchir), ce qui produit des jetons propres. C'est plus sûr que de réémettre : la
    réémission silencieuse masquerait au client qu'il détenait un jeton restreint.

    409 si le nouveau mot de passe a déjà servi (C12), 422 s'il viole la politique, 400 si
    l'ancien est faux — trois causes distinctes, toutes actionnables par le titulaire, donc
    sans intérêt à être confondues.
    """
    user = db.get(User, courant.user_id)
    if user is None:  # jeton valide dont le compte a disparu entre-temps
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=MESSAGE_NON_AUTHENTIFIE
        )
    try:
        changer_son_mot_de_passe(
            db,
            user,
            corps.mot_de_passe_actuel.get_secret_value(),
            corps.nouveau_mot_de_passe.get_secret_value(),
        )
    except MotDePasseActuelInvalideError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=MESSAGE_MOT_DE_PASSE_ACTUEL_INVALIDE,
        ) from None
    except MotDePasseDejaUtiliseError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Ce mot de passe a déjà été utilisé (les {PROFONDEUR_HISTORIQUE} derniers "
            "sont refusés).",
        ) from None
    except MotDePasseInvalideError as erreur:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "message": "Mot de passe non conforme à la politique.",
                "violations": [regle.value for regle in erreur.violations],
            },
        ) from None
    db.commit()
