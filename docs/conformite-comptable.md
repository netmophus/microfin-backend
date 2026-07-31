# Conformité comptable SFD — valeurs à faire valider

> Ce document liste les valeurs **comptables** que le logiciel utilise **par défaut** mais qui
> **doivent être validées par un expert-comptable SFD**, en accord avec le référentiel comptable
> RCSFD de l'UEMOA et les instructions BCEAO en vigueur.
>
> **Principe** (identique au barème KYC — voir `conformite-lbcft.md`) : le plan comptable et les
> schémas d'écriture sont des **données**, pas du code. Ils vivent en base, se règlent sans
> redéploiement. Les valeurs livrées sont **provisoires**, marquées « À VALIDER » (drapeau
> `is_provisional` en base + bannière à l'écran). Aucune n'est présentée comme définitive.

## Le plan de comptes (importé du CSV RCSFD)

- **345 comptes**, 9 classes, importés à l'installation et **tous marqués provisoires**
  (`accounts.is_provisional = TRUE`). L'expert valide (globalement ou compte par compte), puis
  on lève le drapeau.
- À valider : **les numéros de comptes exacts**, leur **libellé**, le **sens normal** (D/C),
  `is_posting` (compte de saisie vs regroupement), la **hiérarchie** (parent), la conformité au
  référentiel RCSFD/BCEAO **en vigueur**.

| # | Valeur | Statut |
|---|--------|--------|
| 1 | Numéros de comptes du plan (les 345) | ⚠️ À VALIDER |
| 2 | Sens normal D/C de chaque compte | ⚠️ À VALIDER |
| 3 | Comptes de saisie (`is_posting`) vs regroupement | ⚠️ À VALIDER |
| 4 | Hiérarchie parent/enfant | ⚠️ À VALIDER |
| 5 | Distinction membre / client (comptes `xxx1` vs `xxx2`) — critère métier | ⚠️ À VALIDER |
| 6 | Nombre de décimales des montants (XOF = 0 en présentation ; calcul d'intérêts ?) | ⚠️ À VALIDER |

## Les schémas d'écriture (E1, paramétrables — POSÉS, provisoires)

Pour **chaque opération**, quels comptes sont débités / crédités. Le moteur (poser une écriture
depuis un schéma) est du code ; **les comptes sont de la donnée**. Les modèles sont désormais
stockés en base (`comptabilite.entry_schemas` + `entry_schema_lines`), par **rôle** résolu à
l'opération : `epargne.depot` = D **CAISSE** / C **EPARGNE** ; `epargne.retrait` = l'inverse. Tous
**provisoires**, à valider avant mise en production.

**La caisse (face argent)** : le rôle CAISSE se résout via `parameters.agencies.compte_caisse_id`
— **un compte de caisse par agence** (provisoire : Siège → **5721** Caisses agences). C'est la face
COMPTABLE seulement ; le vrai module Caisse (guichets, arrêtés, dénombrement, écarts) est reporté.

**Collectif ↔ auxiliaire (loi de rapprochement)** : le rôle EPARGNE se résout via
`epargne.products.compte_epargne_id` — le compte **général 3111**, qui porte le **total** de tous
les membres. Le **détail par membre** vit dans l'auxiliaire (`epargne.accounts`). Invariant à
contrôler (fonction `rapprocher`) : **Σ soldes épargne rattachés à 3111 == solde comptable de
3111**. Un écart = fraude ou bug, à détecter avant l'inspecteur.

Valeurs provisoires à définir et valider avant toute mise en production.

### Paramètres d'opération par produit (E3, PROVISOIRES)

Le plancher d'un retrait est **paramétrable par produit**, pas en dur. Disponible au retrait =
`solde - solde_minimum + découvert_autorisé`.

| Paramètre (`epargne.products`) | Rôle | Défaut | Statut |
|--------------------------------|------|--------|--------|
| `min_balance` | Solde minimum à garder pour maintenir le compte ouvert | 0 | ⚠️ À VALIDER (par produit) |
| `decouvert_autorise` | De combien le solde peut passer sous zéro (comptes 3021/3022) | 0 | ⚠️ À VALIDER (par produit) |

Épargne à vue standard : les deux à **0** (le solde ne descend pas sous zéro ; un découvert est du
crédit, pas de l'épargne). L'expert dira quels produits ont droit à un découvert et à quel plafond.

### Intérêts d'épargne (E5, PROVISOIRES) — la mécanique accueille, l'expert choisit

Le moteur d'intérêts est neutre ; **toutes les valeurs ci-dessous sont des paramètres produit
provisoires** (`epargne.products`), à valider. Défaut « taux 0 » = pas d'intérêt tant que non fixé.

| Paramètre | Rôle | Défaut | Statut |
|-----------|------|--------|--------|
| `taux_bp` | Taux annuel en points de base (350 = 3,5 %) | 0 | ⚠️ À VALIDER |
| `periodicite` | mensuelle / trimestrielle / annuelle | annuelle | ⚠️ À VALIDER |
| `methode_calcul_solde` | **LE point réglementaire** : min_periode / moyen_quotidien / fin_periode | fin_periode | ⚠️ À VALIDER |
| `base_jours` | Base jours (360 / 365) | 360 | ⚠️ À VALIDER |
| `regle_arrondi` | plus_proche / plancher | plus_proche | ⚠️ À VALIDER |
| `solde_minimum_remunere` | Seuil sous lequel pas d'intérêt | 0 | ⚠️ À VALIDER |
| `compte_charge_interet_id` | Compte de charge (603 à vue / 604 programmée) | 603/604 | ⚠️ À VALIDER |
| Fiscalité (retenue) | Hors E5 pour l'instant, place réservée | — | ⚠️ À VALIDER |

**Méthode de calcul du solde** (le plus sensible) — les trois méthodes sont implémentées, le
produit choisit : **min de la période** (conservateur), **moyen quotidien** (le plus juste, pondéré
par le temps), **fin de période** (le plus simple). Toutes reconstituées depuis l'historique des
mouvements. L'expert tranche par produit.

**⚠️ À VALIDER — méthode réglementairement attendue pour les SFD UEMOA (règle des quinzaines ?).**
Une méthode classique en zone franc est la **règle des quinzaines** : le mois est découpé en deux
périodes de 15 jours, avec une **date de valeur** — un dépôt ne produit d'intérêt qu'à partir de la
quinzaine suivante, un retrait fait perdre l'intérêt dès sa quinzaine. **Elle n'est PAS implémentée
aujourd'hui** : les trois méthodes ci-dessus datent chaque mouvement à sa **date d'opération**
(`movements.created_at`), et les mouvements **ne portent pas de date de valeur** distincte. Question
à trancher par l'expert : (a) quelle méthode est réglementairement attendue pour un SFD UEMOA
(quinzaines, capitaux moyens, autre ?) ; (b) si quinzaines/date de valeur, la valeur est-elle
**dérivée mécaniquement** de la date d'opération et du sens (implémentable comme une méthode de
plus, sans changer la structure), ou faut-il une **date de valeur saisissable/corrigible** par
mouvement (alors : ajouter une colonne `date_valeur` à `epargne.movements`, décision de conception).
Ne pas deviner : c'est une valeur réglementaire.

**Date de valeur EN PLACE, dormante (migration 0024).** La colonne `epargne.movements.date_valeur`
(DATE) existe désormais, **égale par défaut à la date d'opération** (`created_at::date`, backfill
des mouvements existants compris). **AUCUNE règle ne l'exploite encore** : le moteur d'intérêts date
toujours sur `created_at`, donc **aucun calcul n'est modifié**. Elle est **figée à la création** et
immuable ensuite (même trigger que le mouvement). Fondation prête : quand l'expert aura confirmé la
méthode (quinzaines ou autre), le service posera `date_valeur` À L'INSERTION selon la règle en
vigueur, et le moteur lira `date_valeur` au lieu de `created_at` ; les mouvements passés gardent leur
date de valeur d'origine (on ne réécrit pas le passé, le calcul reste rejouable). **À VALIDER : la
méthode elle-même** (voir ci-dessus).

**Schéma d'écriture du versement (provisoire)** : `epargne.interet` = **D 603** Intérêts sur
épargne à vue / **C 3111** Épargne à vue membres (journal OD). La charge de l'IMF monte, la dette
envers le membre monte. Membre suspendu : crédité normalement (c'est son argent). Compte fermé :
plus d'intérêts.

| Opération | Schéma provisoire (à valider) | Statut |
|-----------|-------------------------------|--------|
| Dépôt épargne à vue (membre) | D 57x Caisse / C 3111 Épargne à vue membres | ⚠️ À VALIDER |
| Retrait épargne à vue | D 3111 / C 57x Caisse | ⚠️ À VALIDER |
| Ouverture dépôt à terme | D 3111 (ou caisse) / C 3121 DAT | ⚠️ À VALIDER |
| Intérêts créditeurs (charge) | D 6xxx Charges d'intérêts / C 319x Intérêts courus | ⚠️ À VALIDER |
| Capitalisation des intérêts | D 319x / C 3111 | ⚠️ À VALIDER |
| Souscription parts sociales | D Caisse / C 1021 Parts sociales libérées | ⚠️ À VALIDER |
| Frais de tenue de compte | D compte membre / C 7xxx Produits | ⚠️ À VALIDER |

## Rattachement produit d'épargne → compte du plan (Épargne, PROVISOIRE)

Chaque produit d'épargne pointe vers le **compte de dette** (classe 3, sens crédit) crédité au
dépôt. Rattachement livré **provisoire** (`epargne.products.compte_epargne_id`), à valider par
l'expert-comptable SFD.

| Produit | Compte MEMBRE (provisoire) | Compte CLIENT (PS3, provisoire) | Statut |
|---------|----------------------------|--------------------------------|--------|
| Épargne à vue (EAV) | **3111** | **3112** | ⚠️ À VALIDER |
| Dépôt à terme (DAT) | **3121** | **3122** | ⚠️ À VALIDER |
| Épargne programmée (EPR) | **3131** | **3132** | ⚠️ À VALIDER |

**Routage membre/client (PS3) — ANCRÉ PAR COMPTE.** Le compte d'épargne FIGE son collectif à
l'ouverture (`epargne.accounts.compte_collectif_id`) selon le statut du titulaire à ce moment-là :
membre → compte membre (xxx1), client → compte client (xxx2, repli sur le compte membre si non
rattaché — comportement historique). Toutes les écritures du compte (dépôt, retrait, clôture,
intérêts) suivent ce même collectif : un compte ne s'éclate jamais entre 3111 et 3112.
**⚠️ À VALIDER (option B, provisoire)** : un client devenu membre garde ses comptes existants sur
xxx2 — seuls les NOUVEAUX comptes suivent le nouveau statut ; AUCUN transfert automatique n'est
codé (si l'expert exige un transfert xxx2 → xxx1 au passage membre, ce sera une opération
explicite, jamais silencieuse). Les comptes d'avant PS3 sont ancrés là où leurs écritures sont
déjà (le compte membre) : rien n'est réécrit. Rapprochement PAR GROUPE : Σ soldes ancrés
xxx1 == solde xxx1 ET Σ soldes ancrés xxx2 == solde xxx2 (la vue de contrôle montre les deux).

**Question ouverte — membre / client** : le plan distingue épargne à vue **membres (3111)** et
**clients (3112)** (idem par nature de produit). Le compte de dette dépend donc de la **nature du
tiers** (sociétaire vs simple usager). **Le marqueur existe désormais** sur le tiers
(`tiers.tiers.is_member`, migration 0025 : FALSE = client par défaut, TRUE = membre). Le **routage
comptable** qui l'exploite (membre → 3111, client → 3112) est le chantier **Parts sociales / PS3** ;
provisoirement, tous les produits restent rattachés au compte **membre** (3111), rétrocompatible.

**⚠️ À VALIDER — épargne existante au passage client → membre.** Quand un client devient membre,
que deviennent ses comptes d'épargne DÉJÀ ouverts (routés 3112) ?
- *Préférence provisoire (à confirmer par l'expert)* : **les comptes existants ne basculent PAS**
  automatiquement ; seuls les **nouveaux** comptes suivent la nouvelle qualité. Plus simple, aucun
  mouvement d'argent non sollicité. Contrainte technique associée : le routage doit être **ancré
  par compte** (le compte mémorise son collectif à l'ouverture), sinon un même compte s'éclaterait
  sur 3111 ET 3112 et casserait le rapprochement.
- *Alternative* : **transfert** du solde `D 3112 / C 3111` au passage membre, pour que le bilan
  reflète la qualité au jour J. Plus lourd (déplace des soldes), à auditer. **AUCUN transfert
  automatique n'est codé** ; cette option n'existera que si l'expert la valide.

## Parts sociales (PS1, PROVISOIRES) — la mécanique accueille, l'expert/les statuts choisissent

Config d'institution `tiers.share_parameters` (une ligne, `is_provisional`), toutes valeurs
`⚠️ À VALIDER` par l'expert / les statuts de l'IMF (comme les taux d'épargne). Défaut « neutre ».

| Paramètre | Rôle | Défaut | Statut |
|-----------|------|--------|--------|
| `unit_value` | Valeur d'une part (XOF entier) | 0 | ⚠️ À VALIDER |
| `minimum_shares` | Nombre minimum de parts pour adhérer | 1 | ⚠️ À VALIDER |
| `is_refundable` | Parts remboursables ou non | vrai | ⚠️ À VALIDER |
| `membership_on` | Membre à la **souscription** ou à la **libération** | libération | ⚠️ À VALIDER |
| `compte_parts_liberees_id` / `_non_liberees_id` | Rattachement 1021 / 1022 | 1021 / 1022 | ⚠️ À VALIDER |

**Moment de l'adhésion** : défaut **libération** (on est membre quand le capital est réellement
versé — parts libérées ≥ minimum), paramétrable en `souscription`. Le marqueur `tiers.is_member`
bascule DANS la transaction de l'opération. Montants en **francs entiers** (le `NUMERIC(18,2)` des
specs est transposé en BIGINT). Hypothèse provisoire : **valeur d'une part constante** (sinon
historiser, comme la date de valeur d'épargne) — vaut pour la libération et le rapprochement.

**Schémas d'écriture (provisoires, seed `seed-comptabilite`)**. 1022 sens D (souscrites non
libérées, créance), 1021 sens C (libérées, capital). Souscription-engagement SANS caisse (journal
OD) ; libération et comptant AVEC caisse (journal CA — argent qui entre, comme un dépôt).

| Opération | Schéma | Journal | Statut |
|-----------|--------|---------|--------|
| Souscription (engagement) | **D 1022 / C 1021** | OD | ⚠️ À VALIDER |
| Libération (paiement) | **D 5721 / C 1022** | CA | ⚠️ À VALIDER |
| Souscription au comptant | **D 5721 / C 1021** | CA | ⚠️ À VALIDER |
| Remboursement (départ, PS2) | **D 1021 / C 5721** | CA | ⚠️ À VALIDER |

**Rapprochement du capital** (contrôle) : `Σ (parts libérées × valeur d'une part) == NET comptable
1021 - 1022`. ⚠️ Le capital réellement libéré n'est PAS 1021 seul : la souscription-engagement
crédite 1021 (montant souscrit) ET débite 1022 (part non libérée, une créance — motif « capital
souscrit appelé / non appelé »). Le net `1021 - 1022` = `Σ(crédit - débit)` sur les deux comptes.
La libération crédite 1022 (la créance s'éteint) → le net monte. Un écart = anomalie. *(Le libellé
« 1021 = libérées » du plan est un raccourci : 1021 porte le souscrit, 1022 le contra non libéré —
à confirmer par l'expert avec le reste des schémas de parts.)*

## Journaux et exercice (C1)

Journaux livrés (seed `seed-comptabilite`), **tous provisoires** (`journals.is_provisional`) —
à valider/compléter par le comptable : **CA** (caisse), **BQ** (banque), **OD** (opérations
diverses), **AN** (à-nouveaux). L'exercice est ouvert à l'installation (`ouvrir-exercice`), la
clôture est reportée.

| # | Valeur | Statut |
|---|--------|--------|
| 7 | Liste et codification des journaux (CA/BQ/OD/AN livrés) | ⚠️ À VALIDER |
| 8 | Définition de l'exercice (dates) ; règles de clôture (reportées) | ⚠️ À VALIDER |

## Les écritures (C2) — règles imposées par le logiciel

- Une pièce validée est **équilibrée** (Σ débit = Σ crédit au niveau pièce), a **≥ 2 lignes**,
  et n'écrit que sur des **comptes de saisie**. Garanti par le service ET par la base (trigger
  différé au commit + trigger immédiat sur le compte de saisie).
- Montants en **francs CFA entiers** (BIGINT), jamais de flottant.
- Une pièce validée est **immuable** ; correction = **contre-passation** uniquement.
- Numérotation **atomique, sans trou**, par journal et par exercice (ex. `CA-2026-000001`).

_Mettre à jour ce tableau à chaque décision de l'expert : remplacer le défaut provisoire par la
valeur validée, passer le statut à ✅ VALIDÉ (date + nom), et lever `is_provisional` en base._
