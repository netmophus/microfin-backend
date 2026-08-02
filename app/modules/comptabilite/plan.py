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
import hashlib
import io
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.modules.comptabilite.models import Account

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


def lire_lignes(source) -> list[LigneBrute]:
    """Lit un CSV déjà ouvert (fichier ou flux en mémoire) en lignes brutes. Lève
    FichierInvalideError si l'en-tête est mauvais. Cœur partagé par lire_csv (CLI, chemin
    disque) et lire_bytes (web, fichier uploadé) : un seul endroit qui sait lire le format."""
    lecteur = csv.DictReader(source, delimiter=";")
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


def lire_csv(chemin: str) -> list[LigneBrute]:
    """Lit le fichier en lignes brutes. Lève FichierInvalideError si l'en-tête est mauvais."""
    with open(chemin, encoding="utf-8-sig", newline="") as f:
        return lire_lignes(f)


def lire_bytes(contenu: bytes) -> list[LigneBrute]:
    """Même lecture, depuis un fichier déjà en mémoire (upload web) plutôt qu'un chemin disque."""
    try:
        texte = contenu.decode("utf-8-sig")
    except UnicodeDecodeError as erreur:
        raise FichierInvalideError(
            "le fichier n'est pas un texte lisible (encodage attendu : UTF-8)."
        ) from erreur
    return lire_lignes(io.StringIO(texte))


def empreinte(contenu: bytes) -> str:
    """Empreinte du fichier — garantit à la confirmation qu'aucun fichier différent ne s'est
    substitué à celui vu à l'aperçu."""
    return hashlib.sha256(contenu).hexdigest()


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


def importer_lignes(
    db: Session,
    lignes: list[LigneBrute],
    importe_par: uuid.UUID | None = None,
    *,
    lever_provisoire: bool = False,
) -> RapportImport:
    """Écrit des lignes DÉJÀ LUES. Refuse tout en bloc si une anomalie subsiste. Ne committe pas.

    Écrit les parents avant les enfants (tri par longueur de numéro) pour résoudre parent_id
    au fil de l'eau. En cas d'anomalie, lève ImportRefuseError SANS rien écrire.

    lever_provisoire=True marque les comptes CRÉÉS OU MODIFIÉS par cet import comme définitifs
    (is_provisional=FALSE) — réservé à l'import qui EST la validation de l'expert-comptable,
    jamais le comportement par défaut d'une correction intermédiaire.
    """
    anomalies = valider(lignes)
    if anomalies:
        raise ImportRefuseError(anomalies)

    rapport = RapportImport()
    ids: dict[str, uuid.UUID] = {}  # account_number -> id, alimenté au fur et à mesure
    touches: list[str] = []

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
        touches.append(li.account_number)
        if cree:
            rapport.crees += 1
        else:
            rapport.mis_a_jour += 1

    if lever_provisoire and touches:
        db.execute(
            text(
                "UPDATE comptabilite.accounts SET is_provisional = FALSE "
                "WHERE account_number = ANY(:numeros)"
            ),
            {"numeros": touches},
        )

    return rapport


def importer(db: Session, chemin: str, importe_par: uuid.UUID | None = None) -> RapportImport:
    """Importe le plan depuis un fichier disque (CLI). Voir importer_lignes."""
    return importer_lignes(db, lire_csv(chemin), importe_par)


_CHAMPS_APERCU = (
    "name", "short_name", "account_class", "parent_number",
    "normal_side", "is_posting", "is_system", "notes",
)


@dataclass(frozen=True)
class DiffChamp:
    """Un champ qui changerait, en langage lisible (pas les valeurs brutes du CSV)."""

    champ: str
    avant: str
    apres: str


@dataclass(frozen=True)
class CompteApercu:
    account_number: str
    name: str
    diffs: tuple[DiffChamp, ...] = ()


@dataclass(frozen=True)
class RapportApercu:
    """Ce que l'import ferait, sans rien écrire — pour relecture avant confirmation."""

    a_creer: list[CompteApercu]
    a_modifier: list[CompteApercu]
    inchanges: int


def _texte(valeur: object) -> str:
    if valeur is None or valeur == "":
        return "(vide)"
    if isinstance(valeur, bool):
        return "Oui" if valeur else "Non"
    return str(valeur)


def previsualiser(db: Session, lignes: list[LigneBrute]) -> RapportApercu:
    """Compare un CSV DÉJÀ VALIDÉ à l'état actuel de la base — ne modifie rien. Pour chaque
    compte existant, ne retient que les champs qui changeraient réellement (pas de bruit sur
    les lignes identiques)."""
    numeros = [li.account_number for li in lignes]
    existants = list(
        db.execute(select(Account).where(Account.account_number.in_(numeros))).scalars()
    )
    par_numero = {c.account_number: c for c in existants}
    ids_parents = {c.parent_id for c in existants if c.parent_id is not None}
    numeros_parents: dict[uuid.UUID, str] = {}
    if ids_parents:
        numeros_parents = dict(
            db.execute(
                select(Account.id, Account.account_number).where(Account.id.in_(ids_parents))
            ).all()
        )

    a_creer: list[CompteApercu] = []
    a_modifier: list[CompteApercu] = []
    inchanges = 0

    for li in lignes:
        existant = par_numero.get(li.account_number)
        if existant is None:
            a_creer.append(CompteApercu(li.account_number, li.name))
            continue

        parent_actuel = numeros_parents.get(existant.parent_id) if existant.parent_id else None
        cible = {
            "name": li.name,
            "short_name": li.short_name or None,
            "account_class": int(li.classe),
            "parent_number": li.parent_number or None,
            "normal_side": li.normal_side,
            "is_posting": _BOOLEENS[li.is_posting],
            "is_system": _BOOLEENS[li.is_system],
            "notes": li.notes or None,
        }
        actuel = {
            "name": existant.name,
            "short_name": existant.short_name,
            "account_class": existant.account_class,
            "parent_number": parent_actuel,
            "normal_side": existant.normal_side,
            "is_posting": existant.is_posting,
            "is_system": existant.is_system,
            "notes": existant.notes,
        }
        diffs = tuple(
            DiffChamp(champ, _texte(actuel[champ]), _texte(cible[champ]))
            for champ in _CHAMPS_APERCU
            if actuel[champ] != cible[champ]
        )
        if diffs:
            a_modifier.append(CompteApercu(li.account_number, li.name, diffs))
        else:
            inchanges += 1

    return RapportApercu(a_creer=a_creer, a_modifier=a_modifier, inchanges=inchanges)


COLONNES_EXPORT = (
    "account_number", "name", "short_name", "class", "parent_number", "normal_side",
    "is_posting", "is_system", "notes", "is_provisional", "is_active",
)


def exporter_csv(db: Session, *, inclure_inactifs: bool = True) -> str:
    """Exporte le plan de comptes — MÊMES colonnes que l'import (ré-importable telle quelle),
    plus deux colonnes informatives (is_provisional, is_active) que l'import ignore à la
    relecture (COLONNES_ATTENDUES ne les exige pas)."""
    stmt = select(Account).order_by(Account.account_number)
    if not inclure_inactifs:
        stmt = stmt.where(Account.is_active)
    comptes = list(db.execute(stmt).scalars())

    numeros = {c.id: c.account_number for c in comptes}
    ids_manquants = {
        c.parent_id for c in comptes if c.parent_id is not None and c.parent_id not in numeros
    }
    if ids_manquants:  # parent hors du périmètre exporté (ex. désactivé, filtré) — résolu à part
        numeros.update(
            dict(
                db.execute(
                    select(Account.id, Account.account_number).where(Account.id.in_(ids_manquants))
                ).all()
            )
        )

    tampon = io.StringIO()
    ecrivain = csv.DictWriter(tampon, fieldnames=COLONNES_EXPORT, delimiter=";")
    ecrivain.writeheader()
    for c in comptes:
        ecrivain.writerow(
            {
                "account_number": c.account_number,
                "name": c.name,
                "short_name": c.short_name or "",
                "class": c.account_class,
                "parent_number": numeros.get(c.parent_id, "") if c.parent_id else "",
                "normal_side": c.normal_side,
                "is_posting": "TRUE" if c.is_posting else "FALSE",
                "is_system": "TRUE" if c.is_system else "FALSE",
                "notes": c.notes or "",
                "is_provisional": "TRUE" if c.is_provisional else "FALSE",
                "is_active": "TRUE" if c.is_active else "FALSE",
            }
        )
    return tampon.getvalue()
