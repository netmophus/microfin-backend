"""Coordonnées T2b — flag phone_normalized + backfill de tiers.primary_phone dans contacts.

Deux choses :

1. AJOUT DU FLAG tiers.contacts.phone_normalized (défaut TRUE). Il marque les numéros
   enregistrés par FORÇAGE (la bibliothèque ne les a pas validés). But : rendre le forçage
   MESURABLE (un job pourra re-normaliser après mise à jour de phonenumbers ; le Décisionnel
   suivra le « % de forcés ») — un garde-fou dont on ne mesure jamais l'usage cesse d'en être un.

2. BACKFILL de tiers.tiers.primary_phone -> un contact téléphone PRINCIPAL, normalisé au mieux
   (forçage best-effort : on ne perd aucun numéro legacy). IDEMPOTENT : ne touche pas un tiers
   qui a déjà un téléphone principal (rejouable après downgrade/re-upgrade sans doublon).

   La colonne primary_phone RESTE (rien ne casse) ; on la droppera dans une migration ultérieure
   quand plus aucun code ne la lira.

DOWNGRADE : retire le flag. Les contacts backfillés RESTENT — primary_phone étant intact, aucune
donnée n'est perdue ; les identifier pour les supprimer exigerait de deviner lesquels.

Revision ID: 0010
Revises: 0009
Create Date: 2026-07-23
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.modules.tiers.telephone import TelephoneInvalideError, normaliser

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_A_MIGRER = sa.text(
    """
    SELECT id, primary_phone FROM tiers.tiers
     WHERE primary_phone IS NOT NULL AND btrim(primary_phone) <> '' AND deleted_at IS NULL
       AND NOT EXISTS (
         SELECT 1 FROM tiers.contacts c
          WHERE c.tier_id = tiers.tiers.id AND c.contact_type = 'phone'
            AND c.is_primary AND c.deleted_at IS NULL
       )
    """
)

_INSERER = sa.text(
    """
    INSERT INTO tiers.contacts
        (tier_id, contact_type, contact_subtype, phone_raw, phone_number,
         phone_country_code, phone_normalized, is_primary)
    VALUES (:t, 'phone', 'mobile', :raw, :e164, :cc, :norm, TRUE)
    """
)


def upgrade() -> None:
    op.add_column(
        "contacts",
        sa.Column("phone_normalized", sa.Boolean(), server_default=sa.true(), nullable=False),
        schema="tiers",
    )

    conn = op.get_bind()
    for tier_id, brut in conn.execute(_A_MIGRER).fetchall():
        try:
            # Forçage best-effort : un numéro legacy ne doit jamais être perdu à la migration.
            resultat = normaliser(brut, forcer=True)
        except TelephoneInvalideError:
            # Legacy inexploitable (trop court / charabia) : on le laisse sur primary_phone.
            continue
        conn.execute(
            _INSERER,
            {
                "t": tier_id,
                "raw": brut,
                "e164": resultat.e164,
                "cc": resultat.country_code,
                "norm": resultat.normalise,
            },
        )


def downgrade() -> None:
    op.drop_column("contacts", "phone_normalized", schema="tiers")
