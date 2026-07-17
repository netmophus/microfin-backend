"""Protection des rôles système : rejet du DELETE sur security.roles quand is_system.

Référence : « Socle Sécurité & Administration — Conception validée » v1.0, §4 :
« Ces 11 rôles sont créés en seed avec is_system = true (non modifiables, non
supprimables). » — jusqu'ici cette phrase n'était qu'une description : is_system était un
simple booléen, sans trigger ni contrainte. Un rôle système pouvait être renommé et
supprimé librement (vérifié en base le 16/07/2026).

PORTÉE VOLONTAIREMENT PARTIELLE (option B, validée le 16/07/2026) :

  - DELETE d'un rôle système : bloqué ici, en base. Supprimer un rôle livré avec le
    produit n'est jamais légitime, donc aucun appelant n'a besoin de cette porte.
  - UPDATE d'un rôle système : PAS bloqué, à dessein. Le seed (app/cli/seed_security.py)
    upserte les 11 rôles à chaque montée de version pour resynchroniser leurs
    définitions ; un trigger UPDATE tuerait cette convergence. La protection contre
    l'UPDATE *illégitime* (via l'API) relève du service Sécurité, conformément au §6
    (« règles appliquées au niveau du service, pas seulement de l'API »).

L'écart est assumé et couvert par un test qui le documente
(test_update_dun_role_systeme_reste_possible_en_base).

Le WHEN (OLD.is_system) est essentiel : les rôles personnalisés d'une IMF doivent rester
supprimables (permission roles.delete).

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-16
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION security.roles_prevent_system_delete() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'Le rôle système « % » ne peut pas être supprimé', OLD.code
                USING HINT = 'is_system = true : les rôles livrés avec le produit sont '
                             'permanents. Seuls les rôles personnalisés sont supprimables.';
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_roles_prevent_system_delete
        BEFORE DELETE ON security.roles
        FOR EACH ROW WHEN (OLD.is_system)
        EXECUTE FUNCTION security.roles_prevent_system_delete();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_roles_prevent_system_delete ON security.roles")
    op.execute("DROP FUNCTION IF EXISTS security.roles_prevent_system_delete()")
