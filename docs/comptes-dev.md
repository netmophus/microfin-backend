# Comptes de développement

Jeu de comptes **de dev uniquement**, posé par `python -m app.cli seed-dev`. À ne jamais
utiliser en production (la commande refuse `ENV=production`). Mot de passe **public**, assumé :
c'est une commodité de test, pas un secret.

## Installation (base neuve)

```
python -m app.cli seed-security      # rôles + permissions
python -m app.cli creer-admin        # agence siège + admin (mot de passe généré, affiché une fois)
python -m app.cli seed-dev           # les comptes ci-dessous
python -m app.cli seed-comptabilite  # journaux, modèles d'écriture, caisse par agence
python -m app.cli seed-epargne       # produits d'épargne (PROVISOIRES)
python -m app.cli seed-credit        # produit de crédit de démonstration (PROVISOIRE, taux non nul)
```

La commande est **idempotente** : rejouable sans dupliquer ni écraser un mot de passe changé.

## Les comptes

Mot de passe commun : **`MotDePasse!Dev1`** — `must_change_password = false` (connexion directe).
Tous rattachés à l'agence **siège** (AG-001).

| Identifiant | Rôle | Ce qu'il permet d'éprouver |
|-------------|------|----------------------------|
| `resp`      | RESPONSABLE_AGENCE   | Désactiver / **réactiver** une fiche, **valider** l'activation (KYC), vérifier une pièce, voir les désactivés de son agence |
| `lbcft`     | RESPONSABLE_LBC_FT   | Vigilance LBC/FT réseau, **valider** l'activation, vérifier une pièce, voir les désactivés du réseau |
| `auditeur`  | AUDITEUR_INTERNE     | Lecture réseau (fiches, journal d'audit), voir les désactivés |
| `charge`    | CHARGE_CLIENTELE     | Enrôler un tiers, saisir coordonnées et pièces, suspendre — **ne voit pas** les désactivés |
| `caissier`  | CAISSIER             | Vue guichet limitée (read.basic), identification sans données KYC, remboursement de crédit |
| `pret`      | CHARGE_PRET          | Monter un dossier de crédit (créer une demande), consulter |
| `comite`    | MEMBRE_COMITE_CREDIT | Décider une demande de crédit (approuver/refuser) |
| `comptable` | COMPTABLE            | Plan de comptes, tous les écrans de rattachement (Bloc 5), paliers de souffrance (CR5a) |
| `direction` | DIRECTION_GENERALE   | Actes d'institution : versement des intérêts, reclassification (aperçu + exécution, CR5c), consultation des paliers en lecture seule, portée réseau |

Le compte **admin** (ADMIN_FONCTIONNEL) vient de `creer-admin`, séparément (mot de passe généré).

`direction` est un compte de **DEV**, distinct du compte réel `anne` (GG001, DIRECTION_GENERALE
lui aussi) qui peut exister sur une base ayant servi au navigateur — ne jamais confondre les
deux ni toucher au second.

## Note

Cette liste vit ici, pas dans le code : le mot de passe est dans `app/cli/seed_dev.py`
(`MOT_DE_PASSE_DEV`), le reste est dérivable de la commande. Si un rôle nouveau acquiert des
permissions métier, ajouter un compte à `COMPTES` dans ce même fichier et une ligne ici.
