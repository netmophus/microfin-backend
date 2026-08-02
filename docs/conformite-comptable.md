# Conformité comptable SFD — valeurs à faire valider

> Ce document liste les valeurs **comptables** que le logiciel utilise **par défaut** mais qui
> **doivent être validées par un expert-comptable SFD**, en accord avec le référentiel comptable
> RCSFD de l'UEMOA et les instructions BCEAO en vigueur.
>
> **Principe** (identique au barème KYC — voir `conformite-lbcft.md`) : le plan comptable et les
> schémas d'écriture sont des **données**, pas du code. Ils vivent en base, se règlent sans
> redéploiement. Les valeurs livrées sont **provisoires**, marquées « À VALIDER » (drapeau
> `is_provisional` en base + bannière à l'écran). Aucune n'est présentée comme définitive.

## Le plan de comptes — référentiel officiel RCSFD + extensions IMF

**Le plan actif est le référentiel officiel**, depuis le départ : **380 comptes**
(372 comptes officiels + 8 comptes d'extension membre/client), tous marqués
`is_system = TRUE` (numérotation officielle, protégée) et **tous provisoires**
(`accounts.is_provisional = TRUE`) — la numérotation est sûre, mais le **sens** (D/C) et les
**rattachements** restent à faire valider par un expert-comptable SFD avant mise en production.

- [`reference/plan_comptable_rcsfd_officiel.csv`](reference/plan_comptable_rcsfd_officiel.csv) —
  le référentiel officiel tel quel (372 comptes), extrait du *Référentiel comptable spécifique
  des systèmes financiers décentralisés de l'UMOA* (RCSFD, 1re édition, ISBN 978-2-916140-11-7).
  **Correction (02/08/2026)** : une version antérieure de ce document citait par erreur la
  décision BCEAO n° 357-11-2016 (plan comptable **bancaire** révisé de l'UMOA — un référentiel
  différent, pour les banques, pas pour les SFD). Cette citation était une erreur de
  documentation ; les données du plan de comptes n'ont jamais été extraites de ce fichier
  bancaire.
- [`reference/plan_comptable_import.csv`](reference/plan_comptable_import.csv) — le fichier
  réellement importé (380 lignes : les 372 + les 8 extensions), avec hiérarchie/nature/sens
  dérivés (méthodologie ci-dessous).

| # | Valeur | Statut |
|---|--------|--------|
| 1 | Numéros de comptes du plan (les 380) | ✅ officiels (BCEAO) + extensions proposées |
| 2 | Sens normal D/C de chaque compte | ⚠️ À VALIDER — voir priorité de relecture ci-dessous |
| 3 | Comptes de saisie (`is_posting`) vs regroupement | ⚠️ À VALIDER (déduit mécaniquement de la hiérarchie, fiable) |
| 4 | Hiérarchie parent/enfant | ✅ dérivée de la numérotation officielle |
| 5 | Distinction membre / client (comptes à 6 chiffres, extension IMF) | ⚠️ À VALIDER — structure ET libellés proposés |
| 6 | Nombre de décimales des montants (XOF = 0 en présentation ; calcul d'intérêts ?) | ⚠️ À VALIDER |

### Origine et méthodologie (décision du 02/08/2026)

**Fenêtre unique avant mise en production** : aucune donnée réelle à préserver à ce stade,
seulement des jeux de test. Décision prise de **repartir d'une base vierge** avec le seul plan
officiel RCSFD + les 8 extensions, plutôt que de faire coexister un ancien plan provisoire
(jamais validé) et le nouveau. Traçabilité de la **décision**, pas des données : un premier plan
provisoire à 345 comptes (classes 1-9, hors nomenclature officielle) a existé, a servi à tester
le mécanisme (import, garde-fous, rattachements, écritures, les Blocs 1-5 du paramétrage
comptable), puis a été **purgé** — il n'apparaît plus nulle part dans cette base.

**Dérivation du plan importé** (numéro/libellé/classe viennent tels quels du fichier officiel) :
- **Parent et nature** (saisie/regroupement) : déduits **mécaniquement** de la numérotation — un
  compte sans enfant dans le fichier est un compte de saisie, sauf **2511, 2512, 2521, 2531**,
  reclassés en regroupement puisqu'ils gagnent des enfants à 6 chiffres absents du fichier
  officiel. Fiable.
- **Sens (D/C)** : classification par nature comptable standard (actif = D, passif/capitaux
  propres = C, charges = D, produits = C), avec priorité à la règle « Dettes rattachées » = C /
  « Créances rattachées » = D quelle que soit la section parente. **Ma proposition**, pas une
  extraction du texte réglementaire — voir la liste de relecture prioritaire ci-dessous.
- **8 comptes d'extension** (structure proposée, à valider) :

| Compte | Libellé proposé | Parent | Sens | Rattaché à |
|---|---|---|---|---|
| 251111 | Comptes ordinaires — membres | 2511 | C | EAV, membre |
| 251121 | Comptes ordinaires — clients | 2511 | C | EAV, client |
| 251211 | Comptes ordinaires sur livret — membres | 2512 | C | (réservé, aucun produit) |
| 251221 | Comptes ordinaires sur livret — clients | 2512 | C | (réservé, aucun produit) |
| 252111 | Dépôts à terme reçus — membres | 2521 | C | DAT, membre |
| 252121 | Dépôts à terme reçus — clients | 2521 | C | DAT, client |
| 253111 | Compte d'épargne sur livret — membres | 2531 | C | EPR, membre |
| 253121 | Compte d'épargne sur livret — clients | 2531 | C | EPR, client |

`2512`/`251211`/`251221` sont posés en **réserve** (aucun produit actuel ne les utilise) pour un
futur 4ᵉ produit de type livret — coût nul de les poser maintenant.

**Priorité de relecture experte — 32 comptes à sens structurellement moins évident.** Sur les
372 comptes officiels, la hiérarchie et la nature (saisie/regroupement) sont déduites
mécaniquement de la numérotation (fiable). Le **sens** (D/C) suit la nature comptable standard
partout ailleurs, mais ces 32 comptes méritent un œil expert en priorité — pas noyés dans les
372, à traiter d'abord :

**4 comptes reclassés en regroupement** (gagnent des enfants à 6 chiffres qui n'existent pas
dans le fichier officiel — la reclassification elle-même est à confirmer, pas seulement le sens) :

| Compte | Libellé | Sens proposé |
|---|---|---|
| 2511 | Comptes ordinaires | C |
| 2512 | Comptes ordinaires sur livret | C |
| 2521 | Dépôts à terme reçus | C |
| 2531 | Compte d'épargne sur livret | C |

**28 comptes au sens proposé par convention, à confirmer** (ressources affectées, comptes
transitoires/d'attente, comptes de régularisation, report à nouveau et résultat — sections où
un compte peut légitimement porter un solde des deux sens selon l'usage réel de l'institution) :

| Compte | Libellé | Sens proposé | Compte | Libellé | Sens proposé |
|---|---|---|---|---|---|
| 18 | Ressources affectées | C | 3814 | Comptes d'abonnement de produits | D |
| 181 | Ressources affectées à court terme | C | 3815 | Produits à recevoir | D |
| 182 | Ressources affectées à moyen terme | C | 382 | Comptes de régularisation — passif | C |
| 183 | Ressources affectées à long terme | C | 3822 | Produits constatés d'avance | C |
| 184 | Intérêts capitalisés | C | 3824 | Comptes d'abonnement de charges | C |
| 37 | Comptes transitoires et d'attente | D | 3825 | Charges à payer | C |
| 378 | Autres comptes transitoires | D | 58 | Report à nouveau | C |
| 379 | Comptes d'attente | D | 59 | Résultat | C |
| 3791 | Comptes d'attente — actif | D | 591 | Excédent ou déficit en instance d'approbation | C |
| 3792 | Comptes d'attente — passif | C | 592 | Excédent ou déficit de l'exercice | C |
| 38 | Comptes de régularisation | D | 593 | Marge | C |
| 381 | Comptes de régularisation — actif | D | 594 | Produit financier net ou charge financière nette | C |
| 3811 | Charges à répartir sur plusieurs exercices | D | 595 | Excédent ou déficit d'exploitation | C |
| 3812 | Charges constatées d'avance | D | 596 | Excédent ou déficit exceptionnel | C |

Tous les 380 comptes (372 officiels + 8 extensions) restent `is_system = TRUE` (numérotation
officielle, protégée) et `is_provisional = TRUE` (sens à confirmer) — même discipline que le
reste de ce document : aucune valeur n'est présentée comme définitive avant l'expert.

## Concordance bilan / compte de résultat (Annexe 1 RCSFD) — décision provisoire

Pour les rapports « à date » (grand livre, balance, et plus tard bilan/résultat provisoires —
voir R1/R2/R3), la nomenclature officielle (Annexe 1 du RCSFD) fait correspondre chaque poste des
états financiers à une liste de comptes. La quasi-totalité de cette concordance résout sans
ambiguïté sur nos 380 comptes (un compte-parent chez nous se substitue par la somme de ses
enfants de saisie). **6 postes de la classe 2 (comptes membres/clients) restent une hypothèse, pas
une certitude**, et devront porter un badge « à confirmer » dans l'écran du bilan le jour où il
existera (R3) — pas ailleurs sur le rapport.

**Le problème** : la nomenclature marque `2511`/`2512` d'un préfixe « ex » (extrait) — ces comptes
d'origine peuvent porter un solde débiteur (découvert accidentel) OU créditeur (dépôt normal),
et apparaissent donc potentiellement à l'actif (portion débitrice) ET au passif (portion
créditrice). Chez nous, `2511`/`2512` sont éclatés en comptes à 6 chiffres **exclusivement
créditeurs** (`251111`/`251121`/`251211`/`251221`, sens C, sans variante débitrice).

**Décision provisoire (02/08/2026, à valider par l'expert)** : traiter la portion débitrice
(« ex ») comme **structurellement sans objet** dans notre système, parce que (a) un découvert
éventuel est déjà comptabilisé séparément (compte `2023` Découverts, distinct de `2511`), et (b)
nos comptes `251111`/`251121` n'ont **aucune** variante débitrice — le garde-fou
`decouvert_autorise = 0` par défaut (voir plus bas) l'empêche déjà en pratique. Si cette politique
changeait un jour (découvert autorisé directement sur un compte à vue), cette décision serait à
revoir.

**Mapping résultant, sous cette hypothèse** :

| Poste | Formule RCSFD | Chez nous (sous l'hypothèse ci-dessus) |
|---|---|---|
| B01 (actif) | … + ex2511 + … | le terme `ex2511` vaut **0** (aucun compte débiteur chez nous) ; le reste du poste (2022+2023+20227+2031+2037+291..294-2991..2993) résout normalement |
| B2N (actif) | + ex2511 | **0** chez nous par construction (poste entièrement composé de la portion débitrice, sans objet) |
| G01 (passif) | … + ex2511 + ex2512 + 2521 + 2531 + … | substituer `ex2511+ex2512` par `251111+251121+251211+251221` ; `2521` et `2531` selon G15/G2A ci-dessous |
| G10 (passif) | + ex2511 + ex2512 | **251111 + 251121 + 251211 + 251221** |

**Asymétrie de parenté découverte (2 postes de plus, sans préfixe « ex » mais avec le même
piège)** : `25116` (dettes rattachées de 2511) est un ENFANT de `2511` dans notre plan, donc déjà
exclu ci-dessus par construction (G90 le compte séparément). Mais `25316` (dettes rattachées de
2531) est de la même façon un enfant de `2531` chez nous, **alors que** `2526` (dettes rattachées
de 2521) est un enfant de `252` (pas de `2521`) — asymétrie propre à notre plan, pas au
référentiel :

| Poste | Formule RCSFD | Chez nous |
|---|---|---|
| G15 (passif) | + 2521 | **252111 + 252121** (pas d'exclusion nécessaire : `2526` n'est pas un descendant de `2521` chez nous) |
| G2A (passif) | + 2531 | **253111 + 253121** (EXCLURE `25316`, qui est un enfant direct de `2531` chez nous et déjà compté à part dans G90) |

Ces 6 postes (B01, B2N, G01, G10, G15, G2A) sont les seuls de toute la nomenclature (240 postes
vérifiés) à reposer sur une hypothèse plutôt qu'une correspondance mécanique directe. Le reste de
l'extraction (classes 1, 3 à 7, Annexes 2 et 3) est disponible dans le dépôt de travail, sans point
en suspens.

## Les schémas d'écriture (E1, paramétrables — POSÉS, provisoires)

Pour **chaque opération**, quels comptes sont débités / crédités. Le moteur (poser une écriture
depuis un schéma) est du code ; **les comptes sont de la donnée**. Les modèles sont désormais
stockés en base (`comptabilite.entry_schemas` + `entry_schema_lines`), par **rôle** résolu à
l'opération : `epargne.depot` = D **CAISSE** / C **EPARGNE** ; `epargne.retrait` = l'inverse. Tous
**provisoires**, à valider avant mise en production.

**La caisse (face argent)** : le rôle CAISSE se résout via `parameters.agencies.compte_caisse_id`
— **un compte de caisse par agence** (provisoire : toutes les agences → **1011** Billets et
monnaies émis par la BCEAO). C'est la face COMPTABLE seulement ; le vrai module Caisse
(guichets, arrêtés, dénombrement, écarts) est reporté.

**Collectif ↔ auxiliaire (loi de rapprochement)** : le rôle EPARGNE se résout via
`epargne.products.compte_epargne_id` — le compte **général** (ex. **251111** pour l'épargne à
vue membre), qui porte le **total** de tous les membres. Le **détail par membre** vit dans
l'auxiliaire (`epargne.accounts`). Invariant à contrôler (fonction `rapprocher`) : **Σ soldes
épargne rattachés au collectif == solde comptable de ce collectif**. Un écart = fraude ou bug, à
détecter avant l'inspecteur. Vérifié en réel après la bascule (dépôt/retrait/rapprochement
concordants sur 251111 et 251121, voir smoke test du 02/08/2026).

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
| `compte_charge_interet_id` | Compte de charge (602511 à vue / 60252 DAT / 60253 EPR) | selon produit | ⚠️ À VALIDER |
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

**Schéma d'écriture du versement (provisoire)** : `epargne.interet` = **D 602511** Intérêts sur
comptes ordinaires créditeurs / **C 251111** Épargne à vue membres (journal OD). La charge de
l'IMF monte, la dette envers le membre monte. Membre suspendu : crédité normalement (c'est son
argent). Compte fermé : plus d'intérêts.

| Opération | Schéma provisoire (à valider) | Statut |
|-----------|-------------------------------|--------|
| Dépôt épargne à vue (membre) | D 1011 Caisse / C 251111 Épargne à vue membres | ⚠️ À VALIDER |
| Retrait épargne à vue | D 251111 / C 1011 Caisse | ⚠️ À VALIDER |
| Ouverture dépôt à terme | D 251111 (ou caisse) / C 252111 DAT | ⚠️ À VALIDER |
| Intérêts créditeurs (charge) | D 6xxx Charges d'intérêts / C 319x Intérêts courus | ⚠️ À VALIDER |
| Capitalisation des intérêts | D 319x / C 251111 | ⚠️ À VALIDER |
| Souscription parts sociales | D Caisse / C 57111 Capital souscrit appelé versé | ⚠️ À VALIDER |
| Frais de tenue de compte | D compte membre / C 7xxx Produits | ⚠️ À VALIDER |

## Rattachement produit d'épargne → compte du plan (Épargne, PROVISOIRE)

Chaque produit d'épargne pointe vers le **compte de dette** (classe 2, sens crédit) crédité au
dépôt. Rattachement livré **provisoire** (`epargne.products.compte_epargne_id` /
`compte_epargne_client_id`), à valider par l'expert-comptable SFD. Éditable sans redéploiement
via l'écran de rattachement (Bloc 5 du paramétrage comptable).

| Produit | Compte MEMBRE (provisoire) | Compte CLIENT (PS3, provisoire) | Statut |
|---------|----------------------------|--------------------------------|--------|
| Épargne à vue (EAV) | **251111** | **251121** | ⚠️ À VALIDER |
| Dépôt à terme (DAT) | **252111** | **252121** | ⚠️ À VALIDER |
| Épargne programmée (EPR) | **253111** | **253121** | ⚠️ À VALIDER |

**Routage membre/client (PS3) — ANCRÉ PAR COMPTE, ACTIF.** Le compte d'épargne FIGE son
collectif à l'ouverture (`epargne.accounts.compte_collectif_id`) selon le statut du titulaire à
ce moment-là : membre actif (`tiers.tiers.is_member = TRUE`) → compte membre, client → compte
client (repli sur le compte membre si non rattaché). Toutes les écritures du compte (dépôt,
retrait, clôture, intérêts) suivent ce même collectif : un compte ne s'éclate jamais entre les
deux. **Vérifié en réel après la bascule** (02/08/2026) : un dépôt/retrait client s'est posé sur
251121, un dépôt/retrait membre (après souscription de parts sociales) sur 252111, rapprochement
concordant sur les deux.

**⚠️ À VALIDER (option B, provisoire)** : un client devenu membre garde ses comptes existants sur
le compte client — seuls les NOUVEAUX comptes suivent le nouveau statut ; AUCUN transfert
automatique n'est codé (si l'expert exige un transfert au passage membre, ce sera une opération
explicite, jamais silencieuse).
- *Préférence provisoire (à confirmer par l'expert)* : pas de bascule automatique des comptes
  existants. Plus simple, aucun mouvement d'argent non sollicité.
- *Alternative* : **transfert** du solde (compte client → compte membre) au passage membre, pour
  que le bilan reflète la qualité au jour J. Plus lourd (déplace des soldes), à auditer. **AUCUN
  transfert automatique n'est codé** ; cette option n'existera que si l'expert la valide.

## Parts sociales (PS1, PROVISOIRES) — la mécanique accueille, l'expert/les statuts choisissent

Config d'institution `tiers.share_parameters` (une ligne, `is_provisional`), toutes valeurs
`⚠️ À VALIDER` par l'expert / les statuts de l'IMF (comme les taux d'épargne). Défaut « neutre ».

| Paramètre | Rôle | Défaut | Statut |
|-----------|------|--------|--------|
| `unit_value` | Valeur d'une part (XOF entier) | 0 | ⚠️ À VALIDER |
| `minimum_shares` | Nombre minimum de parts pour adhérer | 1 | ⚠️ À VALIDER |
| `is_refundable` | Parts remboursables ou non | vrai | ⚠️ À VALIDER |
| `membership_on` | Membre à la **souscription** ou à la **libération** | libération | ⚠️ À VALIDER |
| `compte_parts_liberees_id` / `_non_liberees_id` | Rattachement 57111 / 57112 | 57111 / 57112 | ⚠️ À VALIDER |

**Moment de l'adhésion** : défaut **libération** (on est membre quand le capital est réellement
versé — parts libérées ≥ minimum), paramétrable en `souscription`. Le marqueur `tiers.is_member`
bascule DANS la transaction de l'opération — **vérifié en réel** après la bascule (souscription
comptant → `is_member` passe à `TRUE` dans la même transaction, smoke test du 02/08/2026). Montants
en **francs entiers** (le `NUMERIC(18,2)` des specs est transposé en BIGINT). Hypothèse
provisoire : **valeur d'une part constante** (sinon historiser, comme la date de valeur
d'épargne) — vaut pour la libération et le rapprochement.

**Schémas d'écriture (provisoires, seed `seed-comptabilite`)**. 57112 sens D (souscrites non
libérées, créance), 57111 sens C (libérées, capital) — ce mapping colle exactement à la
nomenclature officielle (« Capital souscrit appelé versé » / « … non versé »). Souscription-
engagement SANS caisse (journal OD) ; libération et comptant AVEC caisse (journal CA — argent qui
entre, comme un dépôt).

| Opération | Schéma | Journal | Statut |
|-----------|--------|---------|--------|
| Souscription (engagement) | **D 57112 / C 57111** | OD | ⚠️ À VALIDER |
| Libération (paiement) | **D 1011 / C 57112** | CA | ⚠️ À VALIDER |
| Souscription au comptant | **D 1011 / C 57111** | CA | ⚠️ À VALIDER |
| Remboursement (départ, PS2) | **D 57111 / C 1011** | CA | ⚠️ À VALIDER |

**Rapprochement du capital** (contrôle) : `Σ (parts libérées × valeur d'une part) == NET comptable
57111 - 57112`. ⚠️ Le capital réellement libéré n'est PAS 57111 seul : la souscription-engagement
crédite 57111 (montant souscrit) ET débite 57112 (part non libérée, une créance — motif « capital
souscrit appelé / non appelé »). Le net `57111 - 57112` = `Σ(crédit - débit)` sur les deux
comptes. La libération crédite 57112 (la créance s'éteint) → le net monte. Un écart = anomalie.

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
