"""Moteur de calcul des ratios prudentiels RCSFD (lot P2.1.b) — GÉNÉRIQUE : il ne connaît
AUCUNE formule, AUCUN seuil, AUCUNE composition d'agrégat. Tout est lu depuis
`conformite.agregat_prudentiel`/`agregat_compte`/`ratio_prudentiel`/`ratio_seuil`/
`parametre_institution` (lot P2.1.a).

LA CONVENTION DE SIGNE — à lire avant tout le reste.

`rapports.balance()` normalise DÉJÀ chaque solde de compte relativement à son `normal_side`
(`signe = 1 if normal_side == 'D' else -1`, voir rapports.py) : un compte créditeur (classe 5,
capitaux propres) avec un solde normal (crédit > débit) ressort POSITIF, exactement comme un
compte débiteur (actif) avec un solde normal ressort positif. CE PREMIER NIVEAU DE SIGNE EST
DÉJÀ RÉSOLU — ce moteur n'y touche pas, il réutilise `rapports.balance()` tel quel.

Le `sens` (+1/-1) d'`agregat_compte` répond à une question COMPLÈTEMENT DIFFÉRENTE : est-ce que
cette ligne AJOUTE ou SOUSTRAIT à la DÉFINITION RÉGLEMENTAIRE de l'agrégat — jamais une
correction de polarité débit/crédit (déjà faite). Exemple chiffré (fonds propres = capital
créditeur 1000 + réserves créditrices 500 - immobilisations incorporelles nettes 200) :
  - compte capital, normal_side='C', solde créditeur normal -> rapports.balance le rend à +1000.
    Ligne agregat_compte (prefixe, sens=+1) -> contribue +1000.
  - compte réserves, normal_side='C', solde créditeur normal -> balance +500. sens=+1 -> +500.
  - compte immobilisations incorporelles, normal_side='D', solde débiteur normal -> balance
    +200 (positif, car normal pour un compte débiteur). sens=-1 -> CONTRIBUE -200.
  - Total agrégat = 1000 + 500 - 200 = 1300. Positif, comme attendu d'un agrégat de fonds
    propres. Preuve chiffrée reproduite dans test_conformite_moteur.py.

Un agrégat MIXTE actif/passif (deux lignes sens=+1, l'une sur un compte débiteur normal +100,
l'autre sur un compte créditeur normal +50) s'additionne simplement : 100 + 50 = 150 — les deux
contributions sont DÉJÀ dans le même repère (positif = normal pour CE compte) avant que `sens`
n'intervienne ; `sens` ne fait qu'arbitrer l'appartenance à l'agrégat, jamais la polarité.

`nets_de_provisions` (colonne informative sur `agregat_prudentiel`) : AUCUN comportement
spécial ici. Le déduire mécaniquement supposerait une correspondance rigide entre un compte de
risque et son compte de provision — or le projet a déjà constaté qu'elle n'est PAS terme à
terme entre classes (ex. 29 a 3 tranches officielles, 664 en a 4, voir
docs/conformite-credit.md §2) : une règle câblée serait fausse pour au moins un cas réel. La
netting RÉELLE se fait par des lignes `agregat_compte` ordinaires à `sens=-1` sur les comptes
de provision exacts — EXACTEMENT le même mécanisme que la déduction des immobilisations
incorporelles ci-dessus, pas un chemin de code séparé. Le booléen reste une étiquette
documentaire (utile à un futur écran de validation de paramétrage) ; il NE PILOTE RIEN ici —
DÉCISION PRISE, à confirmer : si l'intention était un comportement automatique, le dire avant
l'étape 3.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.modules.comptabilite import etats_financiers, rapports
from app.modules.comptabilite.models import JournalEntry
from app.modules.conformite.models import (
    AgregatCompte,
    AgregatPrudentiel,
    ParametreInstitution,
    RatioPrudentiel,
    RatioSeuil,
)
from app.modules.credit.models import Application
from app.modules.credit.remboursement import encours_actuel

STATUT_CONFORME = "CONFORME"
STATUT_NON_CONFORME = "NON_CONFORME"
STATUT_NON_CALCULABLE = "NON_CALCULABLE"

_DEUX_DECIMALES_POURCENT = Decimal("0.0001")

# Codes d'avertissement — PRÉSENTATION, jamais du calcul : ils ne modifient ni la valeur ni le
# statut d'un ratio, ils rendent lisible une situation anormale (voir `calculer_avertissements`).
AVERT_NUMERATEUR_NUL = "NUMERATEUR_NUL"
AVERT_FONDS_PROPRES_NULS = "FONDS_PROPRES_NULS"


class AgregatIntrouvableError(Exception):
    """Le code d'agrégat demandé n'existe pas dans `conformite.agregat_prudentiel`."""


class RatioIntrouvableError(Exception):
    """Le code de ratio demandé n'existe pas dans `conformite.ratio_prudentiel`."""


class CalculSpecialInconnuError(Exception):
    """Un agrégat SPECIAL référence un `calcul_special` sans fonction enregistrée pour lui —
    paramétrage incomplet, jamais un crash silencieux ailleurs."""


@dataclass(frozen=True)
class Avertissement:
    """Message NON BLOQUANT attaché à une évaluation : `code` stable (testable, traduisible),
    `libelle` en français pour l'écran."""

    code: str
    libelle: str


@dataclass(frozen=True)
class RegleAgregatNul:
    """Règle de présentation nommée : « l'agrégat `agregat_nul` vaut 0 alors que
    `agregat_reference` est strictement positif » -> avertissement `code`. S'applique aux ratios
    qui référencent l'un ou l'autre des deux agrégats. Désactivée sans erreur si l'un des deux
    agrégats n'existe pas dans la base de CETTE institution."""

    code: str
    libelle: str
    agregat_nul: str
    agregat_reference: str


# Seul endroit où un code d'agrégat est nommé pour de la PRÉSENTATION : des dépôts (ressources)
# sans aucun fonds propres sont une anomalie à signaler, mais ce n'est pas une règle
# réglementaire — la formule des ratios, elle, reste entièrement paramétrée en base. Ajouter un
# avertissement du même type = une ligne ici, sans toucher au moteur.
REGLES_AGREGAT_NUL: tuple[RegleAgregatNul, ...] = (
    RegleAgregatNul(
        code=AVERT_FONDS_PROPRES_NULS,
        libelle="Aucun fonds propres enregistré à cette date",
        agregat_nul="FONDS_PROPRES",
        agregat_reference="RESSOURCES",
    ),
)

LIBELLE_NUMERATEUR_NUL = "Aucun risque porté à cette date"


@dataclass(frozen=True)
class EvaluationRatio:
    """Le résultat complet de l'évaluation d'UN ratio à une date d'arrêté."""

    code: str
    libelle: str
    valeur_numerateur: int
    valeur_denominateur: int
    valeur_ratio_pct: Decimal | None
    operateur: str
    seuil_applicable: Decimal | None
    categorie_sfd_appliquee: str | None
    conforme: bool | None
    marge: Decimal | None
    statut: str
    avertissements: tuple[Avertissement, ...] = ()


def _compte_agregat(db: Session, code: str) -> AgregatPrudentiel:
    agregat = db.execute(
        select(AgregatPrudentiel).where(AgregatPrudentiel.code == code)
    ).scalar_one_or_none()
    if agregat is None:
        raise AgregatIntrouvableError(f"Agrégat prudentiel inconnu : {code!r}.")
    return agregat


def _valeur_balance(db: Session, agregat: AgregatPrudentiel, a_la_date: date) -> int:
    """BALANCE : Σ (sens * solde) par ligne de composition, solde lu depuis `rapports.balance`
    (cumul depuis l'origine jusqu'à `a_la_date`, DÉJÀ normalisé par `normal_side` — voir le
    docstring de module). `inclure_sans_mouvement` reste à son défaut (False) : un compte sans
    aucun mouvement a un solde nul, l'exclure ne change jamais la somme."""
    compositions = list(
        db.execute(
            select(AgregatCompte).where(AgregatCompte.agregat_id == agregat.id)
        ).scalars()
    )

    total = 0
    if compositions:
        resultat = rapports.balance(db, date_debut=None, date_fin=a_la_date)
        soldes_par_numero = {
            ligne.compte.account_number: ligne.solde_cloture for ligne in resultat.lignes
        }
        for composition in compositions:
            for numero, solde in soldes_par_numero.items():
                if numero.startswith(composition.prefixe_compte):
                    total += composition.sens * solde

    if agregat.applique_complement_provisions_tutelle:
        total -= _complement_provisions_tutelle(db)
    return total


def _complement_provisions_tutelle(db: Session) -> int:
    """`parametre_institution.complement_provisions_tutelle` — 0 si l'institution n'a pas
    encore été paramétrée (singleton absent), jamais une exception."""
    parametre = db.execute(select(ParametreInstitution)).scalar_one_or_none()
    return parametre.complement_provisions_tutelle if parametre is not None else 0


def _encours_plus_gros_emprunteur(db: Session, a_la_date: date) -> int:
    """SPECIAL 'PLUS_GROS_EMPRUNTEUR' : encours (capital restant dû) par tiers sur les dossiers
    DÉCAISSÉS, le maximum tous tiers confondus — group-by tier_id, réutilise `encours_actuel`
    (par dossier) sans le réécrire.

    LIMITE ASSUMÉE, à signaler explicitement : `encours_actuel` lit l'état COURANT des
    échéances (montant_paye cumulé), pas un instantané à une date passée — `a_la_date` n'est
    PAS honoré par ce calcul SPECIAL. Correct pour un arrêté à la date du jour (l'usage normal
    d'un ratio prudentiel vivant) ; FAUX pour un arrêté rétroactif. Aucune reconstruction
    historique de l'encours n'existe aujourd'hui dans le module crédit — à trancher avant tout
    usage de ce ratio sur une date passée."""
    dossiers = db.execute(
        select(Application.tier_id, Application.id).where(Application.status == "decaisse")
    ).all()
    encours_par_tier: dict[uuid.UUID, int] = {}
    for tier_id, application_id in dossiers:
        encours_par_tier[tier_id] = (
            encours_par_tier.get(tier_id, 0) + encours_actuel(db, application_id)
        )
    return max(encours_par_tier.values(), default=0)


def _total_actif_net(db: Session, a_la_date: date) -> int:
    """SPECIAL 'TOTAL_ACTIF_NET' : le total actif net DU BILAN (`etats_financiers.bilan`), à la
    date d'arrêté — actif brut moins contra-actif (provisions/amortissements), jamais recalculé
    ici par préfixes de comptes. Honore `a_la_date`. Limite héritée du bilan : un compte
    mouvementé sans mapping d'état financier n'y figure pas (le bilan le signale dans
    `comptes_non_mappes`)."""
    return etats_financiers.bilan(db, a_la_date).total_actif_net


_CALCULS_SPECIAUX: dict[str, Callable[[Session, date], int]] = {
    "PLUS_GROS_EMPRUNTEUR": _encours_plus_gros_emprunteur,
    "TOTAL_ACTIF_NET": _total_actif_net,
}


def agregat_valeur(db: Session, code: str, a_la_date: date) -> int:
    """La valeur d'UN agrégat (BALANCE ou SPECIAL) à une date d'arrêté, en F CFA entiers."""
    agregat = _compte_agregat(db, code)
    if agregat.type == "SPECIAL":
        fonction = _CALCULS_SPECIAUX.get(agregat.calcul_special or "")
        if fonction is None:
            raise CalculSpecialInconnuError(
                f"Aucun calcul enregistré pour {agregat.calcul_special!r} "
                f"(agrégat {code!r}) — paramétrage incomplet."
            )
        return fonction(db, a_la_date)
    return _valeur_balance(db, agregat, a_la_date)


@dataclass(frozen=True)
class ComposantAgregat:
    """Une ligne de composition, décomposée : `solde` = somme des soldes de TOUS les comptes
    dont le numéro commence par `prefixe_compte` (avant application du sens) ; `contribution`
    = `sens * solde`, ce qui entre réellement dans le total de l'agrégat."""

    prefixe_compte: str
    sens: int
    solde: int
    contribution: int


@dataclass(frozen=True)
class DetailAgregat:
    """Décomposition d'un agrégat — pour l'écran de détail d'un ratio (justifier un chiffre à
    la tutelle). `composants` est vide pour un agrégat SPECIAL (le calcul n'est pas une somme
    de comptes, rien à décomposer)."""

    code: str
    libelle: str
    type: str
    valeur: int
    composants: tuple[ComposantAgregat, ...]
    complement_provisions_tutelle_applique: int | None


def detail_agregat(db: Session, code: str, a_la_date: date) -> DetailAgregat:
    """Même valeur que `agregat_valeur` (recalculée via le même chemin, jamais dupliquée dans
    sa logique), accompagnée du détail par ligne de composition pour un agrégat BALANCE."""
    agregat = _compte_agregat(db, code)
    valeur = agregat_valeur(db, code, a_la_date)

    if agregat.type == "SPECIAL":
        return DetailAgregat(
            code=agregat.code,
            libelle=agregat.libelle,
            type=agregat.type,
            valeur=valeur,
            composants=(),
            complement_provisions_tutelle_applique=None,
        )

    compositions = list(
        db.execute(
            select(AgregatCompte).where(AgregatCompte.agregat_id == agregat.id)
        ).scalars()
    )
    composants: list[ComposantAgregat] = []
    if compositions:
        resultat = rapports.balance(db, date_debut=None, date_fin=a_la_date)
        soldes_par_numero = {
            ligne.compte.account_number: ligne.solde_cloture for ligne in resultat.lignes
        }
        for composition in compositions:
            solde_matche = sum(
                solde
                for numero, solde in soldes_par_numero.items()
                if numero.startswith(composition.prefixe_compte)
            )
            composants.append(
                ComposantAgregat(
                    prefixe_compte=composition.prefixe_compte,
                    sens=composition.sens,
                    solde=solde_matche,
                    contribution=composition.sens * solde_matche,
                )
            )

    complement = (
        _complement_provisions_tutelle(db)
        if agregat.applique_complement_provisions_tutelle
        else None
    )

    return DetailAgregat(
        code=agregat.code,
        libelle=agregat.libelle,
        type=agregat.type,
        valeur=valeur,
        composants=tuple(composants),
        complement_provisions_tutelle_applique=complement,
    )


def _categorie_institution(db: Session) -> str | None:
    """La catégorie réglementaire de CETTE institution (singleton), ou None si le
    paramétrage n'a pas encore été renseigné — dégrade sur le seuil universel, jamais une
    exception."""
    parametre = db.execute(select(ParametreInstitution)).scalar_one_or_none()
    return parametre.categorie_sfd if parametre is not None else None


def _seuil_applicable(
    db: Session, ratio_id: uuid.UUID, categorie: str | None
) -> Decimal | None:
    """Seuil propre à la catégorie si l'institution en a une ET qu'un seuil lui est dédié,
    sinon repli sur le seuil universel (`categorie_sfd IS NULL`). None si NI L'UN NI L'AUTRE
    n'existe — paramétrage incomplet, jamais une division par une valeur absente."""
    if categorie is not None:
        specifique = db.execute(
            select(RatioSeuil.valeur_seuil).where(
                RatioSeuil.ratio_id == ratio_id, RatioSeuil.categorie_sfd == categorie
            )
        ).scalar_one_or_none()
        if specifique is not None:
            return specifique
    return db.execute(
        select(RatioSeuil.valeur_seuil).where(
            RatioSeuil.ratio_id == ratio_id, RatioSeuil.categorie_sfd.is_(None)
        )
    ).scalar_one_or_none()


def compter_ecritures_validees(db: Session, a_la_date: date) -> int:
    """Nombre d'écritures VALIDÉES jusqu'à `a_la_date` incluse (même filtre que
    `rapports.balance`). Zéro = une vraie base vide, indépendamment des montants des agrégats."""
    return int(
        db.execute(
            select(func.count(JournalEntry.id)).where(
                JournalEntry.status == "validee", JournalEntry.entry_date <= a_la_date
            )
        ).scalar_one()
    )


def calculer_avertissements(
    db: Session,
    a_la_date: date,
    *,
    agregat_num: str,
    valeur_num: int,
    agregat_denom: str,
    valeur_denom: int,
) -> tuple[Avertissement, ...]:
    """Avertissements d'une évaluation — AUCUN effet sur la valeur ni sur le statut.

    - NUMERATEUR_NUL (générique, tout ratio) : numérateur nul pour un dénominateur réel. Le
      0,00 % est exact (cas légitime), mais un lecteur pressé doit voir POURQUOI.
    - Règles nommées `REGLES_AGREGAT_NUL` : un agrégat nul alors que son agrégat de référence
      est positif, pour les ratios qui référencent l'un des deux.
    """
    avertissements: list[Avertissement] = []
    if valeur_num == 0 and valeur_denom > 0:
        avertissements.append(Avertissement(AVERT_NUMERATEUR_NUL, LIBELLE_NUMERATEUR_NUL))

    connues = {agregat_num: valeur_num, agregat_denom: valeur_denom}

    def valeur(code: str) -> int | None:
        if code not in connues:
            try:
                connues[code] = agregat_valeur(db, code, a_la_date)
            except (AgregatIntrouvableError, CalculSpecialInconnuError):
                return None
        return connues[code]

    for regle in REGLES_AGREGAT_NUL:
        if not {regle.agregat_nul, regle.agregat_reference} & {agregat_num, agregat_denom}:
            continue
        nul = valeur(regle.agregat_nul)
        reference = valeur(regle.agregat_reference)
        if nul == 0 and reference is not None and reference > 0:
            avertissements.append(Avertissement(regle.code, regle.libelle))
    return tuple(avertissements)


def evaluer_ratio(db: Session, code: str, a_la_date: date) -> EvaluationRatio:
    """Numérateur, dénominateur, seuil selon la catégorie de l'institution, conformité.
    Dénominateur nul OU seuil absent -> `NON_CALCULABLE`, `conforme=None`, JAMAIS de division."""
    ratio = db.execute(
        select(RatioPrudentiel).where(RatioPrudentiel.code == code)
    ).scalar_one_or_none()
    if ratio is None:
        raise RatioIntrouvableError(f"Ratio prudentiel inconnu : {code!r}.")

    agregat_num = db.get(AgregatPrudentiel, ratio.agregat_numerateur_id)
    agregat_denom = db.get(AgregatPrudentiel, ratio.agregat_denominateur_id)
    assert agregat_num is not None and agregat_denom is not None  # FK NOT NULL

    valeur_num = agregat_valeur(db, agregat_num.code, a_la_date)
    valeur_denom = agregat_valeur(db, agregat_denom.code, a_la_date)

    categorie = _categorie_institution(db)
    seuil = _seuil_applicable(db, ratio.id, categorie)
    avertissements = calculer_avertissements(
        db,
        a_la_date,
        agregat_num=agregat_num.code,
        valeur_num=valeur_num,
        agregat_denom=agregat_denom.code,
        valeur_denom=valeur_denom,
    )

    if valeur_denom == 0 or seuil is None:
        return EvaluationRatio(
            code=ratio.code,
            libelle=ratio.libelle,
            valeur_numerateur=valeur_num,
            valeur_denominateur=valeur_denom,
            valeur_ratio_pct=None,
            operateur=ratio.operateur,
            seuil_applicable=seuil,
            categorie_sfd_appliquee=categorie,
            conforme=None,
            marge=None,
            statut=STATUT_NON_CALCULABLE,
            avertissements=avertissements,
        )

    ratio_pct = (Decimal(valeur_num) / Decimal(valeur_denom) * 100).quantize(
        _DEUX_DECIMALES_POURCENT, rounding=ROUND_HALF_UP
    )
    if ratio.operateur == "GE":
        conforme = ratio_pct >= seuil
        marge = ratio_pct - seuil
    else:
        conforme = ratio_pct <= seuil
        marge = seuil - ratio_pct

    return EvaluationRatio(
        code=ratio.code,
        libelle=ratio.libelle,
        valeur_numerateur=valeur_num,
        valeur_denominateur=valeur_denom,
        valeur_ratio_pct=ratio_pct,
        operateur=ratio.operateur,
        seuil_applicable=seuil,
        categorie_sfd_appliquee=categorie,
        conforme=conforme,
        marge=marge,
        statut=STATUT_CONFORME if conforme else STATUT_NON_CONFORME,
        avertissements=avertissements,
    )


def evaluer_tous(db: Session, a_la_date: date) -> list[EvaluationRatio]:
    """Tous les ratios ACTIFS, dans l'`ordre` de paramétrage."""
    codes = list(
        db.execute(
            select(RatioPrudentiel.code)
            .where(RatioPrudentiel.actif.is_(True))
            .order_by(RatioPrudentiel.ordre)
        ).scalars()
    )
    return [evaluer_ratio(db, code, a_la_date) for code in codes]
