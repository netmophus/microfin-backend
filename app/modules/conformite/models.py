"""Modèles ORM du schéma « conformite » — socle des ratios prudentiels RCSFD (lot P2.1.a,
migration 0057), ENTIÈREMENT PARAMÉTRABLE.

Mappent l'existant, ne créent rien. FK et CHECK reflètent EXACTEMENT la migration.

Aucune formule, aucun seuil, aucune composition d'agrégat codée en dur dans l'application —
tout vit dans ces cinq tables. Voir le docstring de la migration 0057 pour le détail du
modèle ; résumé ici :
  - AgregatPrudentiel (+ AgregatCompte) : définit un agrégat réglementaire, BALANCE (somme
    signée de soldes par préfixe de compte RCSFD) ou SPECIAL (calcul dédié nommé).
  - RatioPrudentiel (+ RatioSeuil) : numérateur/dénominateur + opérateur de conformité (GE/LE)
    + seuil(s), éventuellement différents par catégorie de SFD.
  - ParametreInstitution : SINGLETON, catégorie réglementaire de CETTE institution (mono-tenant
    par déploiement) + complément de provisions sous tutelle.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

UUID = postgresql.UUID(as_uuid=True)
TS = sa.TIMESTAMP(timezone=True)
NOW = sa.text("NOW()")
GEN_UUID = sa.text("gen_random_uuid()")
FK_USER = "security.users.id"
FK_AGREGAT = "conformite.agregat_prudentiel.id"
FK_RATIO = "conformite.ratio_prudentiel.id"


class AgregatPrudentiel(Base):
    """Un agrégat réglementaire — brique de numérateur ou dénominateur d'un ratio.

    `type = 'BALANCE'` : la composition vit dans `AgregatCompte` (préfixes de compte + sens).
    `type = 'SPECIAL'` : `calcul_special` nomme la fonction dédiée que le moteur (lot P2.1.b)
    devra dispatcher (ex. `'PLUS_GROS_EMPRUNTEUR'`) — non réductible à une somme de comptes.
    Les deux colonnes sont mutuellement cohérentes (CHECK en base, pas seulement ici)."""

    __tablename__ = "agregat_prudentiel"
    __table_args__: tuple[Any, ...] = ({"schema": "conformite"},)

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, server_default=GEN_UUID)
    code: Mapped[str] = mapped_column(sa.String(40), nullable=False, unique=True)
    libelle: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    reference: Mapped[str | None] = mapped_column(sa.String(200))
    type: Mapped[str] = mapped_column(sa.String(10), nullable=False)
    calcul_special: Mapped[str | None] = mapped_column(sa.String(50))
    nets_de_provisions: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.false()
    )
    # Booléen GÉNÉRIQUE (lot P2.1.c) : si TRUE, le moteur déduit
    # parametre_institution.complement_provisions_tutelle du résultat de CET agrégat. Aucun
    # agrégat n'est câblé en dur dans le moteur — c'est cette colonne qui pilote.
    applique_complement_provisions_tutelle: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.false()
    )
    is_system: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.false())
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey(FK_USER))
    updated_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey(FK_USER))

    def __repr__(self) -> str:
        return f"<AgregatPrudentiel {self.code} ({self.type})>"


class AgregatCompte(Base):
    """Une ligne de composition d'un agrégat BALANCE : un préfixe de numéro de compte RCSFD
    (`account_number LIKE prefixe || '%'`, résolu par le moteur) + un sens (+1 absorbe la masse
    dans l'agrégat, -1 la déduit — modélise les comptes contra et les déductions
    réglementaires). Un même préfixe n'apparaît qu'une fois par agrégat (UNIQUE)."""

    __tablename__ = "agregat_compte"
    __table_args__: tuple[Any, ...] = (
        sa.UniqueConstraint("agregat_id", "prefixe_compte"),
        sa.Index("ix_conformite_agregat_compte_agregat", "agregat_id"),
        {"schema": "conformite"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, server_default=GEN_UUID)
    agregat_id: Mapped[uuid.UUID] = mapped_column(
        UUID, sa.ForeignKey(FK_AGREGAT, ondelete="CASCADE"), nullable=False
    )
    prefixe_compte: Mapped[str] = mapped_column(sa.String(20), nullable=False)
    sens: Mapped[int] = mapped_column(sa.SmallInteger, nullable=False)
    is_system: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.false())
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey(FK_USER))

    def __repr__(self) -> str:
        signe = "+" if self.sens > 0 else "-"
        return f"<AgregatCompte {signe}{self.prefixe_compte}>"


class RatioPrudentiel(Base):
    """Un ratio prudentiel = numérateur / dénominateur (deux agrégats) + un opérateur de
    conformité. `operateur = 'GE'` : le ratio DOIT être >= seuil (ex. capitalisation).
    `operateur = 'LE'` : DOIT être <= seuil (ex. division des risques). `actif = FALSE` retire
    le ratio du tableau de bord sans le supprimer — utilisé pour les ratios qui dépendent d'un
    gap de données non encore comblé (lot P2.1.c)."""

    __tablename__ = "ratio_prudentiel"
    __table_args__: tuple[Any, ...] = ({"schema": "conformite"},)

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, server_default=GEN_UUID)
    code: Mapped[str] = mapped_column(sa.String(40), nullable=False, unique=True)
    libelle: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    reference_reglementaire: Mapped[str | None] = mapped_column(sa.String(200))
    agregat_numerateur_id: Mapped[uuid.UUID] = mapped_column(
        UUID, sa.ForeignKey(FK_AGREGAT), nullable=False
    )
    agregat_denominateur_id: Mapped[uuid.UUID] = mapped_column(
        UUID, sa.ForeignKey(FK_AGREGAT), nullable=False
    )
    operateur: Mapped[str] = mapped_column(sa.String(2), nullable=False)
    actif: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.true())
    ordre: Mapped[int] = mapped_column(sa.Integer, nullable=False, unique=True)
    is_system: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.false())
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey(FK_USER))
    updated_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey(FK_USER))

    def __repr__(self) -> str:
        return f"<RatioPrudentiel {self.code}>"


class RatioSeuil(Base):
    """Le seuil d'un ratio, pour une catégorie de SFD donnée (`categorie_sfd = NULL` : seuil
    unique pour toute institution, sinon un seuil propre à `'SANS_DEPOTS'`/`'AFFILIE'`/
    `'NON_AFFILIE'`). Au plus un seuil par (ratio, catégorie) — y compris pour NULL, via l'index
    unique sur `COALESCE(categorie_sfd, '*')` posé par la migration."""

    __tablename__ = "ratio_seuil"
    __table_args__: tuple[Any, ...] = (
        # NULL n'est jamais égal à NULL : COALESCE force une valeur comparable ('*' = seuil
        # universel) pour que l'unicité tienne aussi quand categorie_sfd est NULL.
        sa.Index(
            "uq_conformite_ratio_seuil_categorie",
            "ratio_id",
            sa.text("COALESCE(categorie_sfd, '*')"),
            unique=True,
        ),
        {"schema": "conformite"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, server_default=GEN_UUID)
    ratio_id: Mapped[uuid.UUID] = mapped_column(
        UUID, sa.ForeignKey(FK_RATIO, ondelete="CASCADE"), nullable=False
    )
    categorie_sfd: Mapped[str | None] = mapped_column(sa.String(20))
    valeur_seuil: Mapped[Decimal] = mapped_column(sa.Numeric(7, 4), nullable=False)
    is_system: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.false())
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey(FK_USER))
    updated_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey(FK_USER))

    def __repr__(self) -> str:
        return f"<RatioSeuil {self.categorie_sfd or 'TOUS'}={self.valeur_seuil}%>"


class ParametreInstitution(Base):
    """SINGLETON — un déploiement = une institution (mono-tenant confirmé par le diagnostic
    préalable, voir app/core/config.py). `categorie_sfd` détermine quel `RatioSeuil` s'applique
    à CETTE institution. `complement_provisions_tutelle` : ajustement administratif des fonds
    propres effectifs, montant en F CFA entier, 0 par défaut (jamais déduit tant que non
    renseigné). `singleton` forcé à TRUE par CHECK + UNIQUE (migration) : au plus une ligne,
    imposé par la base — même patron que `caisse.parametres` (CA2, migration 0043)."""

    __tablename__ = "parametre_institution"
    __table_args__: tuple[Any, ...] = (
        sa.UniqueConstraint("singleton", name="uq_parametre_institution_singleton"),
        {"schema": "conformite"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, server_default=GEN_UUID)
    categorie_sfd: Mapped[str] = mapped_column(sa.String(20), nullable=False)
    complement_provisions_tutelle: Mapped[int] = mapped_column(
        sa.BigInteger, nullable=False, server_default=sa.text("0")
    )
    singleton: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.true())
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey(FK_USER))
    updated_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey(FK_USER))

    def __repr__(self) -> str:
        return f"<ParametreInstitution {self.categorie_sfd}>"
