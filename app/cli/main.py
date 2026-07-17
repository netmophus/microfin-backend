"""Point d'entrée de la CLI d'administration : `python -m app.cli <commande>`."""

from typing import Annotated

import typer

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
    """Installe les 11 rôles système et les 17 permissions du périmètre Sécurité.

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
