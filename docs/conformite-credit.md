# Conformité crédit SFD — valeurs à faire valider

> Ce document liste les valeurs et conventions du module **Crédit** que le logiciel utilise
> **par défaut** mais qui **doivent être validées par un expert-comptable/crédit SFD**, en
> accord avec le référentiel comptable RCSFD de l'UEMOA et les instructions BCEAO en vigueur.
>
> **Principe** (identique au plan comptable — voir `conformite-comptable.md` — et au barème KYC,
> `conformite-lbcft.md`) : rien de réglementaire n'est deviné. Les valeurs livrées sont
> **provisoires**, marquées « À VALIDER » (`is_provisional = TRUE` sur `credit.products` +
> bannière à l'écran quand le frontend crédit existera). Aucune n'est présentée comme définitive.

**Périmètre couvert (au 04/08/2026)** : CR0 (référentiel produit), CR1 (demande et décision),
CR2 (échéancier, calcul pur), CR3 (décaissement, première écriture comptable). CR4
(remboursements) et CR5 (retards/provisionnement) ne sont pas encore construits — leurs points
ouverts viendront s'ajouter ici au fur et à mesure.

## 1. Rattachement comptable (CR0) — comptes d'extension classe 20

Quatre comptes d'extension à 6 chiffres, `is_system = TRUE` (posés à la conception du module,
comme le référentiel initial de l'Épargne), tous `is_provisional = TRUE` :

| Compte | Rattaché à | Rôle |
|--------|-----------|------|
| 202211 | 2022 (Crédits ordinaires, court terme) | Membre |
| 202221 | 2022 (Crédits ordinaires, court terme) | Client |
| 203111 | 2031 (Crédits à moyen terme) | Membre |
| 203121 | 2031 (Crédits à moyen terme) | Client |

`2031` était `is_posting = TRUE` dans le fichier RCSFD officiel mais sans historique réel au
moment de CR0 : reclassé en compte de regroupement (comme `2511`/`2521`/`2531` l'ont été pour
l'Épargne) plutôt que verrouillé après coup — aucune donnée à préserver à ce stade.

## 2. Point ouvert explicite — granularité classe 29 vs classe 664 (signalé dès CR0, non tranché)

Le référentiel officiel présente une **incohérence de découpage** entre les créances en
souffrance et les provisions correspondantes :

- **Classe 29** (Comptes de crédits en souffrance) : **trois** tranches d'ancienneté —
  `292` (≤ 6 mois), `293` (6-12 mois), `294` (12-24 mois) — plus `291` (Crédits immobilisés,
  un axe contentieux/juridique distinct de l'ancienneté, non encore clarifié non plus) et `299`
  (provisions, avec `2991`/`2992`/`2993`, trois sous-comptes, cohérents avec les trois tranches
  de 29).
- **Classe 664** (Dotations aux provisions pour créances en souffrance) : **quatre** tranches —
  `66411` (0-3 mois), `66412` (3-6 mois), `6642` (6-12 mois), `6643` (12-24 mois).

Trois tranches d'un côté, quatre de l'autre, sur la même échelle de temps. **Ce document ne
tranche rien** : c'est un point réglementaire à soumettre tel quel à l'expert-comptable avant
de construire CR5 (retards/provisionnement). Hypothèses possibles (non retenues, à titre
d'exemple pour le cadrage de la question) : la classe 664 introduit une sous-coupure fine
0-3/3-6 mois à l'intérieur du premier palier de la classe 29, ou l'un des deux référentiels
contient une erreur/omission. **Rien n'est codé sur cet axe tant que la réponse n'est pas
connue** (CR5 reste bloqué en attendant, conformément à la décision prise en amont du
découpage CR0→CR6).

## 3. L'échéancier (CR2) — conventions mécaniques assumées, pas des données réglementaires

`credit.products` porte, par produit, `taux_bp` (points de base), `periodicite`
(mensuelle/trimestrielle/annuelle), `methode_amortissement` (capital_constant /
echeance_constante), `base_jours` (360, non utilisé par CR2), `regle_arrondi` (plus_proche /
plancher) — tous `is_provisional = TRUE`, tous à 0/valeur neutre par défaut.

- **Taux périodique** : `taux_bp / 10000 / nb_périodes_par_an` — une convention
  **proportionnelle simple**, pas actuarielle/composée. Choix mécanique assumé pour que les
  deux méthodes d'amortissement produisent un résultat déterministe et cohérent entre elles à
  taux nul ; **pas une extraction d'un texte réglementaire**.
- **TAEG / taux effectif global** : hors périmètre de CR2, non calculé, non affiché. Aucune
  valeur de communication réglementaire au client n'est produite par ce moteur.
- **`base_jours`** : posé sur le produit dès CR0 mais **non exploité** par `generer_echeancier()`
  — l'échéancier CR2 raisonne en périodes fixes (mensualité/trimestre/année), pas en jours
  exacts. Réservé à un raffinement futur (calcul au jour près), signalé plutôt qu'exploité en
  silence.
- **Garde-fou de cohérence** (`EcheancierImpossibleError`, `app/modules/credit/echeancier.py`) :
  un montant trop faible pour la durée demandée, combiné à un arrondi défavorable, peut rendre
  l'échéancier économiquement impossible (capital restant dû qui deviendrait négatif). Le moteur
  refuse proprement plutôt que de produire un résultat qui n'a pas de sens — voir
  `tests/unit/test_credit_echeancier.py`.

## 4. Le décaissement (CR3) — décisions mécaniques, pas réglementaires

- **Date de la première échéance** : décaissement + UNE période pleine (mensuelle/trimestrielle/
  annuelle selon le produit), calculée en stdlib pur (`calendar.monthrange`, calage sur le
  dernier jour du mois cible si le jour d'origine n'existe pas — ex. 31/01 + 1 mois → 28/02).
  Convention naturelle (le taux périodique de CR2 suppose un intérêt couru sur une période
  pleine avant le premier paiement), pas un choix réglementaire distinct.
- **Décaissement intégral, en une fois** : pas de tranches ni de déblocages progressifs dans ce
  premier périmètre (crédit individuel simple à échéances fixes). Une demande approuvée se
  décaisse pour la totalité de `montant_decide`, une seule fois (`credit.applications.status`
  ne repasse jamais de `decaisse` à un état antérieur).
- **`credit.installments.status`** : colonne posée dès CR3 (`server_default = 'a_echoir'`) mais
  **sans contrainte CHECK** — le vocabulaire complet des états d'une échéance (payée, en
  retard, etc.) appartient à CR4 (remboursements), pas encore décidé. La contrainte sera
  ajoutée quand ce vocabulaire existera, plutôt que d'être devinée maintenant.

## 5. Séparation des tâches (organisationnel, pas comptable — mentionné pour traçabilité)

`credit.demande.decide` (comité de crédit) et `credit.decaissement.create` (responsable
d'agence) sont deux permissions **séparées**, jamais accordées à `CHARGE_PRET` (qui monte le
dossier). Choix organisationnel du projet, aligné sur le même principe déjà appliqué à la
fermeture d'un compte d'épargne et au remboursement de parts sociales — pas une exigence
extraite d'un texte, mais une pratique de contrôle interne standard en microfinance.
