# Runbook — documents archivés (PDF/A)

Le serveur génère les documents (reçus, bons, états) en PDF/A, les écrit dans l'archive et les
indexe en base. Le navigateur ne fait que télécharger ou imprimer ce même fichier.

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
- Tester la restauration : restaurer, puis télécharger un document (l'empreinte SHA-256 est
  recontrôlée à chaque téléchargement).

## Incidents

| Symptôme | Cause probable | Action |
|---|---|---|
| `/health/ready` en 503 | Dossier d'archive absent ou non inscriptible | Créer le dossier / corriger les droits |
| Documents en statut `echec` | Pango absent, disque plein, droits | Corriger la cause, puis `POST /documents/{id}/regenerer` |
| Téléchargement en 409 « empreinte » | Fichier modifié sur le disque | Restaurer le fichier depuis la sauvegarde ; l'altération est auditée (`documents.archive.alteree`) |
| Fichier présent sans ligne d'index | Arrêt entre l'écriture et l'indexation | Rien : la génération suivante **adopte** le fichier tel quel, sans l'écraser |

## Limites connues

- Le rendu n'est pas garanti identique octet pour octet d'une exécution à l'autre sur Linux
  (étiquettes de sous-ensembles de polices). L'idempotence repose sur l'index et le nom
  déterministe, jamais sur un second rendu identique.
- Lot 1 : seul un type d'**essai** existe. Les documents réels sont câblés aux opérations aux lots
  suivants.
