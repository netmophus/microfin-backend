"""Validation de l'import du plan de comptes — PURE, sans base ni fichier.

Le cœur du « tout ou rien » : `valider` doit rendre TOUTES les anomalies d'un coup (pas
s'arrêter à la première), et refuser exactement les cas prévus. Un fichier propre ne rend
aucune anomalie ; un fichier fautif les liste toutes, chacune rattachée à sa ligne.
"""

from app.modules.comptabilite.plan import LigneBrute, valider


def _ligne(
    ligne: int,
    numero: str,
    *,
    name: str = "Compte",
    classe: str | None = None,
    parent: str = "",
    sens: str = "D",
    posting: str = "TRUE",
    system: str = "TRUE",
) -> LigneBrute:
    return LigneBrute(
        ligne=ligne,
        account_number=numero,
        name=name,
        short_name="",
        classe=classe if classe is not None else (numero[:1] or ""),
        parent_number=parent,
        normal_side=sens,
        is_posting=posting,
        is_system=system,
        notes="",
    )


def _plan_minimal() -> list[LigneBrute]:
    # Un mini-plan cohérent : classe 3, un titre et deux feuilles.
    return [
        _ligne(2, "3", sens="C", posting="FALSE"),
        _ligne(3, "31", sens="C", posting="FALSE", parent="3"),
        _ligne(4, "3111", name="Épargne à vue", sens="C", parent="31"),
        _ligne(5, "3121", name="Dépôt à terme", sens="C", parent="31"),
    ]


def test_un_plan_coherent_ne_leve_aucune_anomalie() -> None:
    assert valider(_plan_minimal()) == []


def test_numero_en_double_est_signale() -> None:
    lignes = _plan_minimal()
    lignes.append(_ligne(6, "3111", name="Doublon", sens="C", parent="31"))

    anomalies = valider(lignes)

    assert len(anomalies) == 1
    assert anomalies[0].ligne == 6
    assert "double" in anomalies[0].probleme


def test_parent_introuvable_est_signale() -> None:
    lignes = _plan_minimal()
    # 3115 référence un parent 3119 qui n'existe pas dans le fichier.
    lignes.append(_ligne(6, "3115", name="Orphelin", sens="C", parent="3119"))

    anomalies = valider(lignes)

    assert len(anomalies) == 1
    assert anomalies[0].account_number == "3115"
    assert "3119" in anomalies[0].probleme and "introuvable" in anomalies[0].probleme


def test_parent_non_prefixe_est_signale() -> None:
    lignes = _plan_minimal()
    # 3131 existe et 3121 aussi, mais 3121 n'est pas un préfixe de 3131 : hiérarchie incohérente.
    lignes.append(_ligne(6, "3131", name="Mal rattaché", sens="C", parent="3121"))

    anomalies = valider(lignes)

    assert len(anomalies) == 1
    assert "préfixe" in anomalies[0].probleme


def test_sens_invalide_est_signale() -> None:
    anomalies = valider([_ligne(2, "3", sens="X", posting="FALSE")])

    assert any("sens" in a.probleme for a in anomalies)


def test_classe_incoherente_avec_le_numero_est_signalee() -> None:
    # Numéro commençant par 3 mais déclaré classe 4.
    anomalies = valider([_ligne(2, "3111", classe="4", sens="C")])

    assert any("incohérente" in a.probleme for a in anomalies)


def test_booleen_invalide_est_signale() -> None:
    anomalies = valider([_ligne(2, "3", classe="3", sens="C", posting="OUI")])

    assert any("is_posting" in a.probleme for a in anomalies)


def test_libelle_vide_est_signale() -> None:
    anomalies = valider([_ligne(2, "3", name="", sens="C", posting="FALSE")])

    assert any("libellé" in a.probleme for a in anomalies)


def test_toutes_les_anomalies_sont_collectees_pas_seulement_la_premiere() -> None:
    # Trois lignes fautives, sur trois défauts DIFFÉRENTS : on veut les trois d'un coup.
    lignes = [
        _ligne(2, "3", sens="C", posting="FALSE"),
        _ligne(3, "3111", name="", sens="C", parent="3"),  # libellé vide + parent non préfixe...
        _ligne(4, "4111", classe="9", sens="Z"),  # classe incohérente + sens invalide
        _ligne(5, "", sens="C"),  # numéro vide
    ]

    anomalies = valider(lignes)
    lignes_fautives = {a.ligne for a in anomalies}

    # Chaque ligne fautive est représentée : rien n'est masqué par un arrêt précoce.
    assert lignes_fautives == {3, 4, 5}
    assert len(anomalies) >= 4
