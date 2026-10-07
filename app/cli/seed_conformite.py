"""Seed des ratios prudentiels RCSFD (lot P2.1.c) — ENTIÈREMENT PARAMÉTRABLE : ce fichier ne
contient AUCUNE formule câblée, seulement des DONNÉES Python converties en lignes
`conformite.agregat_prudentiel`/`agregat_compte`/`ratio_prudentiel`/`ratio_seuil`/
`parametre_institution`. Le moteur (`app/modules/conformite/moteur.py`) ne lit que la base.

NON DESTRUCTIF ET IDEMPOTENT : chaque agrégat/ratio est créé UNE SEULE FOIS (vérifié par
`code`, qui est UNIQUE) — un ré-import ne duplique rien et ne touche jamais une ligne déjà
présente (y compris si elle a été corrigée à l'écran depuis), À UNE EXCEPTION PRÈS :
la RESYNCHRONISATION de deux champs de présentation (référence réglementaire des ratios,
libellé des ratios et des agrégats placeholders), corrigés après coup dans ce fichier. Elle
ne s'applique qu'à une ligne que personne n'a retouchée, par TRIPLE GARDE : `is_system` ET
`updated_by IS NULL` (toute modification par l'écran/l'API renseigne `updated_by`) ET — pour un
libellé — valeur actuelle == ANCIEN libellé du seed, à l'identique. Une référence n'est remplie
que si elle est NULL. Aucune entrée d'audit : le seed est une opération de déploiement.

COMPOSITION DES COMPTES — validée avec l'expert, numéro par numéro, contre
`docs/reference/plan_comptable_import.csv` (390 comptes) :

  FONDS_PROPRES : capital libéré (571111), FRRG (54), primes (551), réserves (552, capture
  5521/5522/5523), fonds de dotation (56), report à nouveau (58, un seul compte — positif
  s'additionne, négatif se déduit, DÉJÀ résolu par le signe de `rapports.balance`), résultat en
  instance d'approbation (591, même remarque), subventions d'investissement — SEULEMENT 5011
  (5012 « virées au compte de résultat » EXCLU, déjà reconnu en résultat), fonds affectés (502),
  fonds de crédit (503), provisions pour risques et charges (51), provisions réglementées (52),
  emprunts et titres subordonnés (53, intégral, aucun plafonnement réglementaire modélisé).
  DÉDUIT : parts non libérées (571121), capital non appelé (5712), capital souscrit non
  appelé/versé des associés (573, capture 5731/5732). `applique_complement_provisions_tutelle`
  = TRUE (ajustement administratif, voir parametre_institution).
  EN ATTENTE, PAS SEEDÉES : déduction des immobilisations incorporelles nettes (441) et des
  participations dans d'autres SFD (412) — le plan committé n'a AUCUN compte brut pour ces
  deux familles (seulement leurs amortissements/provisions), voir la note de diagnostic P2.1.
  À reprendre après correction du plan de comptes (hors périmètre P2.0).

  RISQUES_PORTES : expositions sur les institutions financières (11, 12, 13), crédits aux
  membres/clients (20), prêts en souffrance (191/192/193/194, PAS 19 — 19 recouvrirait son
  propre compte de provision 199 et annulerait la déduction au lieu de la faire), provisions
  correspondantes DÉDUITES (199), crédits en souffrance (291/292/293/294, même raison — PAS 29),
  provisions DÉDUITES (299), titres de placement (305+307), provisions DÉDUITES (309),
  participations — versements restants + créances rattachées SEULEMENT (4126+4127, pas de
  compte brut, voir note ci-dessus), provisions DÉDUITES (4129). DÉPÔTS DE GARANTIE REÇUS
  DÉDUITS (162 côté IF, 254 côté membres/clients — réduisent le risque net, collatéral détenu).
  SOUS-COMPTES « RATTACHÉS » NEUTRALISÉS (contribution nette = 0, pas une vraie déduction) :
  1136/1146/1166/1176 (Dettes rattachées, créditrices, nichées sous 11 qui est débiteur — une
  dette n'est pas une exposition au risque).
  HORS PÉRIMÈTRE, ABSENT DU PLAN : engagements par signature et titres d'investissement (aucune
  classe hors-bilan 8/9 dans le plan committé).

  RESSOURCES : dépôts/emprunts/ressources affectées des institutions financières (15, 16, 17,
  18), dépôts/emprunts des membres et clients (25, 27), créditeurs divers (332), comptes
  d'attente passif (3792), régularisation passif (382), PLUS la même composition que
  FONDS_PROPRES (additions et déductions identiques — une ressource de financement inclut les
  fonds propres). SOUS-COMPTES RATTACHÉS NEUTRALISÉS : 1547/1567/1577 (Créances rattachées,
  débitrices, nichées sous 15 qui est créditeur) et 25117 (même raison, sous 25).

RATIOS :
  #1, #5 et #8 ACTIFS (seuils validés). #2, #3, #4, #6, #7, #9, #10 créés `actif=FALSE`,
  « en attente » — chacun référence des agrégats-placeholders à composition VIDE (un agrégat
  BALANCE sans aucune ligne `agregat_compte` rend 0, jamais une exception — voir
  `moteur._valeur_balance`), SAUF #2 qui réutilise FONDS_PROPRES côté numérateur. AUCUN seuil
  n'est seedé pour ces 7 : je n'ai pas de valeur validée, et CLAUDE.md interdit d'inventer une
  valeur de configuration.

  #8 (limitation des titres de participation, <= 25 % des fonds propres) — CÂBLÉ en P2.0-b2 :
  numérateur PARTICIPATIONS_HORS_SFD_EC = 412300 (+1) - 412930 (-1), dénominateur FONDS_PROPRES
  réutilisé tel quel. 412930 est la provision DÉDIE au bucket « hors SFD et établissements de
  crédit » (ventilation de 4129, P2.0-b1-ter) : #8 déduit exactement SA provision. Entre P2.0-b2
  et cette correction, #8 déduisait TOUTE la provision globale 4129 : pour un plafond (<=), un
  numérateur plus petit REND LE RATIO PLUS FAVORABLE, donc cette approximation SOUS-ESTIMAIT le
  risque (elle n'était pas prudente) ; elle est supprimée. Une provision saisie sur 412910
  (bucket SFD/établissements de crédit) n'affecte donc plus #8.
  Préfixes disjoints : 412300 ne recouvre pas 412930, ni l'inverse. Le brut 412100
  (SFD + établissements de crédit) est volontairement ABSENT : il est déduit des fonds propres
  et exclu de #8. 4126/4127 (versements restant à effectuer, créances rattachées) ne sont pas
  inclus : leur rôle reste à confirmer par l'expert. #10 a un agrégat SPECIAL de part et d'autre
  (`DOTATION_RESERVE_GENERALE_PERIODE`/`EXCEDENT_PERIODE`) dont la fonction n'est PAS encore
  écrite dans le moteur — lèverait `CalculSpecialInconnuError` si jamais évalué directement,
  sans risque tant que `actif=FALSE` (jamais atteint par `evaluer_tous`).
"""

from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.conformite.models import (
    AgregatCompte,
    AgregatPrudentiel,
    ParametreInstitution,
    RatioPrudentiel,
    RatioSeuil,
)

# Valeur exacte, volontairement sans article (on affinera les articles plus tard).
REFERENCE_REGLEMENTAIRE = "Instruction 010-08-2010"


@dataclass(frozen=True)
class _CompositionLigne:
    prefixe: str
    sens: int


@dataclass(frozen=True)
class _CablageAgregatPrecedent:
    """État que ce seed écrivait AVANT la correction d'un agrégat : seul un agrégat encore dans
    cet état exact est recâblé sur une base existante (voir `_recabler_agregat`)."""

    composition: Sequence[_CompositionLigne]
    reference: str | None


@dataclass(frozen=True)
class _AgregatDef:
    code: str
    libelle: str
    reference: str | None = None
    type: str = "BALANCE"
    calcul_special: str | None = None
    nets_de_provisions: bool = False
    applique_complement_provisions_tutelle: bool = False
    composition: Sequence[_CompositionLigne] = field(default_factory=tuple)
    # Libellé que ce seed écrivait AVANT correction : seul un libellé strictement égal à celui-ci
    # est resynchronisé (voir docstring de module).
    ancien_libelle: str | None = None
    cablage_precedent: _CablageAgregatPrecedent | None = None


@dataclass(frozen=True)
class _SeuilDef:
    categorie_sfd: str | None
    valeur: int


@dataclass(frozen=True)
class _CablagePrecedent:
    """État que ce seed écrivait AVANT le câblage d'un ratio : seul un ratio encore dans cet état
    exact est recâblé sur une base existante (voir `_recabler_ratio`)."""

    numerateur: str
    actif: bool
    reference: str | None


@dataclass(frozen=True)
class _RatioDef:
    code: str
    libelle: str
    numerateur: str
    denominateur: str
    operateur: str
    ordre: int
    actif: bool = True
    reference_reglementaire: str | None = REFERENCE_REGLEMENTAIRE
    ancien_libelle: str | None = None
    cablage_precedent: _CablagePrecedent | None = None
    seuils: Sequence[_SeuilDef] = field(default_factory=tuple)


def _c(prefixe: str, sens: int) -> _CompositionLigne:
    return _CompositionLigne(prefixe, sens)


_FONDS_PROPRES_COMPOSITION = (
    _c("571111", 1), _c("54", 1), _c("551", 1), _c("552", 1), _c("56", 1),
    _c("58", 1), _c("591", 1), _c("5011", 1), _c("502", 1), _c("503", 1),
    _c("51", 1), _c("52", 1), _c("53", 1),
    _c("571121", -1), _c("5712", -1), _c("573", -1),
)

AGREGATS: tuple[_AgregatDef, ...] = (
    _AgregatDef(
        code="FONDS_PROPRES",
        libelle="Fonds propres effectifs",
        reference="Instruction BCEAO 010-08-2010 — définition large, hors immobilisations "
        "incorporelles nettes et participations dans d'autres SFD (en attente, voir docstring)",
        applique_complement_provisions_tutelle=True,
        composition=_FONDS_PROPRES_COMPOSITION,
    ),
    _AgregatDef(
        code="RISQUES_PORTES",
        libelle="Risques portés (nets de provisions et dépôts de garantie)",
        composition=(
            _c("11", 1), _c("12", 1), _c("13", 1), _c("20", 1),
            _c("191", 1), _c("192", 1), _c("193", 1), _c("194", 1), _c("199", -1),
            _c("291", 1), _c("292", 1), _c("293", 1), _c("294", 1), _c("299", -1),
            _c("305", 1), _c("307", 1), _c("309", -1),
            _c("4126", 1), _c("4127", 1), _c("4129", -1),
            _c("162", -1), _c("254", -1),
            # Neutralisation des rattachés à sens opposé nichés sous 11 (débiteur) : des
            # dettes (créditrices) qui ne sont pas une exposition au risque. Contribution
            # nette = 0, PAS une vraie déduction (voir docstring de module).
            _c("1136", -1), _c("1146", -1), _c("1166", -1), _c("1176", -1),
        ),
    ),
    _AgregatDef(
        code="RESSOURCES",
        libelle="Ressources (comptes créditeurs, emprunts, dépôts, fonds propres)",
        composition=(
            _c("15", 1), _c("16", 1), _c("17", 1), _c("18", 1), _c("25", 1), _c("27", 1),
            _c("332", 1), _c("3792", 1), _c("382", 1),
            # Neutralisation des rattachés à sens opposé : des créances (débitrices) nichées
            # sous 15/25 (créditeurs) qui ne sont pas une ressource de financement.
            _c("1547", -1), _c("1567", -1), _c("1577", -1), _c("25117", -1),
            *_FONDS_PROPRES_COMPOSITION,
        ),
    ),
    _AgregatDef(
        code="ENCOURS_PLUS_GROS_EMPRUNTEUR",
        libelle="Encours du plus gros emprunteur",
        type="SPECIAL",
        calcul_special="PLUS_GROS_EMPRUNTEUR",
    ),
    _AgregatDef(
        code="PARTICIPATIONS_HORS_SFD_EC",
        libelle="Participations hors SFD et établissements de crédit (nettes de provisions)",
        reference="Instruction 010-08-2010 et 016-12-2010 — brut 412300 moins sa provision "
        "dédiée 412930 (provision des titres hors SFD et établissements de crédit)",
        nets_de_provisions=True,
        composition=(_c("412300", 1), _c("412930", -1)),
        cablage_precedent=_CablageAgregatPrecedent(
            composition=(_c("412300", 1), _c("4129", -1)),
            reference=(
                "Instruction 010-08-2010 et 016-12-2010 — brut 412300 moins TOUTE la "
                "provision 4129 (compte global unique) : approximation prudente"
            ),
        ),
    ),
    # --- Placeholders « en attente » (lot P2.1.c) — composition VIDE à dessein : un agrégat
    # BALANCE sans ligne rend 0, jamais une exception (voir moteur._valeur_balance). Les 8
    # ratios qui les référencent sont actif=FALSE, jamais évalués par evaluer_tous().
    # L'état « en attente » se lit au badge de l'écran, pas dans le libellé ; le MOTIF de
    # chaque écart est conservé ici, en commentaire.
    # Total actif net : composition à définir.
    _AgregatDef(
        code="TOTAL_ACTIF_NET",
        libelle="Total actif net",
        ancien_libelle="Total actif net (en attente de composition)",
    ),
    # Durée résiduelle des crédits non encore exploitée.
    _AgregatDef(
        code="EMPLOIS_MLT",
        libelle="Emplois à moyen et long terme",
        ancien_libelle="Emplois à moyen et long terme (en attente — "
        "durée résiduelle des crédits non encore exploitée)",
    ),
    # Durée contractuelle absente sur les produits d'épargne à terme.
    _AgregatDef(
        code="RESSOURCES_STABLES",
        libelle="Ressources stables",
        ancien_libelle="Ressources stables (en attente — durée "
        "contractuelle absente sur les produits d'épargne à terme)",
    ),
    # Aucun marqueur dirigeant/personnel en base.
    _AgregatDef(
        code="PRETS_DIRIGEANTS_PERSONNEL",
        libelle="Prêts aux dirigeants et au personnel",
        ancien_libelle="Prêts aux dirigeants et au "
        "personnel (en attente — aucun marqueur dirigeant/personnel en base)",
    ),
    # Même gap que les ressources stables.
    _AgregatDef(
        code="VALEURS_REALISABLES_DISPONIBLES",
        libelle="Valeurs réalisables et disponibles",
        ancien_libelle="Valeurs réalisables et "
        "disponibles (en attente — même gap que ressources stables)",
    ),
    _AgregatDef(
        code="PASSIF_EXIGIBLE",
        libelle="Passif exigible à court terme",
        ancien_libelle="Passif exigible à court terme (en attente)",
    ),
    # Définition du numérateur à vérifier contre le texte réglementaire.
    _AgregatDef(
        code="OPERATIONS_AUTRES",
        libelle="Opérations autres qu'épargne et crédit",
        ancien_libelle="Opérations autres qu'épargne et crédit "
        "(en attente — définition du numérateur à vérifier contre le texte réglementaire)",
    ),
    # Même gap que les participations.
    _AgregatDef(
        code="IMMOS_PLUS_PARTICIPATIONS",
        libelle="Immobilisations nettes + participations",
        ancien_libelle="Immobilisations nettes + "
        "participations (en attente — même gap)",
    ),
    # Calcul SPECIAL non écrit : flux de la dernière affectation, pas un solde cumulé.
    _AgregatDef(
        code="DOTATION_RESERVE_GENERALE_PERIODE",
        libelle="Dotation à la réserve générale sur la période",
        ancien_libelle="Dotation à la réserve générale sur la période (en attente — calcul "
        "SPECIAL non écrit, flux de la dernière affectation, pas un solde cumulé)",
        type="SPECIAL",
        calcul_special="DOTATION_RESERVE_GENERALE_PERIODE",
    ),
    # Calcul SPECIAL non écrit.
    _AgregatDef(
        code="EXCEDENT_PERIODE",
        libelle="Excédent de la période",
        ancien_libelle="Excédent de la période (en attente — calcul SPECIAL non écrit)",
        type="SPECIAL",
        calcul_special="EXCEDENT_PERIODE",
    ),
)

RATIOS: tuple[_RatioDef, ...] = (
    _RatioDef(
        code="RATIO_1_COUVERTURE_RISQUES",
        libelle="Couverture des risques portés par les ressources",
        numerateur="RISQUES_PORTES",
        denominateur="RESSOURCES",
        operateur="LE",
        ordre=1,
        seuils=(_SeuilDef(categorie_sfd=None, valeur=200),),
    ),
    _RatioDef(
        code="RATIO_2_CAPITALISATION",
        libelle="Capitalisation générale",
        ancien_libelle="Capitalisation générale (en attente)",
        numerateur="FONDS_PROPRES",
        denominateur="TOTAL_ACTIF_NET",
        operateur="GE",
        ordre=2,
        actif=False,
    ),
    _RatioDef(
        code="RATIO_3_COUVERTURE_EMPLOIS_MLT",
        libelle="Couverture des emplois à moyen et long terme par des ressources stables",
        ancien_libelle="Couverture des emplois à moyen et long terme par des ressources stables "
        "(en attente)",
        numerateur="EMPLOIS_MLT",
        denominateur="RESSOURCES_STABLES",
        operateur="LE",
        ordre=3,
        actif=False,
    ),
    _RatioDef(
        code="RATIO_4_LIMITATION_PRETS_DIRIGEANTS",
        libelle="Limitation des prêts aux dirigeants et au personnel",
        ancien_libelle="Limitation des prêts aux dirigeants et au personnel (en attente)",
        numerateur="PRETS_DIRIGEANTS_PERSONNEL",
        denominateur="FONDS_PROPRES",
        operateur="LE",
        ordre=4,
        actif=False,
    ),
    _RatioDef(
        code="RATIO_5_DIVISION_RISQUES",
        libelle="Division des risques (plus gros emprunteur)",
        numerateur="ENCOURS_PLUS_GROS_EMPRUNTEUR",
        denominateur="FONDS_PROPRES",
        operateur="LE",
        ordre=5,
        seuils=(_SeuilDef(categorie_sfd=None, valeur=10),),
    ),
    _RatioDef(
        code="RATIO_6_LIQUIDITE",
        libelle="Liquidité",
        ancien_libelle="Liquidité (en attente)",
        numerateur="VALEURS_REALISABLES_DISPONIBLES",
        denominateur="PASSIF_EXIGIBLE",
        operateur="GE",
        ordre=6,
        actif=False,
    ),
    _RatioDef(
        code="RATIO_7_OPERATIONS_AUTRES",
        libelle="Limitation des opérations autres qu'épargne et crédit",
        ancien_libelle="Limitation des opérations autres qu'épargne et crédit (en attente)",
        numerateur="OPERATIONS_AUTRES",
        denominateur="TOTAL_ACTIF_NET",
        operateur="LE",
        ordre=7,
        actif=False,
    ),
    _RatioDef(
        code="RATIO_8_LIMITATION_PARTICIPATIONS",
        libelle="Limitation des participations",
        ancien_libelle="Limitation des participations (en attente)",
        numerateur="PARTICIPATIONS_HORS_SFD_EC",
        denominateur="FONDS_PROPRES",
        operateur="LE",
        ordre=8,
        reference_reglementaire="Instruction 010-08-2010 (limitation des titres de "
        "participation) + Instruction 016-12-2010",
        cablage_precedent=_CablagePrecedent(
            numerateur="PARTICIPATIONS",
            actif=False,
            reference=REFERENCE_REGLEMENTAIRE,
        ),
        seuils=(_SeuilDef(categorie_sfd=None, valeur=25),),
    ),
    _RatioDef(
        code="RATIO_9_IMMOS_PLUS_PARTICIPATIONS",
        libelle="Limitation immobilisations + participations",
        ancien_libelle="Limitation immobilisations + participations (en attente)",
        numerateur="IMMOS_PLUS_PARTICIPATIONS",
        denominateur="FONDS_PROPRES",
        operateur="LE",
        ordre=9,
        actif=False,
    ),
    _RatioDef(
        code="RATIO_10_RESERVE_GENERALE",
        libelle="Dotation minimale à la réserve générale",
        ancien_libelle="Dotation minimale à la réserve générale (en attente)",
        numerateur="DOTATION_RESERVE_GENERALE_PERIODE",
        denominateur="EXCEDENT_PERIODE",
        operateur="GE",
        ordre=10,
        actif=False,
    ),
)


@dataclass
class RapportSeedConformite:
    agregats_crees: int = 0
    ratios_crees: int = 0
    seuils_crees: int = 0
    parametre_institution_cree: bool = False
    # Resynchronisation de présentation sur des lignes déjà en base (voir docstring de module).
    references_resynchronisees: int = 0
    libelles_resynchronises: int = 0
    # Ratios recâblés sur une base existante (agrégat numérateur, activation, seuil, référence).
    ratios_recables: int = 0
    # Agrégats dont la composition a été recâblée sur une base existante.
    agregats_recables: int = 0


def _non_retouche(ligne: AgregatPrudentiel | RatioPrudentiel) -> bool:
    """Première garde : ligne système que PERSONNE n'a modifiée (toute modification par
    l'écran ou l'API renseigne `updated_by`)."""
    return ligne.is_system and ligne.updated_by is None


def _resynchroniser_libelle(
    ligne: AgregatPrudentiel | RatioPrudentiel,
    *,
    libelle: str,
    ancien_libelle: str | None,
    rapport: RapportSeedConformite,
) -> None:
    """Troisième garde : seul un libellé strictement égal à l'ANCIEN libellé du seed est
    remplacé — un libellé modifié à la main ne lui est jamais égal."""
    if ancien_libelle is not None and _non_retouche(ligne) and ligne.libelle == ancien_libelle:
        ligne.libelle = libelle
        rapport.libelles_resynchronises += 1


def _recabler_ratio(
    db: Session,
    ratio: RatioPrudentiel,
    definition: _RatioDef,
    agregats_par_code: dict[str, AgregatPrudentiel],
    rapport: RapportSeedConformite,
) -> None:
    """Recâble un ratio EXISTANT sur le câblage courant du seed — seulement s'il est encore dans
    l'état exact que ce seed écrivait avant (`cablage_precedent`) : ligne système jamais retouchée
    (`updated_by IS NULL`), même agrégat numérateur, même état d'activation, AUCUN seuil posé.
    Dès qu'une de ces conditions est fausse, quelqu'un a commencé à paramétrer ce ratio :
    on n'y touche pas. La référence n'est remplacée que si elle est vide ou égale à l'ancienne."""
    precedent = definition.cablage_precedent
    if precedent is None or not _non_retouche(ratio) or ratio.actif != precedent.actif:
        return
    numerateur_actuel = db.get(AgregatPrudentiel, ratio.agregat_numerateur_id)
    if numerateur_actuel is None or numerateur_actuel.code != precedent.numerateur:
        return
    if db.execute(select(RatioSeuil.id).where(RatioSeuil.ratio_id == ratio.id)).first():
        return

    ratio.agregat_numerateur_id = agregats_par_code[definition.numerateur].id
    ratio.actif = definition.actif
    if ratio.reference_reglementaire in (None, precedent.reference):
        ratio.reference_reglementaire = definition.reference_reglementaire
    for seuil in definition.seuils:
        db.add(
            RatioSeuil(
                ratio_id=ratio.id,
                categorie_sfd=seuil.categorie_sfd,
                valeur_seuil=seuil.valeur,
                is_system=True,
            )
        )
        rapport.seuils_crees += 1
    db.flush()
    rapport.ratios_recables += 1


def _recabler_agregat(
    db: Session,
    agregat: AgregatPrudentiel,
    definition: _AgregatDef,
    rapport: RapportSeedConformite,
) -> None:
    """Recâble la COMPOSITION d'un agrégat EXISTANT sur le câblage courant du seed — seulement
    s'il est encore dans l'état exact que ce seed écrivait avant (`cablage_precedent`) : agrégat
    système jamais retouché (`updated_by IS NULL` — l'API de paramétrage le renseigne dès qu'elle
    remplace la composition) ET composition strictement égale à l'ancienne, lignes toutes
    système. Dès qu'une condition est fausse, quelqu'un a commencé à paramétrer cet agrégat :
    on n'y touche pas. La référence n'est remplacée que si elle est vide ou égale à l'ancienne."""
    precedent = definition.cablage_precedent
    if precedent is None or not _non_retouche(agregat):
        return
    lignes = list(
        db.execute(select(AgregatCompte).where(AgregatCompte.agregat_id == agregat.id)).scalars()
    )
    actuelle = {(ligne.prefixe_compte, ligne.sens) for ligne in lignes}
    if not all(ligne.is_system for ligne in lignes) or actuelle != {
        (ligne.prefixe, ligne.sens) for ligne in precedent.composition
    }:
        return

    cible = {(ligne.prefixe, ligne.sens) for ligne in definition.composition}
    for ligne in lignes:
        if (ligne.prefixe_compte, ligne.sens) not in cible:
            db.delete(ligne)
    for prefixe, sens in sorted(cible - actuelle):
        db.add(
            AgregatCompte(
                agregat_id=agregat.id, prefixe_compte=prefixe, sens=sens, is_system=True
            )
        )
    if agregat.reference in (None, precedent.reference):
        agregat.reference = definition.reference
    db.flush()
    rapport.agregats_recables += 1


def executer_seed_conformite(db: Session) -> RapportSeedConformite:
    """Non destructif, idempotent — ne touche jamais une ligne déjà présente (vérifié par
    `code`, UNIQUE), sauf la resynchronisation gardée des champs de présentation (voir
    docstring de module). Ne committe pas : l'appelant décide."""
    rapport = RapportSeedConformite()
    agregats_par_code: dict[str, AgregatPrudentiel] = {}

    for definition in AGREGATS:
        existant = db.execute(
            select(AgregatPrudentiel).where(AgregatPrudentiel.code == definition.code)
        ).scalar_one_or_none()
        if existant is not None:
            _recabler_agregat(db, existant, definition, rapport)
            _resynchroniser_libelle(
                existant,
                libelle=definition.libelle,
                ancien_libelle=definition.ancien_libelle,
                rapport=rapport,
            )
            agregats_par_code[definition.code] = existant
            continue

        agregat = AgregatPrudentiel(
            code=definition.code,
            libelle=definition.libelle,
            reference=definition.reference,
            type=definition.type,
            calcul_special=definition.calcul_special,
            nets_de_provisions=definition.nets_de_provisions,
            applique_complement_provisions_tutelle=(
                definition.applique_complement_provisions_tutelle
            ),
            is_system=True,
        )
        db.add(agregat)
        db.flush()
        for ligne in definition.composition:
            db.add(
                AgregatCompte(
                    agregat_id=agregat.id,
                    prefixe_compte=ligne.prefixe,
                    sens=ligne.sens,
                    is_system=True,
                )
            )
        agregats_par_code[definition.code] = agregat
        rapport.agregats_crees += 1

    db.flush()

    for ratio_def in RATIOS:
        ratio_existant = db.execute(
            select(RatioPrudentiel).where(RatioPrudentiel.code == ratio_def.code)
        ).scalar_one_or_none()
        if ratio_existant is not None:
            _recabler_ratio(db, ratio_existant, ratio_def, agregats_par_code, rapport)
            if (
                _non_retouche(ratio_existant)
                and ratio_existant.reference_reglementaire is None
                and ratio_def.reference_reglementaire is not None
            ):
                ratio_existant.reference_reglementaire = ratio_def.reference_reglementaire
                rapport.references_resynchronisees += 1
            _resynchroniser_libelle(
                ratio_existant,
                libelle=ratio_def.libelle,
                ancien_libelle=ratio_def.ancien_libelle,
                rapport=rapport,
            )
            continue

        ratio = RatioPrudentiel(
            code=ratio_def.code,
            libelle=ratio_def.libelle,
            reference_reglementaire=ratio_def.reference_reglementaire,
            agregat_numerateur_id=agregats_par_code[ratio_def.numerateur].id,
            agregat_denominateur_id=agregats_par_code[ratio_def.denominateur].id,
            operateur=ratio_def.operateur,
            actif=ratio_def.actif,
            ordre=ratio_def.ordre,
            is_system=True,
        )
        db.add(ratio)
        db.flush()
        for seuil in ratio_def.seuils:
            db.add(
                RatioSeuil(
                    ratio_id=ratio.id,
                    categorie_sfd=seuil.categorie_sfd,
                    valeur_seuil=seuil.valeur,
                    is_system=True,
                )
            )
            rapport.seuils_crees += 1
        rapport.ratios_crees += 1

    if db.execute(select(ParametreInstitution)).first() is None:
        db.add(ParametreInstitution(categorie_sfd="NON_AFFILIE", complement_provisions_tutelle=0))
        rapport.parametre_institution_cree = True

    return rapport
