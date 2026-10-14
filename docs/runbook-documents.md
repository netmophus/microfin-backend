# Runbook — documents archivés (PDF/A)

Le serveur génère les documents (reçus, bons, lettres, états comptables) en PDF/A, les écrit dans
l'archive et les indexe en base. Le navigateur ne fait que télécharger ou imprimer ce même fichier :
il ne fabrique plus aucun document (l'impression navigateur de la v1 a été retirée).

## Principes

- **Source unique** : le PDF archivé. Jamais régénéré à la volée pour un document déjà archivé.
- **Daté par la journée comptable** : dossier, contenu et métadonnées du PDF portent la journée
  comptable de l'opération, jamais l'horloge du poste.
- **Écriture unique** : un fichier archivé n'est ni réécrit ni supprimé (ni par l'application, ni
  par l'index : triggers `UPDATE`/`DELETE` interdits sur `documents.archives`).
- **Après le commit** : la génération suit le commit métier. Un échec de génération ne défait
  jamais une opération : il laisse une ligne d'index `statut = echec` (avec le message technique),
  rattrapable par `POST /documents/{id}/regenerer`.
- **Aucune suppression automatique.** La durée légale de conservation est à confirmer avec
  l'expert-comptable ; en attendant, tout est conservé.

## Arborescence

```
<DOCUMENTS_REPERTOIRE>/<code_agence>/AAAA/MM/JJ/<module>/<nom_explicite>.pdf
```

`AAAA/MM/JJ` est la **journée comptable** du document (jamais l'horloge). `<module>` : `caisse`,
`epargne`, `tiers` ou `comptabilite`. `<code_agence>` : l'agence de l'opération ; pour les états
comptables (centralisés, sans agence), celle de la personne qui a clôturé la journée.

Composants assainis (lettres, chiffres, `.`, `-`, `_` ; le reste devient `_`).

## Configuration

| Variable | Rôle |
|---|---|
| `DOCUMENTS_REPERTOIRE` | Dossier d'archive, chemin **absolu**. **Obligatoire hors dev** (l'application refuse de démarrer sans). En dev : `./archives_documents`, ignoré par git. |
| `IMF_NOM`, `IMF_ADRESSE`, `IMF_TELEPHONE`, `IMF_NUMERO_AGREMENT` | En-tête des documents. `IMF_NOM` vide : bandeau « provisoire — à valider » sur chaque document. |

**Prêt** : `GET /health/ready` répond 503 si le dossier d'archive n'existe pas ou n'est pas
inscriptible (`GET /health` ne dit que « vivant »). Un orchestrateur doit s'appuyer sur `/health/ready`.

## Production Linux / Docker

WeasyPrint a besoin de **Pango** (bibliothèques système). Le `Dockerfile` du backend les installe :

```
apt-get install -y --no-install-recommends libpango-1.0-0 libpangocairo-1.0-0 libpangoft2-1.0-0
```

- L'image tourne en utilisateur non privilégié (uid 10001) ; `XDG_CACHE_HOME=/tmp/cache` évite les
  avertissements fontconfig (pas de dossier personnel).
- `DOCUMENTS_REPERTOIRE=/data/documents` est un **volume** : monter un stockage persistant,
  **sauvegardé**, appartenant à l'utilisateur 10001. Exemple (compose) :

  ```yaml
  services:
    api:
      build: .
      volumes: ["documents:/data/documents"]
  volumes:
    documents:
  ```
- Droits sur le disque : l'application n'a besoin que de **créer** des fichiers dans l'archive. Si
  le stockage le permet, lui retirer le droit de suppression/écrasement (défense en profondeur).

## Poste de développement Windows

WeasyPrint importe Pango par DLL : sans elle, `import weasyprint` échoue (« could not import some
external libraries »).

```
winget install --id MSYS2.MSYS2 -e --silent --accept-package-agreements --accept-source-agreements
C:\msys64\usr\bin\bash.exe -lc "pacman -Sy --noconfirm mingw-w64-x86_64-pango"
```

- Le premier `pacman` peut échouer (miroir lent) : relancer.
- L'application cherche seule `C:\msys64\mingw64\bin` (variable `WEASYPRINT_DLL_DIRECTORIES`, posée
  par `rendu.py` si elle n'est pas déjà définie). Autre emplacement : définir la variable soi-même.
- MSYS2 occupe environ 900 Mo.

## Polices

Polices **libres embarquées** dans le dépôt (`app/modules/documents/polices/`, DejaVu Sans et Sans
Mono, licence `LICENSE-DejaVu.txt`). Aucune police générique dans le CSS : un rendu ne dépend pas
des polices du serveur.

## Sauvegarde et restauration

- Sauvegarder **ensemble** la base (index `documents.archives`) et le dossier d'archive.
- Pas d'ordre imposé entre les deux, mais ils doivent dater du **même instant** : un index plus récent
  que le dossier laisserait des lignes `ok` sans fichier (téléchargement en 409 « introuvable »).
- Restaurer : (1) la base, (2) le dossier d'archive, **avec le même utilisateur propriétaire** (uid
  10001 en conteneur), (3) vérifier `GET /health/ready`.
- **Tester la restauration** (une sauvegarde jamais restaurée n'est pas une sauvegarde) : restaurer
  sur un environnement jetable, puis télécharger un document de chaque module ; l'empreinte SHA-256
  est recontrôlée à chaque téléchargement, une altération est refusée et auditée.

## Régénération

- Seul un document en statut `echec` se régénère (`POST /documents/{id}/regenerer`, ou le bouton
  « Régénérer » de l'écran). Corriger d'abord la cause (voir Incidents).
- La régénération repart des données conservées à l'index : le document produit reflète l'opération
  telle qu'elle était, pas l'état actuel de la base. Elle pose une nouvelle ligne `ok`, conserve le
  propriétaire d'origine, et est idempotente (une clé déjà `ok` est rendue telle quelle).
- Un document `ok` n'est **jamais** régénéré ni réécrit : c'est la pièce.

## Incidents

| Symptôme | Cause probable | Action |
|---|---|---|
| `/health/ready` en 503 | Dossier d'archive absent ou non inscriptible | Créer le dossier / corriger les droits |
| Documents en statut `echec` | Pango absent, disque plein, droits | Corriger la cause, puis `POST /documents/{id}/regenerer` |
| Aucun état comptable pour une journée clôturée | Arrêt du processus entre la clôture et l'archivage | `etats.archiver_etats_cloture(db, courant, journée)` depuis un shell (idempotent) |
| Téléchargement en 409 « empreinte » | Fichier modifié sur le disque | Restaurer le fichier depuis la sauvegarde ; l'altération est auditée (`documents.archive.alteree`) |
| Fichier présent sans ligne d'index | Arrêt entre l'écriture et l'indexation | Rien : la génération suivante **adopte** le fichier tel quel, sans l'écraser |

## Accès

- `documents.read` ouvre l'écran des documents. SEUL (le caissier), il restreint à **ses propres
  documents** (`acteur_id`), dans son agence.
- `documents.read.agence` (RESPONSABLE_AGENCE, COMPTABLE, DIRECTION_GENERALE, AUDITEUR_INTERNE) élargit
  à tout le périmètre : l'agence, ou le réseau pour les rôles à portée réseau.
- **Journal et balance du jour** (états comptables, centralisés) : réservés à `compta.rapport.read`
  (même droit que le grand livre) ET `documents.read.agence`, quelle que soit l'agence de rattachement
  de l'archive. Un responsable d'agence ou un caissier ne les voit jamais, même dans la même agence.
- Hors périmètre : 404 (jamais 403). Chaque téléchargement est audité (`documents.telecharge`).
- Régénérer le document d'un caissier ne lui en retire pas la propriété (l'`acteur_id` d'origine est
  conservé ; le régénérateur figure dans l'audit).

## Documents générés par les opérations (lot 2)

Générés APRÈS le commit de l'opération, par les routes de guichet ; leur référence (`document`,
`bon`) est jointe à la réponse, avec le statut `ok` ou `echec` (régénérable).

| Opération | Document | Module | Nom |
|---|---|---|---|
| Dépôt | `recu_versement` | `caisse` | `recu_versement_<compte>_<piece>.pdf` |
| Retrait | `recu_retrait` | `caisse` | `recu_retrait_<compte>_<piece>.pdf` |
| Placement DAT | `recu_placement_dat` + `bon_dat` | `epargne` | `recu_placement_dat_<compte>_<piece>.pdf`, `bon_dat_<compte>.pdf` |
| Dénouement DAT | `recu_denouement_dat` | `epargne` | `recu_denouement_dat_<compte>_<piece>.pdf` |
| Retrait anticipé DAT | `recu_retrait_anticipe_dat` | `epargne` | `recu_retrait_anticipe_dat_<compte>_<piece>.pdf` |
| Souscription de parts au comptant | `recu_parts_souscription` | `tiers` | `recu_parts_souscription_<piece>.pdf` |
| Libération de parts | `recu_parts_liberation` | `tiers` | `recu_parts_liberation_<piece>.pdf` |
| Remboursement de parts (sortie d'espèces) | `recu_parts_remboursement` | `tiers` | `recu_parts_remboursement_<piece>.pdf` |
| Lettre d'explication d'un manquant de caisse | `lettre_explication_ecart` | `caisse` | `lettre_explication_<compte_caisse>_<journee>.pdf` |
| Journal du jour (clôture) | `journal_du_jour` | `comptabilite` | `journal_du_jour_<journee>.pdf` |
| Balance du jour (clôture) | `balance_du_jour` | `comptabilite` | `balance_du_jour_<journee>.pdf` |

Les services métier eux-mêmes ne génèrent rien : seuls les routeurs archivent. Une souscription de
parts SANS paiement (engagement) n'encaisse rien : pas de reçu.

### Lettre d'explication

Générée à la demande du responsable (`POST /caisse/sessions/{id}/lettre-explication`, droits de
lecture de la session ET `documents.read`), puis archivée **une seule fois par session** : les
demandes suivantes rendent la même archive. Refusée (422) pour une session ouverte, sans écart ou en
excédent. Elle appartient au caissier concerné (il relit SA lettre). Les heures réelles d'ouverture et
de fermeture n'y figurent pas (aucun fuseau paramétré côté serveur) : la journée comptable identifie
la session.

### Journal et balance du jour

- **À la clôture** de la journée (après le commit, idempotent, jamais bloquant) : le journal (écritures
  validées de la journée, groupées par journal) et la balance générale **arrêtée** à la journée
  (mouvements du jour et soldes **cumulés** depuis l'origine) sont archivés et figés. Une journée
  clôturée est définitive, ses états aussi.
- **Journée ouverte** : `GET /documents/etats/{journal-du-jour|balance-du-jour}/brouillon` rend un PDF
  **brouillon** à la volée — marqué sur chaque page, **ni archivé ni indexé** (un brouillon n'est pas
  une pièce) ; chaque demande est auditée (`documents.brouillon.genere`). Seule la journée ouverte est
  acceptée ; les journées clôturées se retrouvent dans « Documents du jour ».
- L'index conserve le contexte complet (c'est ce qui permet de régénérer à l'identique) : prévoir un
  jsonb de quelques centaines de ko pour une grosse journée.

### Écran « Documents du jour »

`/documents` (permission `documents.read`) : liste filtrable (journée, module, type), paginée côté
serveur, téléchargement et régénération par ligne, brouillons du jour pour `compta.rapport.read`.

## Limites connues

- Le rendu n'est pas garanti identique octet pour octet d'une exécution à l'autre sur Linux
  (étiquettes de sous-ensembles de polices). L'idempotence repose sur l'index et le nom
  déterministe, jamais sur un second rendu identique.
- Le type `essai` (socle) n'est utilisé que par les tests.
- Pas de rattrapage automatique si le processus s'arrête entre le commit de la clôture et l'archivage
  des états : aucune ligne n'existe alors (ni `ok` ni `echec`). Les états ne se régénèrent que depuis
  une ligne `echec` ; un trou se comble en relançant l'archivage depuis un shell
  (`etats.archiver_etats_cloture`, idempotent).
