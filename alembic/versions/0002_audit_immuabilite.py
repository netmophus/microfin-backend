"""Immuabilité de audit.audit_logs : rejet de tout UPDATE, DELETE ou TRUNCATE.

Référence : « Socle Sécurité & Administration — Conception validée » v1.0, §3.2 :
« Immuabilité : trigger PostgreSQL audit_logs_immutable() en BEFORE UPDATE OR DELETE
→ RAISE EXCEPTION. »

Le garde-fou TRUNCATE dépasse la lettre du document (validé le 16/07/2026) : sans lui
l'immuabilité garde une porte dérobée, un TRUNCATE ne déclenchant aucun trigger de ligne.

Deux portées, parce que PostgreSQL ne les propage pas de la même façon (vérifié sur
PG16) :

  - BEFORE UPDATE OR DELETE ... FOR EACH ROW, posé sur la table parente, est cloné sur
    les partitions existantes ET hérité par toute partition créée ensuite.
  - BEFORE TRUNCATE ... FOR EACH STATEMENT n'est NI cloné NI hérité. Posé sur la seule
    table parente, il laisse « TRUNCATE audit.audit_logs_2026_03 » effacer une partition
    entière sans rien déclencher. Il est donc créé explicitement sur le parent et sur
    chacune des 12 partitions.

CONSÉQUENCE POUR LE JOB C13 : le job mensuel de partitionnement devra créer le trigger
TRUNCATE sur chaque nouvelle partition. Le trigger de ligne, lui, sera hérité tout seul.

Limite assumée : le propriétaire de la table peut désactiver ses propres triggers. La
garantie repose sur trois niveaux — le trigger empêche, le chain_hash détecte, et en
production le rôle applicatif ne doit pas être propriétaire de audit.audit_logs.

Hors périmètre, étapes suivantes : calcul du chain_hash sous verrou consultatif,
seed des 11 rôles système et des 17 permissions.

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-16
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Doit rester cohérent avec les partitions créées par la migration 0001.
AUDIT_YEAR = 2026
PARTITIONS: tuple[str, ...] = tuple(
    f"audit_logs_{AUDIT_YEAR}_{month:02d}" for month in range(1, 13)
)

# Le parent d'abord : ordre sans importance à la création, mais lisible.
TRUNCATE_CIBLES: tuple[str, ...] = ("audit_logs", *PARTITIONS)


def upgrade() -> None:
    # Aucune branche : la fonction lève systématiquement. Un trigger BEFORE qui
    # échoue annule l'instruction, donc rien n'est modifié, supprimé ni vidé.
    # La même fonction sert aux deux portées : TG_OP distingue les cas.
    op.execute(
        """
        CREATE FUNCTION audit.audit_logs_immutable() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'audit.audit_logs est immuable : % rejeté sur %',
                TG_OP, TG_TABLE_NAME
                USING HINT = 'Le journal d''audit ne peut être ni modifié, ni supprimé, '
                             'ni vidé (conservation BCEAO).';
        END;
        $$;
        """
    )

    # Cloné automatiquement sur les partitions présentes et futures.
    op.execute(
        """
        CREATE TRIGGER trg_audit_logs_immutable
        BEFORE UPDATE OR DELETE ON audit.audit_logs
        FOR EACH ROW EXECUTE FUNCTION audit.audit_logs_immutable();
        """
    )

    # Sans clonage : chaque table doit porter le sien.
    for table in TRUNCATE_CIBLES:
        op.execute(
            f"CREATE TRIGGER trg_{table}_no_truncate "
            f"BEFORE TRUNCATE ON audit.{table} "
            f"FOR EACH STATEMENT EXECUTE FUNCTION audit.audit_logs_immutable();"
        )


def downgrade() -> None:
    for table in reversed(TRUNCATE_CIBLES):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_no_truncate ON audit.{table}")

    # Supprime aussi les clones portés par les partitions.
    op.execute("DROP TRIGGER IF EXISTS trg_audit_logs_immutable ON audit.audit_logs")
    op.execute("DROP FUNCTION IF EXISTS audit.audit_logs_immutable()")
