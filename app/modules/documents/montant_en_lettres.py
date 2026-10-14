"""Montant en francs CFA entiers écrit en toutes lettres, pour les pièces justificatives.

Seule implémentation du produit (le frontend ne fabrique plus de document) : fonction pure, sans
dépendance. Les cas de référence sont dans `tests/unit/test_documents_montant_en_lettres.py`.
"""

UNITES = (
    "zéro",
    "un",
    "deux",
    "trois",
    "quatre",
    "cinq",
    "six",
    "sept",
    "huit",
    "neuf",
    "dix",
    "onze",
    "douze",
    "treize",
    "quatorze",
    "quinze",
    "seize",
)

DIZAINES = (
    "",
    "",
    "vingt",
    "trente",
    "quarante",
    "cinquante",
    "soixante",
    "soixante",
    "quatre-vingt",
    "quatre-vingt",
)

MILLE = 1_000
MILLION = 1_000_000
MILLIARD = 1_000_000_000
LIMITE = 1_000_000_000_000  # exclue : on écrit jusqu'aux centaines de milliards


def _moins_de_cent(n: int, final: bool) -> str:
    """0 à 99. « vingt » et « quatre-vingt » prennent un « s » seulement quand ils terminent le
    nombre (`final`) : « quatre-vingts », mais « quatre-vingt-un » et « quatre-vingt mille »."""
    if n < 17:
        return UNITES[n]
    if n < 20:
        return f"dix-{UNITES[n - 10]}"
    dizaine, unite = divmod(n, 10)
    if dizaine in (7, 9):
        # soixante-dix… ; quatre-vingt-dix… : 10 à 19 s'ajoutent à la dizaine précédente.
        suite = n - (dizaine - 1) * 10
        base = DIZAINES[dizaine]
        if dizaine == 7 and suite == 11:
            return f"{base} et onze"
        return f"{base}-{_moins_de_cent(suite, final)}"
    base = DIZAINES[dizaine]
    if unite == 0:
        return f"{base}s" if dizaine == 8 and final else base
    if unite == 1 and dizaine != 8:
        return f"{base} et un"
    return f"{base}-{UNITES[unite]}"


def _moins_de_mille(n: int, final: bool) -> str:
    """0 à 999. « cent » prend un « s » quand il est multiplié ET termine le nombre."""
    if n < 100:
        return _moins_de_cent(n, final)
    centaines, reste = divmod(n, 100)
    if centaines == 1:
        cent = "cent"
    else:
        cent = f"{UNITES[centaines]} cent{'s' if reste == 0 and final else ''}"
    return cent if reste == 0 else f"{cent} {_moins_de_cent(reste, final)}"


def _assembler(n: int) -> str:
    if n == 0:
        return "zéro"
    milliards, reste_milliard = divmod(n, MILLIARD)
    millions, reste_million = divmod(reste_milliard, MILLION)
    milliers, reste = divmod(reste_million, MILLE)
    morceaux: list[str] = []
    if milliards > 0:
        pluriel = "s" if milliards > 1 else ""
        morceaux.append(f"{_moins_de_mille(milliards, True)} milliard{pluriel}")
    if millions > 0:
        pluriel = "s" if millions > 1 else ""
        morceaux.append(f"{_moins_de_mille(millions, True)} million{pluriel}")
    if milliers > 0:
        # « mille » est invariable et ne prend pas de « un » devant : « mille », « deux mille ».
        morceaux.append("mille" if milliers == 1 else f"{_moins_de_mille(milliers, False)} mille")
    if reste > 0:
        morceaux.append(_moins_de_mille(reste, True))
    return " ".join(morceaux)


def montant_en_lettres(montant: int) -> str:
    """« deux millions de francs CFA ». Refuse (ValueError) un montant négatif, non entier ou à
    partir de mille milliards : un reçu ne doit jamais afficher une somme approximative."""
    if isinstance(montant, bool) or not isinstance(montant, int) or not 0 <= montant < LIMITE:
        raise ValueError(f"Montant impossible à écrire en lettres : {montant}")
    nombre = _assembler(montant)
    # Un million / un milliard « ronds » se lient au nom par « de » : « deux millions de francs ».
    rond = montant >= MILLION and montant % MILLION == 0
    unite = "francs CFA" if montant >= 2 else "franc CFA"
    return f"{nombre} {'de ' if rond else ''}{unite}"
