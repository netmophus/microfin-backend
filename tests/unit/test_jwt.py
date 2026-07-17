"""Vérifie la fabrication et la vérification des jetons (§6).

Unitaires : aucune base. Aucun secret en dur — la clé vient de settings, chargée du .env.

Les jetons forgés par ces tests (expirés, mal signés, sans claim) sont fabriqués avec
PyJWT directement : passer par le module reviendrait à lui demander de produire ce qu'il
refuse justement de produire.
"""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt as pyjwt
import pytest
from pydantic import ValidationError

from app.core.config import settings
from app.modules.security.jwt import (
    DUREE_ACCES,
    DUREE_RAFRAICHISSEMENT,
    ClaimsAcces,
    JetonExpireError,
    JetonInvalideError,
    TypeDeJetonInvalideError,
    TypeJeton,
    creer_access_token,
    creer_refresh_token,
    decoder_access_token,
    decoder_refresh_token,
)


@pytest.fixture
def user_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def agence_principale() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def forger() -> Callable[..., str]:
    """Signe une charge arbitraire avec la vraie clé : signature valide, contenu choisi.

    Sert à produire ce que le module ne produirait jamais — jeton expiré, claim manquant,
    type inconnu — pour vérifier que le décodage les refuse.
    """

    def _forger(**charge: Any) -> str:
        return pyjwt.encode(
            charge, settings.JWT_SECRET.get_secret_value(), algorithm=settings.JWT_ALGORITHM
        )

    return _forger


# --- access token -------------------------------------------------------------------


def test_un_access_token_se_decode_et_porte_tous_les_claims(
    user_id: uuid.UUID, agence_principale: uuid.UUID
) -> None:
    agence_courante = uuid.uuid4()
    jeton = creer_access_token(
        user_id,
        ["caissier", "chargé_clientèle"],
        primary_agency_id=agence_principale,
        agency_id=agence_courante,
    )
    claims = decoder_access_token(jeton)

    assert claims.sub == user_id
    assert claims.roles == ("caissier", "chargé_clientèle")
    assert claims.primary_agency_id == agence_principale
    assert claims.agency_id == agence_courante
    assert claims.type == TypeJeton.ACCES
    assert isinstance(claims.jti, uuid.UUID)


def test_lagence_courante_retombe_sur_lagence_principale(
    user_id: uuid.UUID, agence_principale: uuid.UUID
) -> None:
    # C6 — le mono-agence est le cas courant : rien à préciser, agency_id suit.
    claims = decoder_access_token(
        creer_access_token(user_id, ["comptable"], primary_agency_id=agence_principale)
    )
    assert claims.agency_id == agence_principale


def test_lagence_courante_peut_differer_de_la_principale(
    user_id: uuid.UUID, agence_principale: uuid.UUID
) -> None:
    # C6 — un agent multi-agences travaille ailleurs que dans son agence de rattachement.
    ailleurs = uuid.uuid4()
    claims = decoder_access_token(
        creer_access_token(
            user_id, ["responsable_agence"], primary_agency_id=agence_principale, agency_id=ailleurs
        )
    )
    assert claims.agency_id == ailleurs
    assert claims.primary_agency_id == agence_principale
    assert claims.agency_id != claims.primary_agency_id


def test_un_utilisateur_sans_agence_est_accepte(user_id: uuid.UUID) -> None:
    # users.primary_agency_id est nullable : un admin technique n'est rattaché à rien.
    claims = decoder_access_token(creer_access_token(user_id, ["admin_technique"]))
    assert claims.primary_agency_id is None
    assert claims.agency_id is None


def test_un_access_token_dure_quinze_minutes(user_id: uuid.UUID) -> None:
    claims = decoder_access_token(creer_access_token(user_id, []))
    assert claims.exp - claims.iat == DUREE_ACCES
    assert timedelta(minutes=15) == DUREE_ACCES


# --- refresh token ------------------------------------------------------------------


def test_un_refresh_token_se_decode(user_id: uuid.UUID) -> None:
    claims = decoder_refresh_token(creer_refresh_token(user_id))
    assert claims.sub == user_id
    assert claims.type == TypeJeton.RAFRAICHISSEMENT
    assert isinstance(claims.jti, uuid.UUID)


def test_un_refresh_token_dure_huit_heures(user_id: uuid.UUID) -> None:
    claims = decoder_refresh_token(creer_refresh_token(user_id))
    assert claims.exp - claims.iat == DUREE_RAFRAICHISSEMENT
    assert timedelta(hours=8) == DUREE_RAFRAICHISSEMENT


def test_un_refresh_token_ne_porte_ni_roles_ni_agences(user_id: uuid.UUID) -> None:
    """Un refresh vit 8 h : y figer des rôles ferait survivre une habilitation révoquée.

    On inspecte la charge brute, pas le modèle : c'est l'absence dans le JETON qui
    compte, un modèle sans champ ne prouverait rien.
    """
    charge = pyjwt.decode(
        creer_refresh_token(user_id),
        settings.JWT_SECRET.get_secret_value(),
        algorithms=[settings.JWT_ALGORITHM],
    )
    assert "roles" not in charge
    assert "agency_id" not in charge
    assert "primary_agency_id" not in charge
    assert set(charge) == {"sub", "jti", "iat", "exp", "type"}


# --- jti ----------------------------------------------------------------------------


def test_le_jti_est_unique_a_chaque_access_token(user_id: uuid.UUID) -> None:
    jtis = {decoder_access_token(creer_access_token(user_id, [])).jti for _ in range(20)}
    assert len(jtis) == 20


def test_le_jti_est_unique_a_chaque_refresh_token(user_id: uuid.UUID) -> None:
    # C'est ce jti qui identifiera la session en base : deux jetons de même jti
    # rendraient la révocation d'une session ambiguë.
    jtis = {decoder_refresh_token(creer_refresh_token(user_id)).jti for _ in range(20)}
    assert len(jtis) == 20


def test_laccess_et_le_refresh_dune_meme_session_ont_des_jti_distincts(
    user_id: uuid.UUID,
) -> None:
    acces = decoder_access_token(creer_access_token(user_id, []))
    refresh = decoder_refresh_token(creer_refresh_token(user_id))
    assert acces.jti != refresh.jti


# --- confusion des familles ---------------------------------------------------------


def test_un_refresh_presente_comme_acces_est_refuse(user_id: uuid.UUID) -> None:
    """Le piège classique : un refresh vit 8 h et ne porte aucun rôle.

    Accepté comme accès, il donnerait une session de 8 h au lieu de 15 min, autorisée
    sur une liste de rôles vide.
    """
    with pytest.raises(TypeDeJetonInvalideError):
        decoder_access_token(creer_refresh_token(user_id))


def test_un_acces_presente_comme_refresh_est_refuse(user_id: uuid.UUID) -> None:
    with pytest.raises(TypeDeJetonInvalideError):
        decoder_refresh_token(creer_access_token(user_id, ["caissier"]))


def test_un_type_inconnu_est_refuse(user_id: uuid.UUID, forger: Callable[..., str]) -> None:
    maintenant = datetime.now(UTC)
    jeton = forger(
        sub=str(user_id),
        jti=str(uuid.uuid4()),
        iat=int(maintenant.timestamp()),
        exp=int((maintenant + DUREE_ACCES).timestamp()),
        type="admin",
    )
    with pytest.raises(TypeDeJetonInvalideError):
        decoder_access_token(jeton)


# --- expiration ---------------------------------------------------------------------


def test_un_access_token_expire_est_refuse(user_id: uuid.UUID, forger: Callable[..., str]) -> None:
    passe = datetime.now(UTC) - timedelta(hours=1)
    jeton = forger(
        sub=str(user_id),
        jti=str(uuid.uuid4()),
        iat=int((passe - DUREE_ACCES).timestamp()),
        exp=int(passe.timestamp()),
        type="access",
        roles=[],
        primary_agency_id=None,
        agency_id=None,
    )
    # Expiré, pas « invalide » : le client doit savoir qu'il lui suffit de rafraîchir.
    with pytest.raises(JetonExpireError):
        decoder_access_token(jeton)


def test_un_refresh_token_expire_est_refuse(user_id: uuid.UUID, forger: Callable[..., str]) -> None:
    passe = datetime.now(UTC) - timedelta(days=1)
    jeton = forger(
        sub=str(user_id),
        jti=str(uuid.uuid4()),
        iat=int((passe - DUREE_RAFRAICHISSEMENT).timestamp()),
        exp=int(passe.timestamp()),
        type="refresh",
    )
    with pytest.raises(JetonExpireError):
        decoder_refresh_token(jeton)


def test_expire_est_distinct_dinvalide(user_id: uuid.UUID, forger: Callable[..., str]) -> None:
    # Deux causes, deux réponses : rafraîchir dans un cas, se réauthentifier dans l'autre.
    passe = datetime.now(UTC) - timedelta(hours=1)
    expire = forger(
        sub=str(user_id),
        jti=str(uuid.uuid4()),
        iat=int((passe - DUREE_ACCES).timestamp()),
        exp=int(passe.timestamp()),
        type="access",
        roles=[],
        primary_agency_id=None,
        agency_id=None,
    )
    with pytest.raises(JetonExpireError):
        decoder_access_token(expire)
    with pytest.raises(JetonInvalideError):
        decoder_access_token(creer_access_token(user_id, []) + "x")


# --- signature ----------------------------------------------------------------------


def test_un_jeton_mal_signe_est_refuse(user_id: uuid.UUID) -> None:
    autre_cle = pyjwt.encode(
        {
            "sub": str(user_id),
            "jti": str(uuid.uuid4()),
            "iat": int(datetime.now(UTC).timestamp()),
            "exp": int((datetime.now(UTC) + DUREE_ACCES).timestamp()),
            "type": "access",
            "roles": [],
            "primary_agency_id": None,
            "agency_id": None,
        },
        "une-autre-cle-de-signature-suffisamment-longue-pour-hs256",
        algorithm="HS256",
    )
    with pytest.raises(JetonInvalideError):
        decoder_access_token(autre_cle)


def test_un_jeton_altere_est_refuse(user_id: uuid.UUID, agence_principale: uuid.UUID) -> None:
    jeton = creer_access_token(user_id, ["caissier"], primary_agency_id=agence_principale)
    entete, charge, signature = jeton.split(".")
    # Un seul caractère de la charge suffit à invalider la signature.
    altere = f"{entete}.{charge[:-2]}XY.{signature}"
    with pytest.raises(JetonInvalideError):
        decoder_access_token(altere)


def test_un_jeton_non_signe_alg_none_est_refuse(user_id: uuid.UUID) -> None:
    """L'attaque JWT canonique : alg=none, aucune signature.

    Refusée parce que decode reçoit algorithms=[HS256] depuis la configuration et
    n'interroge jamais l'en-tête du jeton.
    """
    non_signe = pyjwt.encode(
        {
            "sub": str(user_id),
            "jti": str(uuid.uuid4()),
            "iat": int(datetime.now(UTC).timestamp()),
            "exp": int((datetime.now(UTC) + DUREE_ACCES).timestamp()),
            "type": "access",
            "roles": [],
            "primary_agency_id": None,
            "agency_id": None,
        },
        key="",
        algorithm="none",
    )
    with pytest.raises(JetonInvalideError):
        decoder_access_token(non_signe)


def test_du_charabia_est_refuse() -> None:
    with pytest.raises(JetonInvalideError):
        decoder_access_token("pas.un.jeton")


# --- claims obligatoires ------------------------------------------------------------


def test_un_jeton_sans_exp_est_refuse(user_id: uuid.UUID, forger: Callable[..., str]) -> None:
    """PyJWT n'exige aucun claim par défaut : sans exp, il décode et le jeton est éternel.

    Vérifié sur la 2.13.0 — d'où options={"require": ...}. Ce test garde ce garde-fou.
    """
    sans_exp = forger(
        sub=str(user_id),
        jti=str(uuid.uuid4()),
        iat=int(datetime.now(UTC).timestamp()),
        type="access",
        roles=[],
        primary_agency_id=None,
        agency_id=None,
    )
    # PyJWT seul l'accepterait volontiers : c'est bien notre garde-fou qui refuse.
    assert pyjwt.decode(
        sans_exp, settings.JWT_SECRET.get_secret_value(), algorithms=[settings.JWT_ALGORITHM]
    )
    with pytest.raises(JetonInvalideError):
        decoder_access_token(sans_exp)


def test_un_jeton_sans_jti_est_refuse(user_id: uuid.UUID, forger: Callable[..., str]) -> None:
    # Sans jti, le bloc 3 ne pourrait pas révoquer une session précise.
    maintenant = datetime.now(UTC)
    sans_jti = forger(
        sub=str(user_id),
        iat=int(maintenant.timestamp()),
        exp=int((maintenant + DUREE_ACCES).timestamp()),
        type="access",
        roles=[],
        primary_agency_id=None,
        agency_id=None,
    )
    with pytest.raises(JetonInvalideError):
        decoder_access_token(sans_jti)


def test_un_claim_inconnu_est_refuse(user_id: uuid.UUID, forger: Callable[..., str]) -> None:
    # extra="forbid" : une charge qu'on n'a pas émise n'est pas une charge qu'on accepte.
    maintenant = datetime.now(UTC)
    avec_intrus = forger(
        sub=str(user_id),
        jti=str(uuid.uuid4()),
        iat=int(maintenant.timestamp()),
        exp=int((maintenant + DUREE_ACCES).timestamp()),
        type="access",
        roles=[],
        primary_agency_id=None,
        agency_id=None,
        is_admin=True,
    )
    with pytest.raises(JetonInvalideError):
        decoder_access_token(avec_intrus)


# --- non-exposition -----------------------------------------------------------------


def test_aucune_erreur_ne_contient_le_jeton(user_id: uuid.UUID) -> None:
    """Le jeton est une crédence : le voir dans un log vaut un compte pendant 15 min."""
    jeton = creer_access_token(user_id, ["caissier"])
    with pytest.raises(TypeDeJetonInvalideError) as capture:
        decoder_refresh_token(jeton)
    assert jeton not in str(capture.value)

    with pytest.raises(JetonInvalideError) as capture_invalide:
        decoder_access_token(jeton + "x")
    assert jeton not in str(capture_invalide.value)


def test_les_claims_sont_immuables(user_id: uuid.UUID) -> None:
    # frozen : des claims vérifiés ne doivent pas pouvoir être réécrits après coup.
    claims = decoder_access_token(creer_access_token(user_id, ["caissier"]))
    with pytest.raises(ValidationError):
        claims.roles = ("admin_technique",)  # type: ignore[misc]


def test_le_secret_de_signature_nest_pas_en_dur() -> None:
    # Il vient de settings, donc du .env — et SecretStr le masque à l'impression.
    assert "SecretStr" in repr(settings.JWT_SECRET)
    assert len(settings.JWT_SECRET.get_secret_value()) >= 32


def test_lalgorithme_vient_de_la_configuration() -> None:
    # Et jamais de l'en-tête du jeton : c'est ce qui rend alg=none inopérant.
    assert settings.JWT_ALGORITHM == "HS256"


def test_un_access_token_est_bien_signe_en_hs256(user_id: uuid.UUID) -> None:
    entete = pyjwt.get_unverified_header(creer_access_token(user_id, []))
    assert entete["alg"] == "HS256"


def test_le_modele_de_claims_refuse_un_type_incoherent(user_id: uuid.UUID) -> None:
    # Literal[TypeJeton.ACCES] : le modèle lui-même ne peut pas porter un autre type.
    with pytest.raises(ValidationError):
        ClaimsAcces(
            sub=user_id,
            jti=uuid.uuid4(),
            iat=datetime.now(UTC),
            exp=datetime.now(UTC) + DUREE_ACCES,
            type=TypeJeton.RAFRAICHISSEMENT,  # type: ignore[arg-type]
            roles=(),
            primary_agency_id=None,
            agency_id=None,
        )
