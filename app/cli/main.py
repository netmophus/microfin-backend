"""Point d'entrée de la CLI d'administration : `python -m app.cli <commande>`."""

from typing import Annotated

import typer
from sqlalchemy.exc import IntegrityError

from app.cli.creer_admin import (
    AgenceIntrouvableError,
    ComptesDejaPresentsError,
    RoleIntrouvableError,
    creer_admin,
)
from app.cli.seed_security import executer_seed
from app.core.database import SessionLocal

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


@app.command("creer-admin")
def commande_creer_admin(
    username: Annotated[str, typer.Option(help="Identifiant de connexion.")] = "admin",
    email: Annotated[str, typer.Option(help="Adresse de l'administrateur.")] = "admin@imf.local",
    matricule: Annotated[str, typer.Option(help="Matricule interne.")] = "ADM-001",
    nom: Annotated[str, typer.Option(help="Nom de famille.")] = "Administrateur",
    prenom: Annotated[str, typer.Option(help="Prénom.")] = "Compte",
    agence: Annotated[
        str | None, typer.Option(help="Code d'une agence existante. Facultatif.")
    ] = None,
    force: Annotated[
        bool,
        typer.Option("--force", help="Créer même si des comptes existent (dépannage)."),
    ] = False,
) -> None:
    """Crée le compte administrateur d'installation et affiche son mot de passe UNE FOIS.

    À jouer après `seed-security`, sur une base neuve. Sans elle, aucun compte n'existe et
    personne ne peut se connecter : le logiciel est impossible à démarrer.

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
                agence_code=agence,
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
        except AgenceIntrouvableError as erreur:
            db.rollback()
            typer.secho(
                f"Refus : aucune agence de code « {erreur.args[0]} ».",
                fg=typer.colors.RED,
                err=True,
            )
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
    typer.secho("  Compte administrateur créé.", fg=typer.colors.GREEN, bold=True)
    typer.echo("")
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
