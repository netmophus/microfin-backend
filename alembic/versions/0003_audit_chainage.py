"""Chaînage de audit.audit_logs : chain_hash calculé sous verrou consultatif.

Référence : « Socle Sécurité & Administration — Conception validée » v1.0, §3.2 :
« Chaînage : chain_hash = SHA256(json(record) + previous_chain_hash), sérialisé par un
verrou consultatif (advisory lock) pour garantir la cohérence de la chaîne. »

Le calcul est fait par un trigger BEFORE INSERT, pas par le service Python : le maillon
devient ainsi impossible à contourner, y compris depuis psql ou depuis un futur module
qui oublierait d'appeler le service d'audit. Une chaîne rompue serait irréparable — la
table est immuable (migration 0002). Toute valeur de chain_hash / previous_chain_hash
fournie par l'appelant est écrasée : le maillon n'est pas négociable.

TROIS POINTS VÉRIFIÉS SUR PG16 AVANT ÉCRITURE, qui expliquent des choix non évidents :

1. audit.audit_chain_head (table ajoutée ici, absente du §3.2) est NÉCESSAIRE à la
   correction, pas au confort. occurred_at vaut NOW() = l'heure de DÉBUT de transaction,
   figée jusqu'au commit : l'ordre des occurred_at n'est donc PAS l'ordre d'insertion.
   Chercher le précédent par « ORDER BY occurred_at DESC LIMIT 1 » ferait FOURCHER la
   chaîne — une transaction longue écrit après une transaction courte plus récente, et
   le maillon suivant repointerait sur le même prédécesseur. Un pointeur de tête explicite
   donne l'ordre réel d'insertion, en O(1) et sans dépendre des 12 (puis 60) partitions.

2. SET TimeZone TO 'UTC' sur la fonction n'est pas décoratif : to_jsonb() rend un
   timestamptz selon le fuseau de la SESSION. Le même enregistrement donnerait
   "2026-03-15T10:00:00+00:00" ici et "2026-03-15T19:00:00+09:00" à Tokyo, donc deux
   hashs différents. Sans ce SET, la chaîne ne serait pas rejouable d'une machine à
   l'autre. NE PAS RETIRER (cf. test_hash_independant_du_fuseau_de_session).

3. sha256() est natif en PG16 : aucune extension pgcrypto requise.

Conséquences opérationnelles à connaître :
  - Le verrou sérialise TOUTES les écritures d'audit de l'institution jusqu'au commit.
    C'est le prix d'une chaîne unique : le débit d'audit devient le plafond des
    opérations sensibles concurrentes.
  - Écrire l'audit le plus tard possible dans la transaction. Une transaction qui prend
    le verrou puis attend un verrou de ligne détenu par une autre qui veut le verrou
    d'audit se déadlock. PostgreSQL le détecte et annule l'une des deux — l'opération
    échoue, ce qui reste conforme à C5 (« pas de trace, pas d'opération »).

Hors périmètre, étapes suivantes : vérification / export signé du journal (audit.export),
seed des 11 rôles système et des 17 permissions.

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-16
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Clé réservée au chaînage d'audit. Les clés de verrou consultatif sont globales à la
# base : tenir un registre avant d'en introduire une autre, une collision sérialiserait
# silencieusement deux mécanismes sans rapport.
AUDIT_CHAIN_LOCK_KEY = 20_260_716


def upgrade() -> None:
    # Ligne unique garantie par la PK booléenne + le CHECK : id ne peut valoir que TRUE.
    op.create_table(
        "audit_chain_head",
        sa.Column("id", sa.Boolean(), server_default=sa.true(), nullable=False),
        # NULL avant le tout premier maillon.
        sa.Column("chain_hash", sa.CHAR(64), nullable=True),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("NOW()")),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("id", name="ligne_unique"),
        schema="audit",
    )
    op.execute("INSERT INTO audit.audit_chain_head (id, chain_hash) VALUES (TRUE, NULL)")

    op.execute(
        f"""
        CREATE FUNCTION audit.audit_logs_chain() RETURNS trigger
        LANGUAGE plpgsql
        SET TimeZone TO 'UTC'
        AS $$
        DECLARE
            v_precedent CHAR(64);
            v_charge    TEXT;
        BEGIN
            -- Sérialise les écrivains d'audit jusqu'à la fin de la transaction : deux
            -- maillons ne peuvent pas naître du même prédécesseur.
            PERFORM pg_advisory_xact_lock({AUDIT_CHAIN_LOCK_KEY});

            SELECT chain_hash INTO v_precedent FROM audit.audit_chain_head WHERE id;

            -- Les colonnes de chaîne sont exclues : elles sont le résultat, pas l'entrée.
            -- previous_chain_hash est concaténé à part, conformément à la formule du §3.2.
            v_charge := (to_jsonb(NEW) - 'chain_hash' - 'previous_chain_hash')::text;

            NEW.previous_chain_hash := v_precedent;
            NEW.chain_hash := encode(
                sha256(convert_to(v_charge || coalesce(v_precedent, ''), 'UTF8')), 'hex'
            );

            UPDATE audit.audit_chain_head
               SET chain_hash = NEW.chain_hash, updated_at = clock_timestamp()
             WHERE id;

            RETURN NEW;
        END;
        $$;
        """
    )

    # FOR EACH ROW sur la table parente : cloné sur les 12 partitions et hérité par
    # toute partition créée ensuite (contrairement au trigger TRUNCATE de la 0002).
    op.execute(
        """
        CREATE TRIGGER trg_audit_logs_chain
        BEFORE INSERT ON audit.audit_logs
        FOR EACH ROW EXECUTE FUNCTION audit.audit_logs_chain();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_audit_logs_chain ON audit.audit_logs")
    op.execute("DROP FUNCTION IF EXISTS audit.audit_logs_chain()")
    op.drop_table("audit_chain_head", schema="audit")
