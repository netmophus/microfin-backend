# Conformité LBC/FT — valeurs réglementaires à faire valider

> Ce document liste toutes les valeurs **réglementaires** du module (scores, seuils, critères,
> niveaux de vigilance, pièces exigées) que le logiciel utilise **par défaut** mais qui **doivent
> être validées par le Responsable LBC/FT de l'IMF**, en accord avec la directive UEMOA, les
> recommandations du GAFI et les standards du GIABA.
>
> **Principe :** aucune de ces valeurs n'est du code. Elles vivent en base (module Paramétrage,
> grille KYC) et se règlent sans redéploiement. Les valeurs livrées sont des **défauts
> provisoires** marqués « À VALIDER » — jamais présentées comme définitives. Chaque IMF ajuste
> selon son régulateur ; un durcissement (ex. CENTIF) est un changement de paramètre, pas une
> nouvelle version.

## Le modèle : trois types de règles

Le risque LBC/FT ne se réduit pas à un score. La grille distingue :

1. **Règles contributives** — ajoutent des points ; la somme, comparée à un barème, donne un
   niveau de base (faible / moyen / élevé).
2. **Règles plancher** — imposent un niveau de vigilance MINIMUM quel que soit le score
   (ex. PPE → vigilance renforcée obligatoire). Niveau effectif = max(barème, planchers).
3. **Règles bloquantes (couperet)** — interdisent l'activation (ex. sanctions → refus). Ce n'est
   pas un niveau élevé, c'est un blocage ; peut basculer la fiche en `suspendu_lcb`.

## Liste des valeurs à valider

Les défauts provisoires ci-dessous sont ceux **seedés par la migration 0013** (grille v1,
`is_provisional = TRUE`). Ce sont des points de départ plausibles, **pas** des valeurs
réglementaires — à remplacer par les valeurs de l'expert.

| # | Valeur | Type | Défaut provisoire (grille v1) | Statut |
|---|--------|------|-------------------------------|--------|
| 1a | Pays à risque GAFI | contributive | **+40 points** | ⚠️ À VALIDER |
| 1b | Secteur d'activité à risque | contributive | **+30 points** | ⚠️ À VALIDER |
| 1c | Volume d'activité élevé | contributive | **+20 points** | ⚠️ À VALIDER |
| 1d | Entrée en relation à distance | contributive | **+25 points** | ⚠️ À VALIDER |
| 1e | Entrée par tiers de confiance | contributive | **+10 points** | ⚠️ À VALIDER |
| 2 | Barème score → niveau | barème | **faible < 30, moyen 30–59, élevé ≥ 60** | ⚠️ À VALIDER |
| 3 | Statut PPE → niveau plancher | plancher | **PPE → niveau élevé (vigilance renforcée)** | ⚠️ À VALIDER |
| 4 | Liste des fonctions PPE + périmètre « entourage/proches » | référentiel | à créer (T3+) | ⚠️ À VALIDER |
| 5 | Pays GAFI gris/noir (`countries.is_gafi_high_risk`) | référentiel | flag présent, aucun pays flagué (seed T0) | ⚠️ À VALIDER |
| 6 | Secteurs à risque (parmi le référentiel `secteurs_activite`) | référentiel | **change manuel, métaux précieux, jeux/casinos, immobilier** | ⚠️ À VALIDER |
| 7 | Pièces exigées selon profil / niveau de vigilance | règle | à définir (T3c) | ⚠️ À VALIDER |
| 8 | Périodicité de revue KYC (1 / 3 / 5 ans selon niveau) | échéance | 1 an élevé, 3 ans moyen, 5 ans faible (spec) | ⚠️ À VALIDER |
| 9 | Seuil de « risque élevé » routant la validation vers le LBC/FT | routage | niveau effectif = élevé → LBC/FT (T3d) | ⚠️ À VALIDER |
| 10 | Seuil « volume élevé » (montant déclenchant 1c) | contributive | à définir (T3b) | ⚠️ À VALIDER |
| 11 | Correspondance liste de sanctions | couperet | **blocage de l'activation** (données en T6) | ⚠️ À VALIDER |
| 12 | Double validation KYC (quatre-yeux : activateur ≠ vérificateur) | politique | **exigée par défaut**, assouplissable **par agence** (`agencies.double_validation_kyc`) ; auto-validation tracée | ⚠️ À VALIDER (expert + déploiement) |
| 13 | Activation sous barème PROVISOIRE | politique | **autorisée + avertissement + traçable** (registre dérivable) ; blocage éventuel à la mise en production, pas par fiche | ⚠️ À VALIDER (expert + déploiement) |

Structure paramétrable : `parameters.kyc_risk_grid` (conteneur versionné, `is_provisional`),
`parameters.kyc_risk_rules` (les trois types), `parameters.kyc_risk_thresholds` (barème),
`parameters.secteurs_activite` (référentiel + flag `is_a_risque`).

## Rappels réglementaires structurants (spec §4)

- Conservation des données **10 ans** après la fin de la relation (soft delete + restauration déjà en place).
- Vigilance **renforcée** obligatoire pour les PPE et les relations à risque élevé.
- Identification du **bénéficiaire effectif** pour les personnes morales (chantier ultérieur).
- Filtrage **anti-listes de sanctions** à la validation (module T6).

_Mettre à jour ce tableau à chaque décision de l'expert : remplacer le défaut provisoire par la
valeur validée et passer le statut à ✅ VALIDÉ (avec la date et le nom du valideur)._
