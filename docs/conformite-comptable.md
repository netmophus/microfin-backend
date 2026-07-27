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

## Les schémas d'écriture (à définir en C3, paramétrables)

Pour **chaque opération**, quels comptes sont débités / crédités. Le moteur (poser une écriture
depuis un schéma) est du code ; **les comptes sont de la donnée**. Valeurs provisoires à définir
et valider avant toute mise en production.

| Opération | Schéma provisoire (à valider) | Statut |
|-----------|-------------------------------|--------|
| Dépôt épargne à vue (membre) | D 57x Caisse / C 3111 Épargne à vue membres | ⚠️ À VALIDER |
| Retrait épargne à vue | D 3111 / C 57x Caisse | ⚠️ À VALIDER |
| Ouverture dépôt à terme | D 3111 (ou caisse) / C 3121 DAT | ⚠️ À VALIDER |
| Intérêts créditeurs (charge) | D 6xxx Charges d'intérêts / C 319x Intérêts courus | ⚠️ À VALIDER |
| Capitalisation des intérêts | D 319x / C 3111 | ⚠️ À VALIDER |
| Souscription parts sociales | D Caisse / C 1021 Parts sociales libérées | ⚠️ À VALIDER |
| Frais de tenue de compte | D compte membre / C 7xxx Produits | ⚠️ À VALIDER |

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
