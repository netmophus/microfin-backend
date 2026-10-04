"""Paramétrage des ratios prudentiels RCSFD (lot P2.1.a, couche CRUD au-dessus du moteur
`app/modules/conformite/moteur.py`) — agrégats (composition incluse), ratios, seuils,
`parametre_institution` (singleton).

RÈGLE is_system (proposée et validée avec l'utilisateur, DIVERGENTE du patron users/roles où
is_system verrouille modification ET suppression) : ici `is_system` reste une ÉTIQUETTE
purement informative — « livré avec le paramétrage RCSFD officiel », utile à l'écran pour
distinguer ce socle d'un ajout local — mais elle NE BLOQUE NI la modification (composition
d'un agrégat comprise, remplacement complet) NI la suppression. Le SEUL rempart est la
RÉFÉRENCE RÉELLE : un agrégat utilisé comme numérateur OU dénominateur d'au moins un ratio ne
peut pas être supprimé — vérifié ICI (message clair, compte les ratios en conflit) ET en base
par la FK `ratio_prudentiel.agregat_*_id` sans `ondelete` (RESTRICT, dernier rempart si
l'applicatif est contourné). Un ratio n'est référencé par rien d'autre : sa suppression est
toujours permise et cascade sur ses seuils (FK ON DELETE CASCADE, migration 0057).

Les agrégats numérateur/dénominateur d'un ratio se RÉFÉRENCENT PAR CODE (identifiant métier
stable), jamais par UUID, dans les schémas d'entrée — un code inconnu lève
`moteur.AgregatIntrouvableError`, réutilisée telle quelle (même condition que la résolution
interne du moteur, pas une erreur distincte pour la même chose).
"""

import uuid
from collections.abc import Sequence
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.audit.service import CONTEXTE_VIDE, ContexteRequete, ecrire_audit
from app.modules.conformite.models import (
    AgregatCompte,
    AgregatPrudentiel,
    ParametreInstitution,
    RatioPrudentiel,
    RatioSeuil,
)
from app.modules.conformite.moteur import AgregatIntrouvableError

RESSOURCE_AGREGAT = "conformite.agregat_prudentiel"
RESSOURCE_RATIO = "conformite.ratio_prudentiel"
RESSOURCE_SEUIL = "conformite.ratio_seuil"
RESSOURCE_PARAMETRE = "conformite.parametre_institution"


class ConformiteError(Exception):
    """Base des erreurs métier de ce module — mappées en 422 par le routeur."""


class CodeAgregatDejaUtiliseError(ConformiteError):
    """Un autre agrégat utilise déjà ce code."""


class AgregatReferenceError(ConformiteError):
    """Au moins un ratio référence encore cet agrégat (numérateur ou dénominateur) —
    suppression refusée."""


class CodeRatioDejaUtiliseError(ConformiteError):
    """Un autre ratio utilise déjà ce code."""


class OrdreRatioDejaUtiliseError(ConformiteError):
    """Un autre ratio utilise déjà cet ordre d'affichage."""


class SeuilDejaDefiniError(ConformiteError):
    """Un seuil existe déjà pour ce (ratio, catégorie) — modifier celui-là plutôt qu'en créer
    un second (contrainte d'unicité, migration 0057)."""


class ParametreInstitutionIntrouvableError(ConformiteError):
    """Le singleton n'a pas encore été initialisé (le seed `seed-conformite` ne s'est jamais
    exécuté sur cette installation)."""


# --- Agrégats --------------------------------------------------------------------------------


def lister_agregats(db: Session) -> Sequence[AgregatPrudentiel]:
    return db.execute(select(AgregatPrudentiel).order_by(AgregatPrudentiel.code)).scalars().all()


def obtenir_agregat(db: Session, agregat_id: uuid.UUID) -> AgregatPrudentiel | None:
    return db.get(AgregatPrudentiel, agregat_id)


def composition_agregat(db: Session, agregat_id: uuid.UUID) -> Sequence[AgregatCompte]:
    return (
        db.execute(
            select(AgregatCompte)
            .where(AgregatCompte.agregat_id == agregat_id)
            .order_by(AgregatCompte.prefixe_compte)
        )
        .scalars()
        .all()
    )


def _verifier_code_agregat_unique(
    db: Session, *, code: str, exclure_id: uuid.UUID | None = None
) -> None:
    conflit = db.execute(
        select(AgregatPrudentiel).where(AgregatPrudentiel.code == code)
    ).scalar_one_or_none()
    if conflit is not None and conflit.id != exclure_id:
        raise CodeAgregatDejaUtiliseError(
            f"Le code « {code} » est déjà utilisé par un autre agrégat."
        )


def _remplacer_composition(
    db: Session, agregat: AgregatPrudentiel, composition: Sequence[tuple[str, int]]
) -> None:
    for ligne in composition_agregat(db, agregat.id):
        db.delete(ligne)
    db.flush()
    for prefixe, sens in composition:
        db.add(AgregatCompte(agregat_id=agregat.id, prefixe_compte=prefixe, sens=sens))


def creer_agregat(
    db: Session,
    *,
    code: str,
    libelle: str,
    reference: str | None,
    type: str,
    calcul_special: str | None,
    nets_de_provisions: bool,
    applique_complement_provisions_tutelle: bool,
    composition: Sequence[tuple[str, int]],
    motif: str,
    par: uuid.UUID | None,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> AgregatPrudentiel:
    """Ajoute UN agrégat — `is_system=False` systématiquement (réservé au seed officiel)."""
    _verifier_code_agregat_unique(db, code=code)

    agregat = AgregatPrudentiel(
        code=code,
        libelle=libelle,
        reference=reference,
        type=type,
        calcul_special=calcul_special,
        nets_de_provisions=nets_de_provisions,
        applique_complement_provisions_tutelle=applique_complement_provisions_tutelle,
        is_system=False,
        created_by=par,
        updated_by=par,
    )
    db.add(agregat)
    db.flush()
    _remplacer_composition(db, agregat, [(c[0], c[1]) for c in composition])
    db.flush()

    ecrire_audit(
        db,
        action="conformite.agregat.created",
        contexte=contexte,
        acteur_id=par,
        resource_type=RESSOURCE_AGREGAT,
        resource_id=agregat.id,
        new_values={
            "code": code,
            "libelle": libelle,
            "type": type,
            "calcul_special": calcul_special,
            "composition": list(composition),
            "motif": motif,
        },
    )
    return agregat


def modifier_agregat(
    db: Session,
    agregat: AgregatPrudentiel,
    *,
    code: str,
    libelle: str,
    reference: str | None,
    type: str,
    calcul_special: str | None,
    nets_de_provisions: bool,
    applique_complement_provisions_tutelle: bool,
    composition: Sequence[tuple[str, int]],
    motif: str,
    par: uuid.UUID | None,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> AgregatPrudentiel:
    """Remplace l'ÉTAT COMPLET de l'agrégat, composition comprise (ajout/suppression/
    modification d'une ligne = la soumettre, l'omettre, ou changer son sens dans la liste
    envoyée) — `is_system` n'empêche RIEN ici, voir le docstring de module."""
    _verifier_code_agregat_unique(db, code=code, exclure_id=agregat.id)

    avant = {
        "code": agregat.code,
        "libelle": agregat.libelle,
        "type": agregat.type,
        "calcul_special": agregat.calcul_special,
        "composition": [(c.prefixe_compte, c.sens) for c in composition_agregat(db, agregat.id)],
    }

    agregat.code = code
    agregat.libelle = libelle
    agregat.reference = reference
    agregat.type = type
    agregat.calcul_special = calcul_special
    agregat.nets_de_provisions = nets_de_provisions
    agregat.applique_complement_provisions_tutelle = applique_complement_provisions_tutelle
    agregat.updated_by = par
    _remplacer_composition(db, agregat, [(c[0], c[1]) for c in composition])
    db.flush()

    ecrire_audit(
        db,
        action="conformite.agregat.updated",
        contexte=contexte,
        acteur_id=par,
        resource_type=RESSOURCE_AGREGAT,
        resource_id=agregat.id,
        old_values=avant,
        new_values={
            "code": code,
            "libelle": libelle,
            "type": type,
            "calcul_special": calcul_special,
            "composition": list(composition),
            "motif": motif,
        },
    )
    return agregat


def supprimer_agregat(
    db: Session,
    agregat: AgregatPrudentiel,
    *,
    motif: str,
    par: uuid.UUID | None,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> None:
    """Retire UN agrégat. Refuse si au moins un ratio le référence (numérateur OU
    dénominateur) — dernier rempart posé par la FK en base (RESTRICT, pas d'ondelete)."""
    ratios_en_conflit = list(
        db.execute(
            select(RatioPrudentiel.libelle).where(
                (RatioPrudentiel.agregat_numerateur_id == agregat.id)
                | (RatioPrudentiel.agregat_denominateur_id == agregat.id)
            )
        ).scalars()
    )
    if ratios_en_conflit:
        raise AgregatReferenceError(
            f"L'agrégat « {agregat.libelle} » est utilisé par {len(ratios_en_conflit)} "
            f"ratio(s) ({', '.join(ratios_en_conflit)}) — impossible de le supprimer."
        )

    trace = {"code": agregat.code, "libelle": agregat.libelle, "motif": motif}
    agregat_id = agregat.id
    db.delete(agregat)
    db.flush()

    ecrire_audit(
        db,
        action="conformite.agregat.deleted",
        contexte=contexte,
        acteur_id=par,
        resource_type=RESSOURCE_AGREGAT,
        resource_id=agregat_id,
        old_values=trace,
    )


# --- Ratios -----------------------------------------------------------------------------------


def lister_ratios(db: Session) -> Sequence[RatioPrudentiel]:
    return db.execute(select(RatioPrudentiel).order_by(RatioPrudentiel.ordre)).scalars().all()


def obtenir_ratio(db: Session, ratio_id: uuid.UUID) -> RatioPrudentiel | None:
    return db.get(RatioPrudentiel, ratio_id)


def obtenir_ratio_par_code(db: Session, code: str) -> RatioPrudentiel | None:
    return db.execute(
        select(RatioPrudentiel).where(RatioPrudentiel.code == code)
    ).scalar_one_or_none()


def _resoudre_agregat_par_code(db: Session, code: str) -> AgregatPrudentiel:
    agregat = db.execute(
        select(AgregatPrudentiel).where(AgregatPrudentiel.code == code)
    ).scalar_one_or_none()
    if agregat is None:
        raise AgregatIntrouvableError(f"Agrégat prudentiel inconnu : {code!r}.")
    return agregat


def _verifier_code_ratio_unique(
    db: Session, *, code: str, exclure_id: uuid.UUID | None = None
) -> None:
    conflit = db.execute(
        select(RatioPrudentiel).where(RatioPrudentiel.code == code)
    ).scalar_one_or_none()
    if conflit is not None and conflit.id != exclure_id:
        raise CodeRatioDejaUtiliseError(f"Le code « {code} » est déjà utilisé par un autre ratio.")


def _verifier_ordre_ratio_unique(
    db: Session, *, ordre: int, exclure_id: uuid.UUID | None = None
) -> None:
    conflit = db.execute(
        select(RatioPrudentiel).where(RatioPrudentiel.ordre == ordre)
    ).scalar_one_or_none()
    if conflit is not None and conflit.id != exclure_id:
        raise OrdreRatioDejaUtiliseError(
            f"L'ordre {ordre} est déjà utilisé par le ratio « {conflit.libelle} »."
        )


def creer_ratio(
    db: Session,
    *,
    code: str,
    libelle: str,
    reference_reglementaire: str | None,
    agregat_numerateur_code: str,
    agregat_denominateur_code: str,
    operateur: str,
    actif: bool,
    ordre: int,
    motif: str,
    par: uuid.UUID | None,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> RatioPrudentiel:
    """Ajoute UN ratio — `is_system=False`. Numérateur/dénominateur résolus PAR CODE."""
    _verifier_code_ratio_unique(db, code=code)
    _verifier_ordre_ratio_unique(db, ordre=ordre)
    agregat_num = _resoudre_agregat_par_code(db, agregat_numerateur_code)
    agregat_denom = _resoudre_agregat_par_code(db, agregat_denominateur_code)

    ratio = RatioPrudentiel(
        code=code,
        libelle=libelle,
        reference_reglementaire=reference_reglementaire,
        agregat_numerateur_id=agregat_num.id,
        agregat_denominateur_id=agregat_denom.id,
        operateur=operateur,
        actif=actif,
        ordre=ordre,
        is_system=False,
        created_by=par,
        updated_by=par,
    )
    db.add(ratio)
    db.flush()

    ecrire_audit(
        db,
        action="conformite.ratio.created",
        contexte=contexte,
        acteur_id=par,
        resource_type=RESSOURCE_RATIO,
        resource_id=ratio.id,
        new_values={
            "code": code,
            "libelle": libelle,
            "agregat_numerateur": agregat_numerateur_code,
            "agregat_denominateur": agregat_denominateur_code,
            "operateur": operateur,
            "actif": actif,
            "ordre": ordre,
            "motif": motif,
        },
    )
    return ratio


def modifier_ratio(
    db: Session,
    ratio: RatioPrudentiel,
    *,
    code: str,
    libelle: str,
    reference_reglementaire: str | None,
    agregat_numerateur_code: str,
    agregat_denominateur_code: str,
    operateur: str,
    actif: bool,
    ordre: int,
    motif: str,
    par: uuid.UUID | None,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> RatioPrudentiel:
    """Remplace l'ÉTAT COMPLET du ratio — activation/désactivation et changement d'ordre ou
    d'opérateur passent par ce même endpoint, pas par des routes dédiées."""
    _verifier_code_ratio_unique(db, code=code, exclure_id=ratio.id)
    _verifier_ordre_ratio_unique(db, ordre=ordre, exclure_id=ratio.id)
    agregat_num = _resoudre_agregat_par_code(db, agregat_numerateur_code)
    agregat_denom = _resoudre_agregat_par_code(db, agregat_denominateur_code)

    avant_num = db.get(AgregatPrudentiel, ratio.agregat_numerateur_id)
    avant_denom = db.get(AgregatPrudentiel, ratio.agregat_denominateur_id)
    avant = {
        "code": ratio.code,
        "libelle": ratio.libelle,
        "agregat_numerateur": avant_num.code if avant_num else None,
        "agregat_denominateur": avant_denom.code if avant_denom else None,
        "operateur": ratio.operateur,
        "actif": ratio.actif,
        "ordre": ratio.ordre,
    }

    ratio.code = code
    ratio.libelle = libelle
    ratio.reference_reglementaire = reference_reglementaire
    ratio.agregat_numerateur_id = agregat_num.id
    ratio.agregat_denominateur_id = agregat_denom.id
    ratio.operateur = operateur
    ratio.actif = actif
    ratio.ordre = ordre
    ratio.updated_by = par
    db.flush()

    ecrire_audit(
        db,
        action="conformite.ratio.updated",
        contexte=contexte,
        acteur_id=par,
        resource_type=RESSOURCE_RATIO,
        resource_id=ratio.id,
        old_values=avant,
        new_values={
            "code": code,
            "libelle": libelle,
            "agregat_numerateur": agregat_numerateur_code,
            "agregat_denominateur": agregat_denominateur_code,
            "operateur": operateur,
            "actif": actif,
            "ordre": ordre,
            "motif": motif,
        },
    )
    return ratio


def supprimer_ratio(
    db: Session,
    ratio: RatioPrudentiel,
    *,
    motif: str,
    par: uuid.UUID | None,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> None:
    """Retire UN ratio — rien ne référence un ratio ailleurs dans le modèle, suppression
    toujours permise ; ses seuils partent en cascade (FK ON DELETE CASCADE, migration 0057)."""
    trace = {"code": ratio.code, "libelle": ratio.libelle, "motif": motif}
    ratio_id = ratio.id
    db.delete(ratio)
    db.flush()

    ecrire_audit(
        db,
        action="conformite.ratio.deleted",
        contexte=contexte,
        acteur_id=par,
        resource_type=RESSOURCE_RATIO,
        resource_id=ratio_id,
        old_values=trace,
    )


# --- Seuils -----------------------------------------------------------------------------------


def lister_seuils(db: Session, ratio_id: uuid.UUID) -> Sequence[RatioSeuil]:
    return (
        db.execute(select(RatioSeuil).where(RatioSeuil.ratio_id == ratio_id))
        .scalars()
        .all()
    )


def obtenir_seuil(db: Session, seuil_id: uuid.UUID) -> RatioSeuil | None:
    return db.get(RatioSeuil, seuil_id)


def _verifier_categorie_seuil_unique(
    db: Session,
    *,
    ratio_id: uuid.UUID,
    categorie_sfd: str | None,
    exclure_id: uuid.UUID | None = None,
) -> None:
    conflit = db.execute(
        select(RatioSeuil).where(
            RatioSeuil.ratio_id == ratio_id, RatioSeuil.categorie_sfd == categorie_sfd
        )
    ).scalar_one_or_none()
    if conflit is not None and conflit.id != exclure_id:
        raise SeuilDejaDefiniError(
            f"Un seuil existe déjà pour la catégorie "
            f"{categorie_sfd or 'universelle'!r} de ce ratio."
        )


def creer_seuil(
    db: Session,
    ratio: RatioPrudentiel,
    *,
    categorie_sfd: str | None,
    valeur_seuil: Decimal,
    motif: str,
    par: uuid.UUID | None,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> RatioSeuil:
    _verifier_categorie_seuil_unique(db, ratio_id=ratio.id, categorie_sfd=categorie_sfd)

    seuil = RatioSeuil(
        ratio_id=ratio.id,
        categorie_sfd=categorie_sfd,
        valeur_seuil=valeur_seuil,
        is_system=False,
        created_by=par,
        updated_by=par,
    )
    db.add(seuil)
    db.flush()

    ecrire_audit(
        db,
        action="conformite.seuil.created",
        contexte=contexte,
        acteur_id=par,
        resource_type=RESSOURCE_SEUIL,
        resource_id=seuil.id,
        new_values={
            "ratio": ratio.code,
            "categorie_sfd": categorie_sfd,
            "valeur_seuil": str(valeur_seuil),
            "motif": motif,
        },
    )
    return seuil


def modifier_seuil(
    db: Session,
    seuil: RatioSeuil,
    *,
    categorie_sfd: str | None,
    valeur_seuil: Decimal,
    motif: str,
    par: uuid.UUID | None,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> RatioSeuil:
    _verifier_categorie_seuil_unique(
        db, ratio_id=seuil.ratio_id, categorie_sfd=categorie_sfd, exclure_id=seuil.id
    )

    avant = {"categorie_sfd": seuil.categorie_sfd, "valeur_seuil": str(seuil.valeur_seuil)}

    seuil.categorie_sfd = categorie_sfd
    seuil.valeur_seuil = valeur_seuil
    seuil.updated_by = par
    db.flush()

    ecrire_audit(
        db,
        action="conformite.seuil.updated",
        contexte=contexte,
        acteur_id=par,
        resource_type=RESSOURCE_SEUIL,
        resource_id=seuil.id,
        old_values=avant,
        new_values={
            "categorie_sfd": categorie_sfd,
            "valeur_seuil": str(valeur_seuil),
            "motif": motif,
        },
    )
    return seuil


def supprimer_seuil(
    db: Session,
    seuil: RatioSeuil,
    *,
    motif: str,
    par: uuid.UUID | None,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> None:
    trace = {
        "categorie_sfd": seuil.categorie_sfd,
        "valeur_seuil": str(seuil.valeur_seuil),
        "motif": motif,
    }
    seuil_id = seuil.id
    db.delete(seuil)
    db.flush()

    ecrire_audit(
        db,
        action="conformite.seuil.deleted",
        contexte=contexte,
        acteur_id=par,
        resource_type=RESSOURCE_SEUIL,
        resource_id=seuil_id,
        old_values=trace,
    )


# --- Paramètre institution (singleton) --------------------------------------------------------


def obtenir_parametre_institution(db: Session) -> ParametreInstitution | None:
    return db.execute(select(ParametreInstitution)).scalar_one_or_none()


def modifier_parametre_institution(
    db: Session,
    parametre: ParametreInstitution,
    *,
    categorie_sfd: str,
    complement_provisions_tutelle: int,
    motif: str,
    par: uuid.UUID | None,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> ParametreInstitution:
    """Pas de création via l'API (singleton bootstrapé par `seed-conformite`), pas de
    suppression. Remplacement complet des deux seuls champs métier."""
    avant = {
        "categorie_sfd": parametre.categorie_sfd,
        "complement_provisions_tutelle": parametre.complement_provisions_tutelle,
    }

    parametre.categorie_sfd = categorie_sfd
    parametre.complement_provisions_tutelle = complement_provisions_tutelle
    parametre.updated_by = par
    db.flush()

    ecrire_audit(
        db,
        action="conformite.parametre_institution.updated",
        contexte=contexte,
        acteur_id=par,
        resource_type=RESSOURCE_PARAMETRE,
        resource_id=parametre.id,
        old_values=avant,
        new_values={
            "categorie_sfd": categorie_sfd,
            "complement_provisions_tutelle": complement_provisions_tutelle,
            "motif": motif,
        },
    )
    return parametre
