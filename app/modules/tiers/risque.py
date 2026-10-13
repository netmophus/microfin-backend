"""Moteur de score de risque LBC/FT (T3b).

TROIS TYPES DE RÈGLES, appliqués dans cet ordre — et TOUT est calculé, rien n'est court-circuité
(même sous couperet, on archive le raisonnement entier) :

  1. contributives -> somme des points -> `score` ;
  2. barème        -> `niveau_bareme` (le score projeté sur les bornes) ;
  3. planchers     -> `niveau_effectif = max(niveau_bareme, planchers déclenchés)` ;
  4. couperets     -> `couperets[]` + `couperet_declenche` ; un couperet relève aussi le niveau
                      effectif à « eleve » (un client bloqué n'est jamais « faible »).

CŒUR PUR. `evaluer(entrees, grille, declencheur)` n'a aucun effet de bord et ne lit ni la base ni
l'horloge : mêmes entrées -> même résultat, rejouable. Le chargement de la grille, l'extraction
des entrées et la persistance sont des étapes séparées (I/O).

DONNÉES MANQUANTES = pensée LBC/FT. L'absence n'est pas neutre : on ne fabrique pas un faux score
bas. Une règle dont l'entrée manque est « non évaluée » (distinct de « évaluée, pas à risque »), et
la donnée manquante est recensée -> le gate d'activation (T3c) la réclamera. On calcule sur le
connu, honnêtement, et on refuse de conclure sur l'inconnu.

SNAPSHOT AUTO-LISIBLE. `detail` (JSONB archivé) porte, en clair, chaque règle (libellé humain, type,
constat, effet chiffré), les entrées réelles, le barème et le résultat — lisible seul dans 10 ans,
sans la grille ni le code de l'époque. `is_provisional` de la grille y est GELÉ.
"""

import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.comptabilite.journee import journee_de_reference
from app.modules.parameters.models import (
    Country,
    KycRiskGrid,
    KycRiskRule,
    KycRiskThreshold,
    SecteurActivite,
)
from app.modules.security.autorisation import UtilisateurCourant
from app.modules.tiers.models import (
    IndividualProfile,
    RiskAssessment,
    Tier,
    TierRelatedPerson,
    liens_en_cours,
)

SCHEMA_SNAPSHOT = 1

_RANG = {"faible": 0, "moyen": 1, "eleve": 2}
_LABEL_TYPE_TIERS = {
    "individual": "Personne physique",
    "legal_entity": "Personne morale",
    "group": "Groupement",
}
_LABEL_MODE = {
    "presentiel": "Présentiel (au guichet)",
    "tiers_confiance": "Introduction par un tiers de confiance",
    "distance": "À distance",
}


class GrilleIntrouvableError(Exception):
    """Aucune grille de risque active — le seed n'a pas été appliqué."""


# --- données d'entrée et grille (immuables : le cœur est pur) ---------------------------


@dataclass(frozen=True)
class RegleGrille:
    code: str
    libelle: str
    rule_type: str  # contributive | plancher | couperet
    critere: str
    points: int
    niveau_impose: str | None
    bloquant: bool


@dataclass(frozen=True)
class GrilleSnapshot:
    id: uuid.UUID
    version: int
    libelle: str
    is_provisional: bool
    regles: tuple[RegleGrille, ...]
    bareme: tuple[tuple[str, int], ...]  # (niveau, score_min)


@dataclass(frozen=True)
class EntreeRisque:
    """Les données de risque du client à l'instant du calcul. `*_renseigne` distingue « absent »
    de « présent mais négatif » — la nuance LBC/FT."""

    type_tiers: str
    nationalite_code: str | None = None
    nationalite_libelle: str | None = None
    nationalite_gafi: bool = False
    secteur_renseigne: bool = False
    secteur_libelle: str | None = None
    secteur_a_risque: bool = False
    profession: str | None = None
    ppe_status: bool = False
    ppe_relation: str | None = None
    ppe_fonction: str | None = None
    mode_entree_relation: str | None = None
    revenus_estimes: int | None = None
    sanctions_match: bool = False  # T6 : sans données de sanctions, toujours False


@dataclass(frozen=True)
class ResultatRisque:
    score: int
    niveau_bareme: str
    niveau_effectif: str
    couperet_declenche: bool
    donnees_manquantes: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)


# --- le cœur PUR -----------------------------------------------------------------------


def _niveau_pour_score(score: int, bareme: tuple[tuple[str, int], ...]) -> str:
    """Projette un score sur le barème : le niveau à la borne basse la plus haute atteinte."""
    gagnant = "faible"
    meilleure = -1
    for niveau, score_min in bareme:
        if score >= score_min and score_min >= meilleure:
            gagnant, meilleure = niveau, score_min
    return gagnant


def _max_niveau(niveaux: list[str]) -> str:
    return max(niveaux, key=lambda n: _RANG.get(n, 0))


def _appliquer(regle: RegleGrille, e: EntreeRisque) -> tuple[bool, str, str | None]:
    """Évalue UNE règle -> (déclenchée, constat lisible, donnée manquante | None)."""
    c = regle.critere
    if c == "pays_gafi":
        if e.nationalite_code is None:
            return False, "Non évaluée — nationalité non renseignée", "nationalite"
        nom = e.nationalite_libelle or e.nationalite_code
        if e.nationalite_gafi:
            return True, f"Nationalité « {nom} » listée comme pays à risque GAFI", None
        return False, f"Nationalité « {nom} » non listée comme pays à risque GAFI", None
    if c == "secteur_risque":
        if not e.secteur_renseigne:
            return False, "Non évaluée — secteur d'activité non renseigné", "secteur_activite"
        nom = e.secteur_libelle or "?"
        if e.secteur_a_risque:
            return True, f"Secteur « {nom} » marqué à risque", None
        return False, f"Secteur « {nom} » non marqué à risque", None
    if c == "volume_eleve":
        return False, "Non évaluée — seuil de volume à paramétrer", None
    if c == "mode_distance":
        if e.mode_entree_relation is None:
            return False, "Non évaluée — mode d'entrée non renseigné", "mode_entree_relation"
        if e.mode_entree_relation == "distance":
            return True, "Mode d'entrée « À distance »", None
        libelle = _LABEL_MODE.get(e.mode_entree_relation, e.mode_entree_relation)
        return False, f"Mode d'entrée « {libelle} », pas à distance", None
    if c == "mode_tiers":
        if e.mode_entree_relation is None:
            return False, "Non évaluée — mode d'entrée non renseigné", "mode_entree_relation"
        if e.mode_entree_relation == "tiers_confiance":
            return True, "Mode d'entrée « Introduction par un tiers de confiance »", None
        return False, "Mode d'entrée autre que « tiers de confiance »", None
    if c == "ppe":
        if e.ppe_status:
            fonction = e.ppe_fonction or "non précisée"
            relation = e.ppe_relation or "non précisée"
            constat = f"Client politiquement exposé — fonction « {fonction} » (lien : {relation})"
            return True, constat, None
        return False, "Client non politiquement exposé", None
    if c == "sanctions":
        if e.sanctions_match:
            return True, "Correspondance avec une liste de sanctions", None
        return False, "Aucune correspondance (module de filtrage des sanctions non en place)", None
    return False, "Non évaluée — critère non reconnu par le moteur", None


def _entrees_lisibles(e: EntreeRisque, manquantes: list[str]) -> dict[str, Any]:
    nationalite: Any = None
    if e.nationalite_code is not None:
        nationalite = {
            "code": e.nationalite_code,
            "libelle": e.nationalite_libelle,
            "pays_a_risque_gafi": e.nationalite_gafi,
        }
    secteur: Any = "non renseigné"
    if e.secteur_renseigne:
        secteur = {"libelle": e.secteur_libelle, "marque_a_risque": e.secteur_a_risque}
    return {
        "type_tiers": _LABEL_TYPE_TIERS.get(e.type_tiers, e.type_tiers),
        "nationalite": nationalite,
        "secteur_activite": secteur,
        "profession_declaree": e.profession,
        "personne_politiquement_exposee": e.ppe_status,
        "ppe_relation": e.ppe_relation,
        "ppe_fonction": e.ppe_fonction,
        "mode_entree_relation": _LABEL_MODE.get(
            e.mode_entree_relation or "", e.mode_entree_relation
        ),
        "revenus_mensuels_estimes": (
            str(e.revenus_estimes) if e.revenus_estimes is not None else None
        ),
        "donnees_manquantes": manquantes,
    }


def evaluer(entrees: EntreeRisque, grille: GrilleSnapshot, declencheur: str) -> ResultatRisque:
    """CŒUR PUR. Applique la grille aux entrées, renvoie le résultat + le snapshot complet."""
    regles_out: list[dict[str, Any]] = []
    score = 0
    planchers: list[tuple[str, str]] = []
    couperets: list[str] = []
    manquantes: list[str] = []

    for regle in grille.regles:
        declenchee, constat, manque = _appliquer(regle, entrees)
        if manque and manque not in manquantes:
            manquantes.append(manque)

        if regle.rule_type == "contributive":
            points = regle.points if declenchee else 0
            score += points
            effet: dict[str, Any] = {"points": points}
        elif regle.rule_type == "plancher":
            effet = {"niveau_impose": regle.niveau_impose if declenchee else None}
            if declenchee and regle.niveau_impose:
                planchers.append((regle.libelle, regle.niveau_impose))
        else:  # couperet
            bloque = bool(declenchee and regle.bloquant)
            effet = {"bloque": bloque}
            if bloque:
                couperets.append(regle.libelle)

        regles_out.append(
            {
                "libelle": regle.libelle,
                "type": regle.rule_type,
                "declenchee": declenchee,
                "constat": constat,
                "effet": effet,
            }
        )

    niveau_bareme = _niveau_pour_score(score, grille.bareme)
    niveaux = [niveau_bareme, *[n for _, n in planchers]]
    if couperets:
        niveaux.append("eleve")
    niveau_effectif = _max_niveau(niveaux)

    explication = f"Score {score} → « {niveau_bareme} » selon le barème"
    if _RANG[niveau_effectif] > _RANG[niveau_bareme]:
        cause = "un couperet" if couperets else "un plancher"
        explication += f" ; relevé à « {niveau_effectif} » par {cause}"

    detail: dict[str, Any] = {
        "schema_version": SCHEMA_SNAPSHOT,
        "declencheur": declencheur,
        "grille": {
            "id": str(grille.id),
            "version": grille.version,
            "is_provisional": grille.is_provisional,
            "libelle": grille.libelle,
        },
        "entrees": _entrees_lisibles(entrees, manquantes),
        "regles": regles_out,
        "bareme": [{"niveau": n, "score_min": s} for n, s in grille.bareme],
        "calcul": {
            "score": score,
            "niveau_bareme": niveau_bareme,
            "planchers_appliques": [{"libelle": lib, "niveau": niv} for lib, niv in planchers],
            "couperets_declenches": couperets,
            "niveau_effectif": niveau_effectif,
            "explication": explication,
        },
        "resultat": {
            "score": score,
            "niveau_effectif": niveau_effectif,
            "bloque": bool(couperets),
        },
    }

    return ResultatRisque(
        score=score,
        niveau_bareme=niveau_bareme,
        niveau_effectif=niveau_effectif,
        couperet_declenche=bool(couperets),
        donnees_manquantes=manquantes,
        detail=detail,
    )


# --- I/O : chargement de la grille, extraction des entrées, persistance ----------------


def charger_grille_active(db: Session) -> GrilleSnapshot:
    """Charge la grille ACTIVE (règles actives + barème) en mémoire — la seule I/O de lecture."""
    grid = db.execute(
        select(KycRiskGrid).where(KycRiskGrid.is_active.is_(True))
    ).scalars().first()
    if grid is None:
        raise GrilleIntrouvableError()

    regles = db.execute(
        select(KycRiskRule).where(KycRiskRule.grid_id == grid.id, KycRiskRule.actif.is_(True))
    ).scalars().all()
    bornes = db.execute(
        select(KycRiskThreshold).where(KycRiskThreshold.grid_id == grid.id)
    ).scalars().all()

    return GrilleSnapshot(
        id=grid.id,
        version=grid.version,
        libelle=grid.libelle,
        is_provisional=grid.is_provisional,
        regles=tuple(
            RegleGrille(
                code=r.code,
                libelle=r.libelle,
                rule_type=r.rule_type,
                critere=r.critere,
                points=r.points,
                niveau_impose=r.niveau_impose,
                bloquant=r.bloquant,
            )
            for r in regles
        ),
        bareme=tuple((b.niveau, b.score_min) for b in bornes),
    )


def extraire_entrees(db: Session, tier: Tier) -> EntreeRisque:
    """Rassemble les données de risque d'un tiers. Aujourd'hui : personne physique (T3 = PP)."""
    if not isinstance(tier, IndividualProfile):
        return EntreeRisque(type_tiers=tier.tier_type)

    nat = db.get(Country, tier.nationality_id)
    secteur = (
        db.get(SecteurActivite, tier.secteur_activite_id) if tier.secteur_activite_id else None
    )
    return EntreeRisque(
        type_tiers=tier.tier_type,
        nationalite_code=nat.code if nat else None,
        nationalite_libelle=nat.name if nat else None,
        nationalite_gafi=nat.is_gafi_high_risk if nat else False,
        secteur_renseigne=secteur is not None,
        secteur_libelle=secteur.libelle if secteur else None,
        secteur_a_risque=secteur.is_a_risque if secteur else False,
        profession=tier.profession,
        ppe_status=tier.ppe_status,
        ppe_relation=tier.ppe_relation,
        ppe_fonction=tier.ppe_fonction,
        mode_entree_relation=tier.mode_entree_relation,
        revenus_estimes=tier.monthly_income_estimate,
    )


# --- personnes morales et groupements : risque HÉRITÉ des personnes liées ---------------
#
# RÈGLE PROVISOIRE (à définir avec l'expert) : le niveau d'une PM ou d'un groupement est le plus
# haut niveau parmi ses personnes liées EN COURS (une PPE au bureau relève donc la PM). Les
# critères propres aux PM (secteur, pays, structure du capital) ne sont pas encore modélisés.
# Aucune personne liée évaluée -> pas de score : le gate d'activation réclame un représentant légal.

REGLE_PM_HERITEE = "pm_heritee"
_LABEL_NIVEAU = {"faible": "faible", "moyen": "moyen", "eleve": "élevé"}
_LABEL_ROLE = {
    "representant_legal": "représentant légal",
    "dirigeant": "dirigeant",
    "membre_bureau": "membre du bureau",
    "beneficiaire_effectif": "bénéficiaire effectif",
    "signataire": "signataire",
}


def _couperet_actif(db: Session, tier_id: uuid.UUID) -> bool:
    """La dernière évaluation archivée de la personne porte-t-elle un couperet ?"""
    return bool(
        db.execute(
            select(RiskAssessment.couperet_declenche)
            .where(RiskAssessment.tier_id == tier_id)
            .order_by(RiskAssessment.assessed_at.desc(), RiskAssessment.id.desc())
            .limit(1)
        ).scalar_one_or_none()
    )


def evaluer_pm(db: Session, tier: Tier, declencheur: str) -> ResultatRisque:
    """Risque d'une PM/d'un groupement = le plus haut niveau de ses personnes liées en cours."""
    aujourdhui = journee_de_reference(db)
    liens = db.execute(
        select(TierRelatedPerson, IndividualProfile)
        .join(IndividualProfile, IndividualProfile.id == TierRelatedPerson.individual_tier_id)
        .where(
            TierRelatedPerson.tier_id == tier.id,
            liens_en_cours(aujourdhui),
            IndividualProfile.deleted_at.is_(None),
        )
        .order_by(TierRelatedPerson.created_at, TierRelatedPerson.id)
    ).all()

    personnes: list[dict[str, Any]] = []
    couperet = False
    for lien, individu in liens:
        if individu.risk_level is None or individu.risk_score is None:
            continue  # personne liée jamais évaluée : on ne fabrique pas de niveau
        # Le reflet de la fiche est mis à jour dans la même transaction que l'archive.
        couperet = couperet or _couperet_actif(db, individu.id)
        personnes.append(
            {
                "nom": f"{individu.last_name} {individu.first_name}",
                "numero": individu.tier_number,
                "role": lien.role,
                "niveau": individu.risk_level,
                "score": individu.risk_score,
            }
        )

    base: dict[str, Any] = {
        "schema_version": SCHEMA_SNAPSHOT,
        "declencheur": declencheur,
        "regle": REGLE_PM_HERITEE,
        "regle_provisoire": True,
        "type_tiers": _LABEL_TYPE_TIERS.get(tier.tier_type, tier.tier_type),
        "personnes_liees": personnes,
    }
    if not personnes:
        base["sans_evaluation"] = True
        base["explication"] = "Aucune personne liée évaluée : niveau de risque non déterminé."
        return ResultatRisque(
            score=0,
            niveau_bareme="faible",
            niveau_effectif="faible",
            couperet_declenche=False,
            donnees_manquantes=[],
            detail=base,
        )

    origine = max(personnes, key=lambda p: (_RANG.get(p["niveau"], 0), p["score"]))
    niveau = origine["niveau"]
    role = _LABEL_ROLE.get(origine["role"], origine["role"])
    base["origine"] = origine
    base["explication"] = (
        f"{_LABEL_NIVEAU.get(niveau, niveau).capitalize()} — hérité de {origine['nom']}, {role}"
    )
    return ResultatRisque(
        score=max(p["score"] for p in personnes),
        niveau_bareme=niveau,
        niveau_effectif=niveau,
        couperet_declenche=couperet,
        donnees_manquantes=[],
        detail=base,
    )


def evaluer_tiers(
    db: Session, tier: Tier, grille: GrilleSnapshot, declencheur: str
) -> ResultatRisque:
    """Point d'entrée unique du calcul : grille pour une personne physique, héritage sinon."""
    if isinstance(tier, IndividualProfile):
        return evaluer(extraire_entrees(db, tier), grille, declencheur)
    return evaluer_pm(db, tier, declencheur)


def _evaluer_et_archiver_pm(
    db: Session, tier: Tier, declencheur: str, acteur_id: uuid.UUID | None
) -> ResultatRisque:
    resultat = evaluer_pm(db, tier, declencheur)
    if resultat.detail.get("sans_evaluation"):
        # Plus de personne évaluée : le reflet est effacé (rien à archiver, rien à inventer).
        tier.risk_level = None
        tier.risk_score = None
        tier.risk_computed_at = None
        tier.risk_grid_version = None
        db.flush()
        return resultat
    # Règle PM provisoire : le drapeau est gelé à vrai dans l'archive.
    grille = replace(charger_grille_active(db), is_provisional=True)
    enregistrer(db, tier, resultat, grille, declencheur, acteur_id)
    return resultat


def _propager_aux_liens(db: Session, individu: Tier, acteur_id: uuid.UUID | None) -> None:
    """Le niveau d'une personne physique a changé : recalcule chaque PM/groupement qui l'a pour
    personne liée EN COURS (c'est ce qui fait remonter une PPE jusqu'à la PM)."""
    aujourdhui = journee_de_reference(db)
    parents = (
        db.execute(
            select(TierRelatedPerson.tier_id)
            .where(
                TierRelatedPerson.individual_tier_id == individu.id, liens_en_cours(aujourdhui)
            )
            .distinct()
        )
        .scalars()
        .all()
    )
    for parent_id in parents:
        parent = db.get(Tier, parent_id)
        if parent is None or parent.deleted_at is not None:
            continue
        _evaluer_et_archiver_pm(db, parent, "personne_liee_modifiee", acteur_id)


def enregistrer(
    db: Session,
    tier: Tier,
    resultat: ResultatRisque,
    grille: GrilleSnapshot,
    declencheur: str,
    acteur_id: uuid.UUID | None,
) -> RiskAssessment:
    """Archive l'évaluation (append-only) et met à jour le REFLET sur la fiche. Ne committe pas."""
    ancien = (tier.risk_level, tier.risk_score)
    evaluation = RiskAssessment(
        tier_id=tier.id,
        assessed_by=acteur_id,
        trigger_event=declencheur,
        score=resultat.score,
        niveau_bareme=resultat.niveau_bareme,
        niveau_effectif=resultat.niveau_effectif,
        grid_id=grille.id,
        grid_version=grille.version,
        # GELÉ : reste provisoire même si la grille est validée plus tard.
        is_provisional=grille.is_provisional,
        couperet_declenche=resultat.couperet_declenche,
        detail=resultat.detail,
    )
    db.add(evaluation)
    # Reflet courant sur la fiche (cache d'affichage/filtrage, pas la source).
    tier.risk_level = resultat.niveau_effectif
    tier.risk_score = resultat.score
    tier.risk_computed_at = datetime.now(UTC)
    tier.risk_grid_version = grille.version
    db.flush()
    if isinstance(tier, IndividualProfile) and ancien != (tier.risk_level, tier.risk_score):
        _propager_aux_liens(db, tier, acteur_id)
    return evaluation


def evaluer_et_enregistrer(
    db: Session,
    courant: UtilisateurCourant | None,
    tier: Tier,
    declencheur: str,
) -> ResultatRisque:
    """Orchestration : charge la grille, extrait les entrées, ÉVALUE (pur), archive. Sans commit.

    courant None = calcul système (ex. à la création) -> assessed_by NULL."""
    acteur = courant.user_id if courant is not None else None
    if not isinstance(tier, IndividualProfile):
        return _evaluer_et_archiver_pm(db, tier, declencheur, acteur)
    grille = charger_grille_active(db)
    resultat = evaluer_tiers(db, tier, grille, declencheur)
    enregistrer(db, tier, resultat, grille, declencheur, acteur)
    return resultat
