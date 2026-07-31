"""Rapprochement du CAPITAL — Σ des parts libérées (auxiliaire) ↔ solde comptable 1021 (général).

Même loi que le rapprochement de l'épargne (Σ soldes ↔ 3111), mais sur le capital social. Le
compte 1021 (parts libérées) porte le TOTAL du capital versé par les membres ; le DÉTAIL par
membre vit dans `tiers.member_shares`. Invariant :

    Σ (parts libérées x valeur d'une part)  ==  solde comptable de 1021

Un écart est le premier signe d'une anomalie (mouvement de parts sans écriture, ou écriture 1021
sans mouvement). Deux tables distinctes -> vrai contrôle croisé, à lancer périodiquement.

HYPOTHÈSE (provisoire) : valeur d'une part CONSTANTE. Si l'IMF la change, l'auxiliaire calculé sur
les comptes x valeur COURANTE divergerait du capital historique -> il faudrait historiser la valeur.
"""

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.modules.tiers.parts import ParametrageManquantError, _config


@dataclass(frozen=True)
class RapprochementCapital:
    compte_general: str  # numéro du compte du plan (1021)
    auxiliaire: int  # Σ (parts libérées x valeur d'une part)
    general: int  # NET comptable 1021 - 1022 (capital réellement libéré, écritures validées)
    concordant: bool
    ecart: int  # auxiliaire moins general (0 si concordant)


def rapprocher_capital_libere(db: Session) -> RapprochementCapital:
    """Rapproche le capital LIBÉRÉ (Σ parts libérées x valeur) au NET comptable 1021 - 1022.

    Motif capital souscrit appelé/non appelé : la souscription-engagement crédite 1021 (montant
    souscrit) et débite 1022 (part non libérée, une créance). Le capital RÉELLEMENT libéré n'est
    donc pas 1021 seul mais le NET 1021 - 1022 = Σ(C-D) sur les DEUX comptes. À la libération, on
    crédite 1022 (la créance s'éteint) : le net monte. Invariant : Σ libérées x valeur == net.
    """
    config = _config(db)
    if config.compte_parts_liberees_id is None:
        raise ParametrageManquantError("Le compte des parts libérées (1021) n'est pas rattaché.")

    parts_liberees = db.execute(
        text("SELECT COALESCE(SUM(shares_liberees), 0) FROM tiers.member_shares")
    ).scalar_one()
    auxiliaire = int(parts_liberees) * config.unit_value

    comptes = [config.compte_parts_liberees_id]
    if config.compte_parts_non_liberees_id is not None:
        comptes.append(config.compte_parts_non_liberees_id)
    # NET libéré = Σ(crédit - débit) sur 1021 ET 1022 : le débit 1022 (non libéré) retranche
    # ce qui n'a pas encore été payé.
    general = db.execute(
        text(
            "SELECT COALESCE(SUM(CASE WHEN l.side = 'C' THEN l.amount ELSE -l.amount END), 0) "
            "FROM comptabilite.journal_lines l "
            "JOIN comptabilite.journal_entries e ON e.id = l.entry_id "
            "WHERE l.account_id = ANY(:comptes) AND e.status = 'validee'"
        ),
        {"comptes": comptes},
    ).scalar_one()

    numero = db.execute(
        text("SELECT account_number FROM comptabilite.accounts WHERE id = :c"),
        {"c": config.compte_parts_liberees_id},
    ).scalar_one()

    return RapprochementCapital(
        compte_general=numero,
        auxiliaire=int(auxiliaire),
        general=int(general),
        concordant=(int(auxiliaire) == int(general)),
        ecart=int(auxiliaire) - int(general),
    )
