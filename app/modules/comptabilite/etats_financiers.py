"""Bilan et compte de résultat RCSFD — chantier P1, dernier lot (c).

Couche MINCE au-dessus de `rapports.balance` (jamais modifié) et de la table de mapping
`comptabilite.financial_statement_mapping` (compte -> poste, chantier précédent, seedée par
`app/cli/seed_financial_statement_mapping.py`).

BILAN : `rapports.balance(date_debut=None, date_fin=<date>)` donne le solde CUMULÉ (depuis
l'origine) de chaque compte de bilan à une date — exactement ce qu'un bilan réclame. Les postes
`CONTRA_ACTIF` (provisions, amortissements) sont retranchés du total ACTIF, jamais ajoutés au
passif — c'est la clé de l'équilibre (décision actée). Les 2 lignes `MIXTE` du mapping (comptes
`33`/`38`, regroupements purs, `is_posting=FALSE`) n'apparaissent jamais dans `balance()` (qui ne
retourne que des comptes de saisie) — ignorées par construction, jamais une vraie masse.

COMPTE DE RÉSULTAT, EXERCICE EN COURS : agrégation des classes 6/7 (mapping `etat='RESULTAT'`)
sur `[exercice.date_debut, min(aujourd'hui, exercice.date_fin)]` via `balance()`.

COMPTE DE RÉSULTAT, EXERCICE CLOS : les classes 6/7 sont soldées à zéro par la clôture (b1) —
`balance()` n'y lirait plus rien. Le résultat net est RE-DÉRIVÉ depuis la ligne 591 de LA PIÈCE DE
CLÔTURE de cet exercice précis, via `affectation_resultat._piece_de_cloture` /
`_montant_a_affecter` — jamais le solde courant agrégé de 591 (même raison qu'en b2a : 591 est un
compte global, non scopé par exercice). Le détail par poste n'est alors PLUS disponible (les
lignes d'origine ont été reversées par la clôture) — `charges`/`produits` reviennent vides,
`source_resultat='cloture'` le signale explicitement au lieu de laisser croire à un détail réel.

COMPTES NON MAPPÉS : tout compte renvoyé par `balance()` (donc avec un mouvement sur la fenêtre
demandée) mais absent de la table de mapping est listé dans `comptes_non_mappes` — JAMAIS ignoré
silencieusement (décision actée). Peut arriver si un compte est créé après le dernier seed du
mapping.
"""

import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.modules.comptabilite import affectation_resultat, rapports
from app.modules.comptabilite.models import Account, Exercice, FinancialStatementMapping

MASSES_ACTIF = ("ACTIF", "CONTRA_ACTIF")


@dataclass(frozen=True)
class LignePoste:
    poste_libelle: str
    poste_ordre: int
    masse: str
    montant: int  # toujours positif ; le sens (ajout/déduction) est porté par `masse`


@dataclass(frozen=True)
class CompteNonMappe:
    account_number: str
    name: str
    account_class: int
    solde: int


@dataclass(frozen=True)
class Bilan:
    date: date
    actif: list[LignePoste]  # ACTIF et CONTRA_ACTIF mêlés, triés par poste_ordre
    passif: list[LignePoste]
    total_actif_brut: int
    total_contra_actif: int
    total_actif_net: int
    total_passif: int
    ecart: int
    comptes_non_mappes: list[CompteNonMappe]

    @property
    def equilibre(self) -> bool:
        return self.ecart == 0


@dataclass(frozen=True)
class CompteResultat:
    exercice_id: uuid.UUID
    date_debut: date
    date_fin: date
    exercice_clos: bool
    charges: list[LignePoste]
    produits: list[LignePoste]
    total_charges: int
    total_produits: int
    resultat_net: int
    source_resultat: str  # 'periode' | 'cloture'
    comptes_non_mappes: list[CompteNonMappe]


def _lignes_mappees(
    db: Session, *, date_debut: date | None, date_fin: date | None, etat: str
) -> tuple[list[tuple[str, str, int, int]], list[CompteNonMappe]]:
    """Σ par compte sur la fenêtre, croisée avec le mapping. Rend (masse, poste_libelle,
    poste_ordre, montant) pour chaque compte dont le mapping correspond à `etat`, et la liste des
    comptes mouvementés sans AUCUNE ligne de mapping (quel que soit leur etat)."""
    resultat = rapports.balance(
        db, date_debut=date_debut, date_fin=date_fin, inclure_sans_mouvement=False
    )
    mapping_par_compte = {
        m.account_id: m for m in db.execute(select(FinancialStatementMapping)).scalars()
    }

    lignes: list[tuple[str, str, int, int]] = []
    non_mappes: list[CompteNonMappe] = []
    for ligne in resultat.lignes:
        compte = ligne.compte
        mapping = mapping_par_compte.get(compte.id)
        montant = int(ligne.solde_cloture)  # défensif : SUM() Postgres peut revenir en Decimal
        if mapping is None:
            non_mappes.append(
                CompteNonMappe(
                    account_number=compte.account_number,
                    name=compte.name,
                    account_class=compte.account_class,
                    solde=montant,
                )
            )
            continue
        if mapping.etat != etat or mapping.masse == "MIXTE":
            continue
        lignes.append((mapping.masse, mapping.poste_libelle, mapping.poste_ordre, montant))
    return lignes, non_mappes


def _regrouper_par_poste(lignes: list[tuple[str, str, int, int]]) -> list[LignePoste]:
    """Agrège les montants par (masse, poste), triés par ordre d'affichage."""
    postes: dict[tuple[str, str, int], int] = {}
    for masse, poste_libelle, poste_ordre, montant in lignes:
        cle = (masse, poste_libelle, poste_ordre)
        postes[cle] = postes.get(cle, 0) + montant
    return sorted(
        (
            LignePoste(poste_libelle=poste, poste_ordre=ordre, masse=masse, montant=montant)
            for (masse, poste, ordre), montant in postes.items()
        ),
        key=lambda ligne: ligne.poste_ordre,
    )


def bilan(db: Session, a_la_date: date | None = None) -> Bilan:
    """Bilan à une date (par défaut : aujourd'hui) : solde cumulé (depuis l'origine) de chaque
    compte de bilan."""
    if a_la_date is None:
        a_la_date = db.execute(text("SELECT CURRENT_DATE")).scalar_one()
    lignes, non_mappes = _lignes_mappees(db, date_debut=None, date_fin=a_la_date, etat="BILAN")
    postes = _regrouper_par_poste(lignes)

    actif = [p for p in postes if p.masse in MASSES_ACTIF]
    passif = [p for p in postes if p.masse == "PASSIF"]

    total_actif_brut = sum(p.montant for p in actif if p.masse == "ACTIF")
    total_contra_actif = sum(p.montant for p in actif if p.masse == "CONTRA_ACTIF")
    total_actif_net = total_actif_brut - total_contra_actif
    total_passif = sum(p.montant for p in passif)

    return Bilan(
        date=a_la_date,
        actif=actif,
        passif=passif,
        total_actif_brut=total_actif_brut,
        total_contra_actif=total_contra_actif,
        total_actif_net=total_actif_net,
        total_passif=total_passif,
        ecart=total_actif_net - total_passif,
        comptes_non_mappes=non_mappes,
    )


def compte_resultat(db: Session, exercice: Exercice) -> CompteResultat:
    """Compte de résultat sur un exercice — en cours (agrégation 6/7 sur la période) ou clos
    (résultat re-dérivé depuis la pièce de clôture, détail par poste indisponible)."""
    if exercice.status == "clos":
        ligne_591 = affectation_resultat._piece_de_cloture(db, exercice)
        resultat_net = (
            affectation_resultat._montant_a_affecter(ligne_591)
            if ligne_591 is not None
            else 0
        )
        return CompteResultat(
            exercice_id=exercice.id,
            date_debut=exercice.date_debut,
            date_fin=exercice.date_fin,
            exercice_clos=True,
            charges=[],
            produits=[],
            total_charges=0,
            total_produits=0,
            resultat_net=resultat_net,
            source_resultat="cloture",
            comptes_non_mappes=[],
        )

    aujourdhui = db.execute(text("SELECT CURRENT_DATE")).scalar_one()
    date_fin = min(aujourdhui, exercice.date_fin)
    lignes, non_mappes = _lignes_mappees(
        db, date_debut=exercice.date_debut, date_fin=date_fin, etat="RESULTAT"
    )
    postes = _regrouper_par_poste(lignes)

    charges = [p for p in postes if p.masse == "CHARGE"]
    produits = [p for p in postes if p.masse == "PRODUIT"]
    total_charges = sum(p.montant for p in charges)
    total_produits = sum(p.montant for p in produits)

    return CompteResultat(
        exercice_id=exercice.id,
        date_debut=exercice.date_debut,
        date_fin=date_fin,
        exercice_clos=False,
        charges=charges,
        produits=produits,
        total_charges=total_charges,
        total_produits=total_produits,
        resultat_net=total_produits - total_charges,
        source_resultat="periode",
        comptes_non_mappes=non_mappes,
    )


# --- Administration du mapping ----------------------------------------------------------------
# CRUD minimal : lister + modifier une ligne. Toute modification pose `gere_manuellement = TRUE`
# (voir seed_financial_statement_mapping.py) : le prochain seed ne réécrira plus cette ligne.


class MappingIntrouvableError(Exception):
    """Pas de ligne de mapping pour ce compte — le seed n'a jamais été joué, ou l'id est faux."""


def lister_mapping(db: Session) -> list[tuple[Account, FinancialStatementMapping]]:
    return [
        (compte, mapping)
        for compte, mapping in db.execute(
            select(Account, FinancialStatementMapping)
            .join(FinancialStatementMapping, FinancialStatementMapping.account_id == Account.id)
            .order_by(
                FinancialStatementMapping.etat,
                FinancialStatementMapping.masse,
                FinancialStatementMapping.poste_ordre,
            )
        ).all()
    ]


def modifier_mapping(
    db: Session,
    account_id: uuid.UUID,
    *,
    etat: str,
    masse: str,
    poste_libelle: str,
    poste_ordre: int,
    par: uuid.UUID | None,
) -> FinancialStatementMapping:
    """Ajuste une ligne À LA MAIN — pose `gere_manuellement = TRUE`, verrouillée contre le seed
    jusqu'à une future réinitialisation (pas codée dans ce lot, voir security.roles pour le
    précédent si un jour nécessaire)."""
    ligne = db.get(FinancialStatementMapping, account_id)
    if ligne is None:
        raise MappingIntrouvableError(f"aucune ligne de mapping pour le compte {account_id}.")
    ligne.etat = etat
    ligne.masse = masse
    ligne.poste_libelle = poste_libelle
    ligne.poste_ordre = poste_ordre
    ligne.gere_manuellement = True
    ligne.updated_by = par
    db.flush()
    return ligne


# --- Héritage du mapping à la création d'un compte --------------------------------------------
# Un compte créé à l'écran n'a aucune ligne de mapping (le seed ne lit que le CSV) : il sortirait
# du bilan. Il hérite donc de la ligne de son parent — même esprit que l'héritage du sens.


@dataclass(frozen=True)
class RapportRattrapageMapping:
    crees: list[tuple[str, str, str]]  # (compte, parent, poste)
    ignores: list[tuple[str, str]]  # (compte, motif)


def _copier_mapping(
    db: Session, compte_id: uuid.UUID, modele: FinancialStatementMapping, par: uuid.UUID | None
) -> None:
    # gere_manuellement reste FALSE : le seed et l'écran d'admin gardent le droit de l'ajuster.
    db.add(
        FinancialStatementMapping(
            account_id=compte_id,
            etat=modele.etat,
            masse=modele.masse,
            poste_libelle=modele.poste_libelle,
            poste_ordre=modele.poste_ordre,
            gere_manuellement=False,
            created_by=par,
            updated_by=par,
        )
    )


def heriter_mapping_du_parent(db: Session, compte: Account, par: uuid.UUID | None) -> str:
    """Donne au compte la ligne de mapping de son parent, si le parent en a une. Rend la mention
    d'audit. Aucune ligne n'est devinée si le parent n'est pas mappé."""
    parent = db.get(Account, compte.parent_id) if compte.parent_id is not None else None
    modele = db.get(FinancialStatementMapping, parent.id) if parent is not None else None
    if parent is None or modele is None:
        return "sans mapping (parent non mappé)"
    _copier_mapping(db, compte.id, modele, par)
    db.flush()
    return f"mapping hérité de {parent.account_number} : {modele.poste_libelle}"


def rattraper_mapping_orphelins(
    db: Session, par: uuid.UUID | None = None
) -> RapportRattrapageMapping:
    """Applique la règle d'héritage aux comptes déjà en base sans mapping. Parents traités avant
    leurs enfants (ordre de numéro) : une chaîne d'orphelins se mappe d'un seul passage."""
    orphelins = db.execute(
        select(Account)
        .outerjoin(FinancialStatementMapping, FinancialStatementMapping.account_id == Account.id)
        .where(FinancialStatementMapping.account_id.is_(None))
        .order_by(Account.account_number)
    ).scalars().all()
    crees: list[tuple[str, str, str]] = []
    ignores: list[tuple[str, str]] = []
    for compte in orphelins:
        parent = db.get(Account, compte.parent_id) if compte.parent_id is not None else None
        modele = db.get(FinancialStatementMapping, parent.id) if parent is not None else None
        if parent is None or modele is None:
            motif = "sans parent" if parent is None else f"parent {parent.account_number} non mappé"
            ignores.append((compte.account_number, motif))
            continue
        _copier_mapping(db, compte.id, modele, par)
        db.flush()
        crees.append((compte.account_number, parent.account_number, modele.poste_libelle))
    return RapportRattrapageMapping(crees=crees, ignores=ignores)
