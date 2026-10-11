"""Types de pièces — `applies_to` (à quel type de tiers un type de pièce s'applique) + 5 types PM.

Les 7 types livrés par la migration 0007 (CNI, passeport, carte consulaire, permis, carte
d'électeur, deux attestations) sont tous des pièces de PERSONNES PHYSIQUES. Une personne morale ou
un groupement n'a ni CNI ni passeport : il a un RCCM, des statuts, un récépissé… Sans
`applies_to`, le sélecteur de la fiche propose les mêmes pièces à tout le monde.

`applies_to` est un TABLEAU de textes (CHECK : non vide, valeurs parmi individual / legal_entity /
group) : un type comme STATUTS s'applique à la fois aux PM et aux groupements, ce qu'une valeur
unique ne sait pas dire. Défaut = les trois (« tous »), puis les 7 types existants passent à
`{individual}`.

Les 5 types ajoutés sont des DONNÉES de référentiel, posées ici comme en 0007 : elles existent donc
sur toute base après `upgrade`, sans réimport. Aucun format de numéro n'est inventé (laissé NULL,
comme en 0007).

Revision ID: 0060
Revises: 0059
Create Date: 2026-10-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0060"
down_revision: str | None = "0059"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TOUS = "ARRAY['individual','legal_entity','group']::text[]"

# (code, libellé, applies_to, requires_expiry_date, requires_issuer, enforce_unique, ordre)
_NOUVEAUX_TYPES = [
    ("RCCM", "Registre du commerce et du crédit mobilier (RCCM)", ["legal_entity"],
     False, True, True, 110),
    ("STATUTS", "Statuts", ["legal_entity", "group"], False, False, False, 120),
    ("PV_NOMINATION", "Procès-verbal de nomination des dirigeants",
     ["legal_entity", "group"], False, False, False, 130),
    ("RECEPISSE_RECONNAISSANCE", "Récépissé de reconnaissance", ["group"],
     False, True, True, 140),
    ("AGREMENT", "Agrément", ["legal_entity"], False, True, False, 150),
]


def upgrade() -> None:
    op.add_column(
        "identity_document_types",
        sa.Column(
            "applies_to",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text(_TOUS),
            nullable=False,
        ),
        schema="parameters",
    )
    op.create_check_constraint(
        "applies_to_valide",
        "identity_document_types",
        "cardinality(applies_to) > 0 "
        "AND applies_to <@ ARRAY['individual','legal_entity','group']::text[]",
        schema="parameters",
    )
    # Les types existants sont tous des pièces de personnes physiques.
    op.execute(
        "UPDATE parameters.identity_document_types SET applies_to = ARRAY['individual']::text[]"
    )

    types = sa.table(
        "identity_document_types",
        sa.column("code", sa.String),
        sa.column("name", sa.String),
        sa.column("applies_to", postgresql.ARRAY(sa.Text())),
        sa.column("requires_expiry_date", sa.Boolean),
        sa.column("requires_issuer", sa.Boolean),
        sa.column("enforce_unique", sa.Boolean),
        sa.column("acceptance_level", sa.String),
        sa.column("display_order", sa.Integer),
        schema="parameters",
    )
    op.bulk_insert(
        types,
        [
            {
                "code": code,
                "name": nom,
                "applies_to": applicable,
                "requires_expiry_date": expiry,
                "requires_issuer": issuer,
                "enforce_unique": unique,
                "acceptance_level": "standard",
                "display_order": ordre,
            }
            for code, nom, applicable, expiry, issuer, unique, ordre in _NOUVEAUX_TYPES
        ],
    )


def downgrade() -> None:
    codes = ", ".join(f"'{code}'" for code, *_ in _NOUVEAUX_TYPES)
    # Un type déjà utilisé par une pièce fait échouer la suppression (FK) : aucune donnée métier
    # n'est perdue en silence.
    op.execute(f"DELETE FROM parameters.identity_document_types WHERE code IN ({codes})")
    op.drop_constraint(
        "applies_to_valide", "identity_document_types", schema="parameters", type_="check"
    )
    op.drop_column("identity_document_types", "applies_to", schema="parameters")
