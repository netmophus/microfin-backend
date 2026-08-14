"""Point d'entrée de la CLI d'administration : `python -m app.cli <commande>`."""

from typing import Annotated

import typer
from sqlalchemy.exc import IntegrityError

from app.cli.creer_admin import (
    ComptesDejaPresentsError,
    RoleIntrouvableError,
    creer_admin,
)
from app.cli.seed_comptabilite import (
    executer_seed_journaux,
    executer_seed_schemas,
    ouvrir_exercice,
    rattacher_caisse_agences,
    seed_parametres_caisse,
    seed_parametres_parts,
)
from app.cli.seed_credit import executer_seed_paliers_souffrance, executer_seed_produits_credit
from app.cli.seed_dev import (
    MOT_DE_PASSE_DEV,
    AgenceManquanteError,
    RoleManquantError,
    executer_seed_dev,
)
from app.cli.seed_epargne import executer_seed_produits
from app.cli.seed_security import executer_seed
from app.core.config import settings
from app.core.database import SessionLocal

# Enregistre TOUS les modèles ORM dans Base.metadata avant toute utilisation — MÊME LISTE
# qu'alembic/env.py, même raison : une FK inter-schéma déclarée en chaîne (ex.
# credit.Application.tier_id -> "tiers.tiers.id") ne se résout qu'au premier flush/query
# touchant les mappers, et lève NoReferencedTableError si le module qui définit la table cible
# n'a jamais été importé. Le contexte FastAPI (app.main) importe tout transitivement ; cette
# CLI, non — sans ces imports, toute commande touchant un modèle à FK croisée casse au premier
# usage (découvert avec configurer-prelevement/prelever-echeances, CR5d).
from app.modules.audit import models as _audit_models  # noqa: F401
from app.modules.caisse import models as _caisse_models  # noqa: F401
from app.modules.comptabilite import models as _comptabilite_models  # noqa: F401
from app.modules.comptabilite.plan import (
    FichierInvalideError,
    ImportRefuseError,
    importer,
)
from app.modules.credit import models as _credit_models  # noqa: F401
from app.modules.epargne import models as _epargne_models  # noqa: F401
from app.modules.parameters import models as _parameters_models  # noqa: F401
from app.modules.security import models as _security_models  # noqa: F401
from app.modules.tiers import models as _tiers_models  # noqa: F401

app = typer.Typer(help="Outils d'administration du SIG microfinance.", no_args_is_help=True)


@app.callback()
def racine() -> None:
    """Sans ce callback, Typer replierait une application à commande unique en CLI sans
    sous-commande : `seed-security` deviendrait un argument parasite."""


@app.command("seed-security")
def seed_security(
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Simule : la transaction est annulée, rien n'est écrit."),
    ] = False,
) -> None:
    """Installe les 11 rôles système et les 18 permissions du périmètre Sécurité.

    Idempotente : rejouable à chaque installation d'une IMF et à chaque montée de version.
    """
    with SessionLocal() as db:
        rapport = executer_seed(db)

        if dry_run:
            db.rollback()
        else:
            db.commit()

    typer.echo(f"Rôles système     : {rapport.roles}")
    typer.echo(f"Permissions       : {rapport.permissions}")
    typer.echo(f"Habilitations     : {rapport.accords}")
    typer.echo(f"Révocations       : {rapport.revocations}")
    typer.echo("Simulation — rien n'a été écrit." if dry_run else "Seed appliqué.")


@app.command("seed-dev")
def seed_dev(
    force: Annotated[
        bool,
        typer.Option("--force", help="Autoriser malgré ENV=production (à éviter)."),
    ] = False,
) -> None:
    """Crée un jeu de comptes de DÉVELOPPEMENT (un par rôle métier), mot de passe fixe et connu.

    Distincte du seed de production. Refuse ENV=production sauf --force : des comptes à mot de
    passe public n'ont rien à faire en prod. Idempotente. À jouer après seed-security + creer-admin.
    """
    if settings.ENV.lower().startswith("prod") and not force:
        typer.secho(
            f"Refus : ENV={settings.ENV}. Les comptes de dev ne s'installent pas en production "
            "(--force pour outrepasser, à vos risques).",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)

    with SessionLocal() as db:
        try:
            rapport = executer_seed_dev(db)
        except AgenceManquanteError:
            db.rollback()
            typer.secho("Refus : aucune agence.", fg=typer.colors.RED, err=True)
            typer.echo("Jouez d'abord : python -m app.cli creer-admin", err=True)
            raise typer.Exit(code=1) from None
        except RoleManquantError as erreur:
            db.rollback()
            typer.secho(
                f"Refus : rôle(s) absent(s) : {erreur.args[0]}.", fg=typer.colors.RED, err=True
            )
            typer.echo("Jouez d'abord : python -m app.cli seed-security", err=True)
            raise typer.Exit(code=1) from None
        db.commit()

    typer.echo("")
    typer.secho("  Comptes de développement en place.", fg=typer.colors.GREEN, bold=True)
    if rapport.crees:
        typer.echo(f"  Créés   : {', '.join(rapport.crees)}")
    if rapport.ignores:
        typer.echo(f"  Déjà là : {', '.join(rapport.ignores)}")
    typer.echo(f"  Mot de passe (tous) : {MOT_DE_PASSE_DEV}")
    typer.secho("  Voir docs/comptes-dev.md pour la liste rôle par rôle.", fg=typer.colors.YELLOW)
    typer.echo("")


@app.command("import-plan-comptable")
def import_plan_comptable(
    chemin: Annotated[str, typer.Argument(help="Chemin du CSV du plan de comptes RCSFD.")],
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Valide et simule : la transaction est annulée."),
    ] = False,
) -> None:
    """Importe le plan de comptes depuis un CSV — TOUT OU RIEN.

    Le fichier est d'abord VALIDÉ intégralement en mémoire. À la moindre anomalie, RIEN n'est
    écrit et toutes les anomalies sont listées ligne par ligne. Sinon, les comptes sont importés
    et marqués PROVISOIRES (à faire valider par un expert-comptable SFD).

    Idempotente : rejouable (upsert par numéro de compte).
    """
    with SessionLocal() as db:
        try:
            rapport = importer(db, chemin)
        except FichierInvalideError as erreur:
            db.rollback()
            typer.secho(f"Fichier invalide : {erreur.args[0]}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1) from None
        except ImportRefuseError as erreur:
            db.rollback()
            typer.secho(
                f"Import refusé — {len(erreur.anomalies)} anomalie(s), aucun compte écrit :",
                fg=typer.colors.RED,
                err=True,
            )
            for anomalie in erreur.anomalies:
                typer.echo(f"  - {anomalie}", err=True)
            raise typer.Exit(code=1) from None

        if dry_run:
            db.rollback()
        else:
            db.commit()

    total = rapport.crees + rapport.mis_a_jour
    typer.echo("")
    typer.secho(f"  Plan de comptes importé : {total} comptes.", fg=typer.colors.GREEN, bold=True)
    typer.echo(f"  Créés    : {rapport.crees}")
    typer.echo(f"  Mis à jour : {rapport.mis_a_jour}")
    typer.secho(
        "  Tous marqués PROVISOIRES — à valider par un expert-comptable SFD "
        "(voir docs/conformite-comptable.md).",
        fg=typer.colors.YELLOW,
    )
    if dry_run:
        typer.echo("  Simulation — rien n'a été écrit.")
    typer.echo("")


@app.command("seed-comptabilite")
def seed_comptabilite() -> None:
    """Installe journaux + modèles d'écriture + rattachement caisse (PROVISOIRES). Idempotente."""
    with SessionLocal() as db:
        nb_journaux = executer_seed_journaux(db)
        nb_schemas = executer_seed_schemas(db)
        nb_caisses = rattacher_caisse_agences(db)
        seed_parametres_parts(db)
        seed_parametres_caisse(db)
        db.commit()
    typer.echo("")
    typer.secho(
        f"  Comptabilité amorcée : {nb_journaux} journaux, {nb_schemas} modèles d'écriture, "
        f"{nb_caisses} agence(s) rattachée(s) à une caisse.",
        fg=typer.colors.GREEN,
        bold=True,
    )
    typer.secho("  PROVISOIRES — à valider/compléter par le comptable.", fg=typer.colors.YELLOW)
    typer.echo("")


@app.command("seed-epargne")
def seed_epargne() -> None:
    """Installe les produits d'épargne standard (PROVISOIRES). Idempotente."""
    with SessionLocal() as db:
        nb = executer_seed_produits(db)
        db.commit()
    typer.echo("")
    typer.secho(f"  Produits d'épargne en place : {nb}.", fg=typer.colors.GREEN, bold=True)
    typer.secho(
        "  PROVISOIRES — à valider/compléter par l'IMF (taux, règles).", fg=typer.colors.YELLOW
    )
    typer.echo("")


@app.command("seed-credit")
def seed_credit() -> None:
    """Installe le produit de crédit de démonstration et les paliers de souffrance de départ
    (CR5a) — tout PROVISOIRE. Idempotente."""
    with SessionLocal() as db:
        nb_produits = executer_seed_produits_credit(db)
        nb_paliers = executer_seed_paliers_souffrance(db)
        db.commit()
    typer.echo("")
    typer.secho(
        f"  Produit(s) de crédit en place : {nb_produits}.", fg=typer.colors.GREEN, bold=True
    )
    typer.secho(
        f"  Palier(s) de souffrance en place : {nb_paliers}.", fg=typer.colors.GREEN, bold=True
    )
    typer.secho(
        "  PROVISOIRE — taux/seuils de démonstration, comptes non rattachés, à valider par l'IMF.",
        fg=typer.colors.YELLOW,
    )
    typer.echo("")


@app.command("verser-interets")
def verser_interets_cmd(
    periode: Annotated[str, typer.Option(help="Libellé de période (clé anti-double).")],
    debut: Annotated[str, typer.Option(help="Début de période (AAAA-MM-JJ).")],
    fin: Annotated[str, typer.Option(help="Fin de période (AAAA-MM-JJ).")],
) -> None:
    """Verse les intérêts de la période à tous les comptes actifs. Idempotente (anti-double).

    Relançable sans risque : un compte déjà traité pour la période est sauté (contrainte base).
    """
    from datetime import date

    from app.modules.epargne.interets import verser_interets

    with SessionLocal() as db:
        rapport = verser_interets(
            db, periode=periode, debut=date.fromisoformat(debut), fin=date.fromisoformat(fin)
        )
    typer.echo("")
    typer.secho(
        f"  Intérêts période {periode} : {rapport.traites} comptes traités, "
        f"{rapport.credites} crédités, {rapport.ignores} ignorés, total {rapport.total} F.",
        fg=typer.colors.GREEN,
        bold=True,
    )
    typer.echo("")


@app.command("configurer-prelevement")
def configurer_prelevement_cmd(
    dossier: Annotated[
        str, typer.Option(help="Numéro du dossier de crédit (ex. CR-2026-0000001).")
    ],
    compte: Annotated[
        str, typer.Option(help="Numéro du compte d'épargne à débiter (ex. EP-2026-0000001).")
    ],
) -> None:
    """Choisit le compte d'épargne à débiter pour le prélèvement automatique (CR5d) d'un crédit
    DÉJÀ décaissé. Refuse si le compte n'appartient pas au même tiers, n'est pas actif, ou est un
    dépôt à terme (DAT)."""
    from sqlalchemy import select

    from app.modules.credit.models import Application
    from app.modules.credit.prelevement import (
        CompteInvalideError,
        DemandeNonDecaisseeError,
        configurer_prelevement,
    )
    from app.modules.epargne.models import SavingsAccount

    with SessionLocal() as db:
        demande = db.execute(
            select(Application).where(Application.application_number == dossier)
        ).scalar_one_or_none()
        if demande is None:
            typer.secho(f"Dossier introuvable : {dossier}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1)

        compte_epargne = db.execute(
            select(SavingsAccount).where(SavingsAccount.account_number == compte)
        ).scalar_one_or_none()
        if compte_epargne is None:
            typer.secho(f"Compte introuvable : {compte}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1)

        try:
            configurer_prelevement(db, demande, compte_epargne.id, par=None)
        except (DemandeNonDecaisseeError, CompteInvalideError) as erreur:
            db.rollback()
            typer.secho(str(erreur), fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1) from None
        db.commit()

    typer.echo("")
    typer.secho(
        f"  Compte de prélèvement de {dossier} : {compte}.", fg=typer.colors.GREEN, bold=True
    )
    typer.echo("")


@app.command("prelever-echeances")
def prelever_echeances_cmd(
    date_echeance: Annotated[
        str, typer.Option(help="Traite les échéances dues au plus tard cette date (AAAA-MM-JJ).")
    ],
) -> None:
    """Prélève automatiquement (CR5d) les échéances dues, sur les crédits décaissés ayant un
    compte de prélèvement configuré (voir configurer-prelevement).

    Solde insuffisant : prélève ce qui est disponible, jamais en dessous du plancher du compte —
    le reliquat reste dû, retentable les jours suivants. Idempotente pour CE jour (contrainte
    base) : relancée le même jour, elle ne retraite pas une échéance déjà tentée.
    """
    from datetime import date

    from app.modules.credit.prelevement import executer_prelevement

    with SessionLocal() as db:
        rapport = executer_prelevement(db, date_echeance=date.fromisoformat(date_echeance))
    typer.echo("")
    typer.secho(
        f"  Prélèvement au {date_echeance} : {rapport.dossiers_evalues} dossier(s) évalué(s), "
        f"{rapport.prelevements} prélevé(s) pour {rapport.total_preleve} F, "
        f"{rapport.sans_disponible} sans solde disponible.",
        fg=typer.colors.GREEN,
        bold=True,
    )
    if rapport.ignores_deja_tente:
        typer.echo(f"  Déjà tentées ce jour (ignorées) : {rapport.ignores_deja_tente}.")
    if rapport.ignores_rattachement_manquant:
        dossiers = ", ".join(rapport.ignores_rattachement_manquant)
        typer.secho(f"  Rattachement manquant (ignorés) : {dossiers}.", fg=typer.colors.YELLOW)
    typer.echo("")


@app.command("ouvrir-exercice")
def commande_ouvrir_exercice(
    code: Annotated[str, typer.Option(help="Code de l'exercice, ex. « 2026 ».")],
    debut: Annotated[str, typer.Option(help="Date de début (AAAA-MM-JJ).")],
    fin: Annotated[str, typer.Option(help="Date de fin (AAAA-MM-JJ).")],
    label: Annotated[str, typer.Option(help="Libellé.")] = "",
) -> None:
    """Ouvre un exercice comptable. Refuse un chevauchement avec un exercice existant (base)."""
    from datetime import date

    from sqlalchemy.exc import IntegrityError

    with SessionLocal() as db:
        try:
            exercice = ouvrir_exercice(
                db,
                code=code,
                label=label or f"Exercice {code}",
                date_debut=date.fromisoformat(debut),
                date_fin=date.fromisoformat(fin),
            )
            db.commit()
        except IntegrityError as erreur:
            db.rollback()
            detail = (
                "chevauchement avec un exercice existant"
                if "chevauch" in str(erreur)
                else "code déjà utilisé, bornes incohérentes ou chevauchement"
            )
            typer.secho(f"Refus : {detail}.", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1) from None

    typer.echo("")
    typer.secho(
        f"  Exercice {exercice.code} ouvert ({exercice.date_debut} -> {exercice.date_fin}).",
        fg=typer.colors.GREEN,
        bold=True,
    )
    typer.echo("")


@app.command("creer-admin")
def commande_creer_admin(
    username: Annotated[str, typer.Option(help="Identifiant de connexion.")] = "admin",
    email: Annotated[str, typer.Option(help="Adresse de l'administrateur.")] = "admin@imf.local",
    matricule: Annotated[str, typer.Option(help="Matricule interne.")] = "ADM-001",
    nom: Annotated[str, typer.Option(help="Nom de famille.")] = "Administrateur",
    prenom: Annotated[str, typer.Option(help="Prénom.")] = "Compte",
    agence: Annotated[
        str, typer.Option(help="Nom de l'agence siège créée à l'amorçage.")
    ] = "Siège",
    force: Annotated[
        bool,
        typer.Option("--force", help="Créer même si des comptes existent (dépannage)."),
    ] = False,
) -> None:
    """Amorce une installation : agence siège + administrateur, et affiche le mot de passe UNE FOIS.

    À jouer après `seed-security`, sur une base neuve. Sans elle, aucune agence ni aucun
    compte n'existe et personne ne peut se connecter : le logiciel est impossible à démarrer.
    Une IMF a toujours au moins une agence — la commande crée donc le siège et y rattache
    l'administrateur.

    Le mot de passe est généré, affiché une seule fois, et devra être changé à la première
    connexion (`must_change_password`). Il n'est ni stocké en clair, ni journalisé : s'il
    est perdu, il faut recréer le compte.
    """
    with SessionLocal() as db:
        try:
            resultat = creer_admin(
                db,
                username=username,
                email=email,
                matricule=matricule,
                last_name=nom,
                first_name=prenom,
                agence_nom=agence,
                force=force,
            )
        except ComptesDejaPresentsError as erreur:
            db.rollback()
            typer.secho(
                f"Refus : {erreur.args[0]} compte(s) existent déjà.",
                fg=typer.colors.RED,
                err=True,
            )
            typer.echo(
                "Cette commande sert à AMORCER une installation neuve. Créez les comptes\n"
                "suivants via l'API (POST /users), qui les audite et applique le\n"
                "cloisonnement par agence. En cas de blocage total, --force.",
                err=True,
            )
            raise typer.Exit(code=1) from None
        except RoleIntrouvableError as erreur:
            db.rollback()
            typer.secho(
                f"Refus : le rôle {erreur.args[0]} n'existe pas.", fg=typer.colors.RED, err=True
            )
            typer.echo("Jouez d'abord : python -m app.cli seed-security", err=True)
            raise typer.Exit(code=1) from None
        except IntegrityError:
            db.rollback()
            typer.secho(
                "Refus : matricule, identifiant ou adresse déjà utilisés.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=1) from None

        db.commit()

    typer.echo("")
    typer.secho("  Installation amorcée.", fg=typer.colors.GREEN, bold=True)
    typer.echo("")
    typer.echo(f"  Agence        : {resultat.agence_nom} ({resultat.agence_code})")
    typer.echo(f"  Identifiant   : {resultat.username}")
    typer.echo(f"  Adresse       : {resultat.email}")
    typer.secho(f"  Mot de passe  : {resultat.mot_de_passe}", bold=True)
    typer.echo("")
    typer.secho(
        "  Ce mot de passe n'est affiché QU'UNE FOIS et n'est stocké nulle part.",
        fg=typer.colors.YELLOW,
    )
    typer.secho(
        "  Il devra être changé à la première connexion.",
        fg=typer.colors.YELLOW,
    )
    typer.echo("")
