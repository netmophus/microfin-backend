"""Import du plan de comptes depuis le CSV RCSFD — tout ou rien.

Le plan de comptes est une DONNÉE. Cette brique lit le CSV, VALIDE tout le fichier EN MÉMOIRE,
et n'écrit RIEN tant qu'une seule anomalie subsiste. En cas d'erreur, elle les remonte TOUTES,
ligne par ligne : jamais de plan à moitié importé, jamais un import qui s'arrête à la première
faute en laissant deviner les suivantes.

Découpage : `lire_csv` (fichier → lignes brutes), `valider` (lignes → anomalies, PURE et donc
testable sans base ni fichier), `importer` (orchestration : lit, valide, refuse en bloc ou écrit
dans une transaction que l'appelant committe). L'import est idempotent : upsert par
account_number, rejouable à chaque installation ou mise à jour du référentiel.

Tout compte importé est marqué PROVISOIRE (`is_provisional = TRUE`) : les valeurs du CSV sont
un modèle, à faire valider par un expert-comptable SFD (voir docs/conformite-comptable.md).
"""

import csv
import uuid
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.orm import Session

COLONNES_ATTENDUES = frozenset(
    {
        "account_number",
        "name",
        "short_name",
        "class",
        "parent_number",
        "normal_side",
        "is_posting",
        "is_system",
        "notes",
    }
)

_BOOLEENS = {"TRUE": True, "FALSE": False}


@dataclass(frozen=True)
class LigneBrute:
    """Une ligne du CSV, telle que lue, avec son numéro de ligne fichier (en-tête = 1)."""

    ligne: int
    account_number: str
    name: str
    short_name: str
    classe: str
    parent_number: str
    normal_side: str
    is_posting: str
    is_system: str
    notes: str


@dataclass(frozen=True)
class Anomalie:
    """Un défaut précis, rattaché à sa ligne et au compte fautif."""

    ligne: int
    account_number: str
    probleme: str

    def __str__(self) -> str:
        compte = f" (compte {self.account_number})" if self.account_number else ""
        return f"ligne {self.ligne}{compte} : {self.probleme}"


@dataclass
class RapportImport:
    """Bilan d'un import réussi."""

    crees: int = 0
    mis_a_jour: int = 0
    anomalies: list[Anomalie] = field(default_factory=list)


class FichierInvalideError(Exception):
    """Le CSV est illisible ou n'a pas les colonnes attendues (échec avant toute ligne)."""


class ImportRefuseError(Exception):
    """Au moins une anomalie : rien n'est écrit. Porte la liste complète."""

    def __init__(self, anomalies: list[Anomalie]) -> None:
        self.anomalies = anomalies
        super().__init__(f"{len(anomalies)} anomalie(s) — aucun compte écrit")


def lire_csv(chemin: str) -> list[LigneBrute]:
    """Lit le fichier en lignes brutes. Lève FichierInvalideError si l'en-tête est mauvais."""
    with open(chemin, encoding="utf-8-sig", newline="") as f:
        lecteur = csv.DictReader(f, delimiter=";")
        entete = set(lecteur.fieldnames or [])
        manquantes = COLONNES_ATTENDUES - entete
        if manquantes:
            raise FichierInvalideError(f"colonnes manquantes : {', '.join(sorted(manquantes))}")

        lignes: list[LigneBrute] = []
        for index, brut in enumerate(lecteur, start=2):  # ligne 1 = en-tête
            lignes.append(
                LigneBrute(
                    ligne=index,
                    account_number=(brut.get("account_number") or "").strip(),
                    name=(brut.get("name") or "").strip(),
                    short_name=(brut.get("short_name") or "").strip(),
                    classe=(brut.get("class") or "").strip(),
                    parent_number=(brut.get("parent_number") or "").strip(),
                    normal_side=(brut.get("normal_side") or "").strip().upper(),
                    is_posting=(brut.get("is_posting") or "").strip().upper(),
                    is_system=(brut.get("is_system") or "").strip().upper(),
                    notes=(brut.get("notes") or "").strip(),
                )
            )
    return lignes


def valider(lignes: list[LigneBrute]) -> list[Anomalie]:
    """Valide TOUT le fichier et rend TOUTES les anomalies. Pure : ni base, ni fichier.

    Règles :
      - champs obligatoires (numéro, libellé, sens, is_posting, is_system, classe) présents ;
      - numéro UNIQUE dans le fichier ;
      - classe = entier 1..9 ET égale au 1er chiffre du numéro ;
      - sens ∈ {D, C} ; is_posting et is_system ∈ {TRUE, FALSE} ;
      - parent : s'il est fourni, il doit EXISTER dans le fichier ET être un PRÉFIXE du numéro
        (la hiérarchie du plan RCSFD suit le numéro : parent de 1011 = 101).
    """
    anomalies: list[Anomalie] = []
    numeros = {li.account_number for li in lignes if li.account_number}
    vus: set[str] = set()

    for li in lignes:
        num = li.account_number

        if not num:
            anomalies.append(Anomalie(li.ligne, "", "numéro de compte vide"))
            continue
        if num in vus:
            anomalies.append(Anomalie(li.ligne, num, f"numéro « {num} » en double dans le fichier"))
        vus.add(num)

        if not li.name:
            anomalies.append(Anomalie(li.ligne, num, "libellé (name) vide"))

        if li.normal_side not in ("D", "C"):
            anomalies.append(
                Anomalie(li.ligne, num, f"sens « {li.normal_side} » invalide (attendu D ou C)")
            )

        if li.is_posting not in _BOOLEENS:
            anomalies.append(
                Anomalie(li.ligne, num, f"is_posting « {li.is_posting} » invalide (TRUE/FALSE)")
            )
        if li.is_system not in _BOOLEENS:
            anomalies.append(
                Anomalie(li.ligne, num, f"is_system « {li.is_system} » invalide (TRUE/FALSE)")
            )

        classe_ok = li.classe.isdigit() and 1 <= int(li.classe) <= 9
        if not classe_ok:
            anomalies.append(
                Anomalie(li.ligne, num, f"classe « {li.classe} » invalide (attendu 1 à 9)")
            )
        elif not num[0].isdigit() or int(li.classe) != int(num[0]):
            anomalies.append(
                Anomalie(
                    li.ligne,
                    num,
                    f"classe {li.classe} incohérente avec le 1er chiffre du numéro « {num} »",
                )
            )

        if li.parent_number:
            if li.parent_number not in numeros:
                anomalies.append(
                    Anomalie(
                        li.ligne, num, f"parent_number « {li.parent_number} » introuvable"
                    )
                )
            elif not num.startswith(li.parent_number) or num == li.parent_number:
                anomalies.append(
                    Anomalie(
                        li.ligne,
                        num,
                        f"parent « {li.parent_number} » n'est pas un préfixe du numéro « {num} »",
                    )
                )

    return anomalies


_UPSERT = text(
    """
    INSERT INTO comptabilite.accounts
        (account_number, name, short_name, account_class, parent_id,
         normal_side, is_posting, is_system, is_provisional, is_active, notes, created_by)
    VALUES
        (:account_number, :name, :short_name, :account_class, :parent_id,
         :normal_side, :is_posting, :is_system, TRUE, TRUE, :notes, :created_by)
    ON CONFLICT (account_number) DO UPDATE SET
        name          = EXCLUDED.name,
        short_name    = EXCLUDED.short_name,
        account_class = EXCLUDED.account_class,
        parent_id     = EXCLUDED.parent_id,
        normal_side   = EXCLUDED.normal_side,
        is_posting    = EXCLUDED.is_posting,
        is_system     = EXCLUDED.is_system,
        notes         = EXCLUDED.notes,
        updated_at    = NOW(),
        updated_by    = EXCLUDED.created_by
    RETURNING (xmax = 0) AS cree
    """
)


def importer(db: Session, chemin: str, importe_par: uuid.UUID | None = None) -> RapportImport:
    """Importe le plan. Refuse tout en bloc si une anomalie subsiste. Ne committe pas.

    Écrit les parents avant les enfants (tri par longueur de numéro) pour résoudre parent_id
    au fil de l'eau. En cas d'anomalie, lève ImportRefuseError SANS rien écrire.
    """
    lignes = lire_csv(chemin)
    anomalies = valider(lignes)
    if anomalies:
        raise ImportRefuseError(anomalies)

    rapport = RapportImport()
    ids: dict[str, uuid.UUID] = {}  # account_number -> id, alimenté au fur et à mesure

    # Parents avant enfants : le préfixe le plus court d'abord (validé, donc sûr).
    for li in sorted(lignes, key=lambda x: (len(x.account_number), x.account_number)):
        cree = db.execute(
            _UPSERT,
            {
                "account_number": li.account_number,
                "name": li.name,
                "short_name": li.short_name or None,
                "account_class": int(li.classe),
                "parent_id": ids.get(li.parent_number),
                "normal_side": li.normal_side,
                "is_posting": _BOOLEENS[li.is_posting],
                "is_system": _BOOLEENS[li.is_system],
                "notes": li.notes or None,
                "created_by": importe_par,
            },
        ).scalar_one()
        # Récupère l'id réel (upsert : la ligne peut préexister), pour les enfants.
        ids[li.account_number] = db.execute(
            text("SELECT id FROM comptabilite.accounts WHERE account_number = :n"),
            {"n": li.account_number},
        ).scalar_one()
        if cree:
            rapport.crees += 1
        else:
            rapport.mis_a_jour += 1

    return rapport
