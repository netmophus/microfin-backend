"""Numérotation atomique des tiers — le garde-fou principal de T1b.

Le numéro lisible (M-2026-0000001) figure sur le livret d'épargne du membre, ses reçus et les
archives comptables de 10 ans. Deux agents qui créent une fiche au même instant ne doivent
JAMAIS recevoir le même numéro : l'erreur serait visible par le client final.

MÉCANISME — une seule instruction atomique, verrou de ligne sur la ligne (prefix, year) :

    INSERT ... VALUES (:prefix, :annee, 1)
    ON CONFLICT (prefix, year) DO UPDATE SET last_value = last_value + 1 ...
    RETURNING last_value

C'est le même verrou de ligne que le FOR UPDATE employé ailleurs (compteur d'échecs 3b,
rotation des sessions 3c), condensé en une instruction. Le second appelant BLOQUE jusqu'au
commit du premier, puis reprend sur last_value + 1. L'ON CONFLICT absorbe en prime la course
du tout premier numéro d'une clé (deux agents créant la ligne au même instant).

L'incrément vit dans la MÊME transaction que la création de la fiche (câblée en T1c) : si la
création échoue et rollback, l'incrément est annulé — PAS de trou consommé par un échec. Une
numérotation sans trous est plus défendable devant un auditeur qu'une séquence à trous
inexpliqués. Défense en profondeur : la contrainte UNIQUE(tier_number) rattraperait une
collision si le verrou venait à manquer.

ANNÉE — tirée de NOW() côté base, PAS de l'horloge Python. created_at de la fiche a pour
défaut NOW() ; en prenant l'année de NOW() (transaction_timestamp, figé au début de
transaction), le millésime du numéro et l'année de created_at CONCORDENT par construction.
Une transaction du 31/12 qui commit le 01/01 produit donc un numéro de l'année de DÉBUT — le
même millésime que sa propre created_at, ce qui est correct. Le fuseau est ÉPINGLÉ en UTC
(comme les bornes des partitions d'audit, migration 0001) : un serveur mal configuré sur un
autre fuseau ne doit pas décaler la frontière d'année.

La séquence repart à 1 à chaque nouvelle année, sans cron : la clé (prefix, year) fait qu'un
premier appel d'une année neuve ne trouve aucune ligne et INSÈRE last_value = 1.
"""

from sqlalchemy import text
from sqlalchemy.orm import Session

# Préfixes par type de tiers — en dur (D8). La nomenclature paramétrable par IMF viendra plus
# tard. Réutilisé par le service de création (T1c) pour mapper tier_type -> préfixe.
PREFIXES_PAR_TYPE: dict[str, str] = {
    "individual": "M",
    "legal_entity": "P",
    "group": "G",
}

# Longueur minimale du compteur (zéro-padding). Ne tronque pas au-delà : un compteur qui
# dépasserait 9 999 999 dans l'année produirait simplement un numéro plus long, toujours unique.
_LARGEUR_COMPTEUR = 7

_UPSERT = text(
    """
    INSERT INTO tiers.numbering_sequences (prefix, year, last_value)
    VALUES (:prefix, :annee, 1)
    ON CONFLICT (prefix, year)
    DO UPDATE SET last_value = tiers.numbering_sequences.last_value + 1,
                  updated_at = NOW()
    RETURNING last_value
    """
)

# Année courante côté base, fuseau épinglé en UTC (cf. docstring).
_ANNEE_COURANTE = text("SELECT EXTRACT(YEAR FROM NOW() AT TIME ZONE 'UTC')::int")


def prefixe_pour_type(tier_type: str) -> str:
    """Rend le préfixe de numérotation d'un type de tiers ('individual' -> 'M')."""
    return PREFIXES_PAR_TYPE[tier_type]


def prochain_numero_pour_annee(db: Session, prefix: str, annee: int) -> str:
    """Alloue atomiquement le prochain numéro pour (prefix, annee) et le formate.

    Primitive atomique : une seule instruction sous verrou de ligne. Prend une année
    explicite — c'est le point d'entrée testable (rollover d'année sans attendre 2027) et,
    accessoirement, la porte pour une numérotation par exercice si une IMF le demande.
    """
    valeur: int = db.execute(_UPSERT, {"prefix": prefix, "annee": annee}).scalar_one()
    return f"{prefix}-{annee}-{valeur:0{_LARGEUR_COMPTEUR}d}"


def prochain_numero(db: Session, prefix: str) -> str:
    """Alloue le prochain numéro pour l'année courante (NOW() côté base, UTC).

    Point d'entrée de production. L'année et created_at de la fiche viennent tous deux de
    NOW() dans la même transaction, donc concordent.
    """
    annee: int = db.execute(_ANNEE_COURANTE).scalar_one()
    return prochain_numero_pour_annee(db, prefix, annee)
