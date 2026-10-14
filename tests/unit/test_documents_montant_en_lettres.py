"""Montant en lettres (documents serveur) : le serveur est la seule implémentation (le frontend ne
fabrique plus de document). Table de cas de référence en français correct (traits d'union, « et »,
pluriels de « vingt » et « cent »).
"""


import pytest

from app.modules.documents.montant_en_lettres import montant_en_lettres

CAS: list[tuple[int, str]] = [
    (0, "zéro franc CFA"),
    (1, "un franc CFA"),
    (2, "deux francs CFA"),
    (16, "seize francs CFA"),
    (17, "dix-sept francs CFA"),
    (20, "vingt francs CFA"),
    (21, "vingt et un francs CFA"),
    (22, "vingt-deux francs CFA"),
    (31, "trente et un francs CFA"),
    (61, "soixante et un francs CFA"),
    (70, "soixante-dix francs CFA"),
    (71, "soixante et onze francs CFA"),
    (77, "soixante-dix-sept francs CFA"),
    (80, "quatre-vingts francs CFA"),
    (81, "quatre-vingt-un francs CFA"),
    (90, "quatre-vingt-dix francs CFA"),
    (91, "quatre-vingt-onze francs CFA"),
    (99, "quatre-vingt-dix-neuf francs CFA"),
    (100, "cent francs CFA"),
    (101, "cent un francs CFA"),
    (180, "cent quatre-vingts francs CFA"),
    (200, "deux cents francs CFA"),
    (201, "deux cent un francs CFA"),
    (999, "neuf cent quatre-vingt-dix-neuf francs CFA"),
    (1_000, "mille francs CFA"),
    (1_001, "mille un francs CFA"),
    (2_000, "deux mille francs CFA"),
    (21_000, "vingt et un mille francs CFA"),
    (80_000, "quatre-vingt mille francs CFA"),
    (200_000, "deux cent mille francs CFA"),
    (250_000, "deux cent cinquante mille francs CFA"),
    (1_000_000, "un million de francs CFA"),
    (2_000_000, "deux millions de francs CFA"),
    (2_500_000, "deux millions cinq cent mille francs CFA"),
    (
        12_345_678,
        "douze millions trois cent quarante-cinq mille six cent soixante-dix-huit francs CFA",
    ),
    (80_000_000, "quatre-vingts millions de francs CFA"),
    (200_000_000, "deux cents millions de francs CFA"),
    (1_000_000_000, "un milliard de francs CFA"),
    (2_000_000_000, "deux milliards de francs CFA"),
    (
        999_999_999_999,
        "neuf cent quatre-vingt-dix-neuf milliards neuf cent quatre-vingt-dix-neuf millions "
        "neuf cent quatre-vingt-dix-neuf mille neuf cent quatre-vingt-dix-neuf francs CFA",
    ),
]



@pytest.mark.parametrize(("montant", "lettres"), CAS)
def test_montant_en_lettres(montant: int, lettres: str) -> None:
    assert montant_en_lettres(montant) == lettres


@pytest.mark.parametrize("invalide", [-1, 10.5, float("nan"), 1_000_000_000_000, True, "12"])
def test_refuse_ce_qu_un_recu_ne_doit_jamais_approximer(invalide: object) -> None:
    with pytest.raises(ValueError):
        montant_en_lettres(invalide)  # type: ignore[arg-type]
