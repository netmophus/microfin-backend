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

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.comptabilite import rapports
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


class AgregatIntrouvableError(Exception):
    """Le code d'agrégat demandé n'existe pas dans `conformite.agregat_prudentiel`."""


class RatioIntrouvableError(Exception):
    """Le code de ratio demandé n'existe pas dans `conformite.ratio_prudentiel`."""


class CalculSpecialInconnuError(Exception):
    """Un agrégat SPECIAL référence un `calcul_special` sans fonction enregistrée pour lui —
    paramétrage incomplet, jamais un crash silencieux ailleurs."""


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


_CALCULS_SPECIAUX: dict[str, Callable[[Session, date], int]] = {
    "PLUS_GROS_EMPRUNTEUR": _encours_plus_gros_emprunteur,
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
