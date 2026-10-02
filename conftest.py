"""Bascule de l'URL de base AVANT tout import applicatif — chantier P1ter (isolation tests).

CRITIQUE : pytest charge ce fichier, à la racine, avant tout autre conftest.py et avant toute
collecte de test — donc avant le premier `import app...`. `app/core/database.py` crée son
`engine` UNE SEULE FOIS, à l'import (`engine = create_engine(settings.DATABASE_URL, ...)`) ;
si DATABASE_URL n'est pas déjà la base de TEST à cet instant précis, l'engine applicatif
pointera pour toute la session sur la base de dev (mifin) — sans aucun moyen de rattraper le
tir ensuite (le module est mis en cache par Python, un second `create_engine` ne le remplace
pas). D'où la règle absolue de ce fichier : AUCUN import de `app.*` avant la bascule, et la
bascule est la toute première chose exécutée.

Garde-fou fail-fast : si l'URL effective ne désigne pas explicitement une base dont le nom
contient « test », la suite refuse de démarrer. Objectif : qu'il soit structurellement
impossible de repolluer par accident la base de dev partagée (mifin) depuis les tests.
"""

import os

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://mifin:mifin@localhost:5435/mifin_test"
)
os.environ["DATABASE_URL"] = TEST_DATABASE_URL

_nom_base = TEST_DATABASE_URL.rsplit("/", 1)[-1].split("?", 1)[0]
if "test" not in _nom_base.lower():
    raise RuntimeError(
        f"Garde-fou : TEST_DATABASE_URL désigne la base « {_nom_base} », dont le nom ne "
        "contient pas « test ». La suite refuse de démarrer plutôt que de risquer d'écrire "
        "sur une base qui ne serait pas une base de test jetable (ex. la base de dev mifin)."
    )
