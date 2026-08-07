"""Crédit CR5c — reclassification automatique de l'encours et du provisionnement.

Job MANUEL (bouton réservé DIRECTION, `credit.delinquency.executer` — même patron que
l'exécution des intérêts épargne E5, pas un cron caché) : parcourt tous les crédits décaissés,
recalcule le palier applicable (paramétrage CR5a) et reclasse si ça diffère du palier
actuellement enregistré (`credit.applications.delinquency_tier_id`).

RÈGLE UNIQUE, jamais de cas spécial : une ligne d'écriture n'est postée QUE si son montant
calculé est > 0 (le moteur comptable refuse tout montant nul). Un crédit classé en souffrance
qui devient intégralement soldé voit son encours retomber à 0 tout seul — `rembourser()` crédite
déjà le compte COURANT de l'encours (voir `compte_encours_courant` dans remboursement.py) à
chaque versement, il n'y a donc plus rien à déplacer à ce stade — mais la provision accumulée,
elle, doit être reprise en entier.

PROVISION JAMAIS NETTÉE EN DELTA : chaque palier a son PROPRE compte 299x (pas un pool commun
entre paliers). Passer d'un palier à un autre REPREND intégralement la provision de l'ancien
(sur son `compte_reprise_id`, depuis son `compte_provision_id`) et REDOTE intégralement celle
du nouveau (sur son `compte_dotation_id`, vers son `compte_provision_id`) — les deux écritures
peuvent coexister dans un même reclassement (ex. SOUFFRANCE -> DOUTEUX).

DEUX/TROIS ÉCRITURES CONSTRUITES EN CODE (PAS via poser_depuis_schema) : même décision que
CR4/CR5b (voir remboursement.py) — les comptes sont dynamiques, résolus par palier au moment du
job, pas configurés une fois pour toutes dans un modèle d'écriture à seeder. Journal OD
(opérations diverses) : aucun mouvement d'espèces, reclassement comptable interne pur — même
choix que le décaissement en mode 'epargne' (voir decaissement.py).

PALIERS TERMINAUX (`is_terminal`) : jamais reclassés automatiquement, quel que soit le nouveau
jours_retard calculé. La sortie d'un crédit irrécouvrable (radiation) est un acte manuel séparé,
hors périmètre CR5c.

CHAQUE DOSSIER EST COMMITTÉ SÉPARÉMENT (voir executer_reclassification, même patron que
epargne.interets.verser_interets) : un paramétrage incomplet sur un dossier ne bloque pas le
traitement des autres.

APERÇU (`previsualiser_reclassement`, dry-run) : on MONTRE avant de poser de vraies écritures de
dotation/reprise sur potentiellement tout le portefeuille — même exigence que le versement
d'intérêts épargne (E5). N'est PAS un refactor de `reclasser_un_credit` (le chemin qui écrit,
déjà testé, n'est pas touché) : réutilise les 4 fonctions déjà PURES et publiques ci-dessous
(`jours_de_retard`, `palier_applicable`, `encours_actuel`, `compte_encours_courant`), et
duplique en lecture seule les 4 contrôles de rattachement de `reclasser_un_credit` — même
patron que `epargne/interets.py` (`previsualiser_interets`/`_verser_un` sont deux implémentations
distinctes qui partagent les primitives de calcul, pas une fonction commune). Si les messages de
refus de `reclasser_un_credit` changent, les mettre à jour ICI aussi (repère : « MÊME CONTRÔLE »).
"""

import uuid
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.modules.audit.service import CONTEXTE_VIDE, ContexteRequete, ecrire_audit
from app.modules.comptabilite import ecritures
from app.modules.comptabilite.ecritures import LigneSaisie
from app.modules.comptabilite.models import Journal, JournalEntry
from app.modules.credit.decaissement import RattachementManquantError
from app.modules.credit.demandes import RESSOURCE
from app.modules.credit.models import Application, DelinquencyEvent, DelinquencyTier
from app.modules.credit.remboursement import compte_encours_courant, prochaine_echeance

CODE_JOURNAL = "OD"


def encours_actuel(db: Session, application_id: uuid.UUID) -> int:
    """Capital restant dû TOTAL de ce crédit à cet instant — 0 si intégralement soldé. Tient
    compte d'un versement partiel CR5b sur l'échéance en cours (la part capital déjà versée
    dessus est déduite, dérivée de montant_paye/interets, aucune colonne dédiée — même
    discipline que la ventilation de rembourser())."""
    echeance = prochaine_echeance(db, application_id)
    if echeance is None:
        return 0
    encours_avant_cette_echeance = echeance.capital_restant_du + echeance.capital
    part_capital_deja_versee = max(0, echeance.montant_paye - echeance.interets)
    return encours_avant_cette_echeance - part_capital_deja_versee


def jours_de_retard(db: Session, application_id: uuid.UUID, *, aujourdhui: date) -> int:
    """Jours écoulés depuis l'échéance due de la plus ancienne installment non soldée — 0 si le
    crédit est à jour ou intégralement soldé."""
    echeance = prochaine_echeance(db, application_id)
    if echeance is None:
        return 0
    return max(0, (aujourdhui - echeance.due_date).days)


def palier_applicable(db: Session, jours_retard: int) -> DelinquencyTier | None:
    """Le palier dont le seuil est le plus grand <= jours_retard — None si aucun ne correspond
    (crédit sain). `seuil_jours` sert LUI-MÊME de clé de tri (voir CR5a)."""
    return db.execute(
        select(DelinquencyTier)
        .where(DelinquencyTier.seuil_jours <= jours_retard)
        .order_by(DelinquencyTier.seuil_jours.desc())
        .limit(1)
    ).scalar_one_or_none()


def _poser_deux_lignes(
    db: Session,
    *,
    compte_debit: uuid.UUID,
    compte_credit: uuid.UUID,
    montant: int,
    description: str,
    journal_id: uuid.UUID,
    jour: date,
    par: uuid.UUID | None,
    contexte: ContexteRequete,
) -> JournalEntry:
    entry = ecritures.creer_brouillon(
        db,
        journal_id=journal_id,
        entry_date=jour,
        description=description,
        lignes=[
            LigneSaisie(account_id=compte_debit, side="D", amount=montant, label=description),
            LigneSaisie(account_id=compte_credit, side="C", amount=montant, label=description),
        ],
        par=par,
    )
    ecritures.valider(db, entry, par, contexte=contexte)
    return entry


def reclasser_un_credit(
    db: Session,
    demande: Application,
    *,
    aujourdhui: date,
    par: uuid.UUID | None,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> DelinquencyEvent | None:
    """Reclasse UN crédit décaissé si sa situation le justifie — None si rien ne change (aucun
    événement écrit, aucune écriture posée) : soit le palier calculé est identique à l'actuel,
    soit le dossier est gelé dans un palier terminal. Voir docstring module pour la règle des
    montants nuls et le traitement de la provision (jamais nettée, toujours reprise+dotation
    séparées)."""
    tier_avant = (
        db.get(DelinquencyTier, demande.delinquency_tier_id)
        if demande.delinquency_tier_id is not None
        else None
    )
    if tier_avant is not None and tier_avant.is_terminal:
        return None

    jours = jours_de_retard(db, demande.id, aujourdhui=aujourdhui)
    tier_apres = palier_applicable(db, jours)
    id_avant = tier_avant.id if tier_avant is not None else None
    id_apres = tier_apres.id if tier_apres is not None else None
    if id_avant == id_apres:
        return None

    encours = encours_actuel(db, demande.id)
    ancien_compte_encours = compte_encours_courant(db, demande)
    if tier_apres is not None:
        if tier_apres.compte_encours_id is None:
            raise RattachementManquantError(
                f"le palier « {tier_apres.libelle} » n'a pas de compte d'encours rattaché "
                "(paramétrage)"
            )
        nouveau_compte_encours = tier_apres.compte_encours_id
    else:
        assert demande.compte_credit_id is not None
        nouveau_compte_encours = demande.compte_credit_id

    journal_id = db.execute(select(Journal.id).where(Journal.code == CODE_JOURNAL)).scalar_one()

    entry_id_encours: uuid.UUID | None = None
    montant_encours_reclasse = 0
    # Deux paliers peuvent partager le même compte d'encours (CR5a) : rien à déplacer si le
    # compte ne change pas, même si l'encours est non nul.
    if encours > 0 and nouveau_compte_encours != ancien_compte_encours:
        entry = _poser_deux_lignes(
            db,
            compte_debit=nouveau_compte_encours,
            compte_credit=ancien_compte_encours,
            montant=encours,
            description=f"Reclassement encours crédit {demande.application_number}",
            journal_id=journal_id,
            jour=aujourdhui,
            par=par,
            contexte=contexte,
        )
        entry_id_encours = entry.id
        montant_encours_reclasse = encours

    dernier_evenement = db.execute(
        select(DelinquencyEvent)
        .where(DelinquencyEvent.application_id == demande.id)
        .order_by(DelinquencyEvent.executed_at.desc())
        .limit(1)
    ).scalars().first()
    provision_avant = dernier_evenement.provision_apres if dernier_evenement else 0
    taux_cible = tier_apres.taux_provision_bp if tier_apres is not None else 0
    provision_apres = (encours * taux_cible) // 10000

    entry_id_reprise: uuid.UUID | None = None
    if provision_avant > 0:
        if tier_avant is None:
            # Ne devrait pas arriver : provision_avant > 0 implique qu'un palier a déjà existé
            # pour ce dossier (voir dernier_evenement ci-dessus).
            raise RattachementManquantError(
                "provision positive sans palier antérieur connu — incohérence de données"
            )
        if tier_avant.compte_provision_id is None or tier_avant.compte_reprise_id is None:
            raise RattachementManquantError(
                f"le palier « {tier_avant.libelle} » n'a pas de compte de provision/reprise "
                "rattaché (paramétrage)"
            )
        entry = _poser_deux_lignes(
            db,
            compte_debit=tier_avant.compte_provision_id,
            compte_credit=tier_avant.compte_reprise_id,
            montant=provision_avant,
            description=f"Reprise provision crédit {demande.application_number}",
            journal_id=journal_id,
            jour=aujourdhui,
            par=par,
            contexte=contexte,
        )
        entry_id_reprise = entry.id

    entry_id_dotation: uuid.UUID | None = None
    if provision_apres > 0:
        assert tier_apres is not None  # taux_cible > 0 => tier_apres existe
        if tier_apres.compte_dotation_id is None or tier_apres.compte_provision_id is None:
            raise RattachementManquantError(
                f"le palier « {tier_apres.libelle} » n'a pas de compte de dotation/provision "
                "rattaché (paramétrage)"
            )
        entry = _poser_deux_lignes(
            db,
            compte_debit=tier_apres.compte_dotation_id,
            compte_credit=tier_apres.compte_provision_id,
            montant=provision_apres,
            description=f"Dotation provision crédit {demande.application_number}",
            journal_id=journal_id,
            jour=aujourdhui,
            par=par,
            contexte=contexte,
        )
        entry_id_dotation = entry.id

    demande.delinquency_tier_id = id_apres
    db.flush()

    event = DelinquencyEvent(
        application_id=demande.id,
        executed_by=par,
        jours_retard=jours,
        tier_avant_id=id_avant,
        tier_apres_id=id_apres,
        encours_actuel=encours,
        montant_encours_reclasse=montant_encours_reclasse,
        provision_avant=provision_avant,
        provision_apres=provision_apres,
        entry_id_encours=entry_id_encours,
        entry_id_reprise=entry_id_reprise,
        entry_id_dotation=entry_id_dotation,
    )
    db.add(event)
    db.flush()

    ecrire_audit(
        db,
        action="credit.delinquency.reclasse",
        contexte=contexte,
        acteur_id=par,
        resource_type=RESSOURCE,
        resource_id=demande.id,
        agency_id=demande.agency_id,
        old_values={
            "delinquency_tier_id": str(id_avant) if id_avant else None,
            "provision": provision_avant,
        },
        new_values={
            "delinquency_tier_id": str(id_apres) if id_apres else None,
            "provision": provision_apres,
            "jours_retard": jours,
        },
    )
    return event


@dataclass(frozen=True)
class LigneReclassement:
    """UN dossier réellement reclassé — palier avant/après, en clair (pas un UUID nu) pour
    l'écran : un contrôleur doit pouvoir lire le rapport sans aller rouvrir chaque dossier."""

    application_number: str
    tier_avant_code: str | None
    tier_avant_libelle: str | None
    tier_apres_code: str | None
    tier_apres_libelle: str | None
    jours_retard: int
    encours_actuel: int
    provision_avant: int
    provision_apres: int


@dataclass
class RapportReclassement:
    """Résultat d'une exécution du job — même esprit que RapportInterets (épargne E5)."""

    dossiers_evalues: int = 0
    reclasses: int = 0
    ignores_rattachement_manquant: list[str] = field(default_factory=list)
    lignes: list[LigneReclassement] = field(default_factory=list)


def _libelles_paliers(db: Session) -> dict[uuid.UUID, tuple[str, str]]:
    """(code, libelle) de tous les paliers, par id — chargé UNE fois, pour ne pas refaire un
    aller-retour base par ligne de rapport."""
    return {
        p.id: (p.code, p.libelle)
        for p in db.execute(select(DelinquencyTier)).scalars()
    }


def executer_reclassification(
    db: Session,
    *,
    aujourdhui: date | None = None,
    par: uuid.UUID | None = None,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> RapportReclassement:
    """Traitement de masse : reclasse tous les crédits décaissés dont la situation le justifie.

    CHAQUE DOSSIER EST COMMITTÉ SÉPARÉMENT (même patron que epargne.interets.verser_interets) :
    un paramétrage incomplet (compte manquant sur un palier) fait échouer CE dossier seul —
    rollback, comptabilisé dans `ignores_rattachement_manquant`, le job continue sur les
    suivants plutôt que de tout bloquer."""
    jour = aujourdhui
    if jour is None:
        jour = db.execute(text("SELECT CURRENT_DATE")).scalar_one()

    ids = list(
        db.execute(select(Application.id).where(Application.status == "decaisse")).scalars()
    )
    paliers = _libelles_paliers(db)
    rapport = RapportReclassement()
    for application_id in ids:
        demande = db.get(Application, application_id)
        assert demande is not None
        rapport.dossiers_evalues += 1
        try:
            event = reclasser_un_credit(db, demande, aujourdhui=jour, par=par, contexte=contexte)
        except RattachementManquantError:
            db.rollback()
            rapport.ignores_rattachement_manquant.append(demande.application_number)
            continue
        if event is None:
            # Rien n'a été touché (palier inchangé ou gel terminal) : PAS de rollback ici — un
            # rollback sans rien à défaire ne fait qu'annuler du travail en attente d'ailleurs
            # dans la même session (observé en test : ça défaisait le palier de test posé juste
            # avant l'appel, encore non committé au moment où un dossier SANS rapport passait
            # en premier dans la boucle).
            continue
        db.commit()
        rapport.reclasses += 1
        avant = paliers.get(event.tier_avant_id) if event.tier_avant_id else None
        apres = paliers.get(event.tier_apres_id) if event.tier_apres_id else None
        rapport.lignes.append(
            LigneReclassement(
                application_number=demande.application_number,
                tier_avant_code=avant[0] if avant else None,
                tier_avant_libelle=avant[1] if avant else None,
                tier_apres_code=apres[0] if apres else None,
                tier_apres_libelle=apres[1] if apres else None,
                jours_retard=event.jours_retard,
                encours_actuel=event.encours_actuel,
                provision_avant=event.provision_avant,
                provision_apres=event.provision_apres,
            )
        )
    return rapport


# --- Aperçu (dry-run) : voir docstring module ----------------------------------------------


@dataclass(frozen=True)
class LigneApercuReclassement:
    """UN dossier qui SERAIT reclassé — même forme que LigneReclassement, plus le motif de
    refus s'il y en aurait un (paramétrage incomplet, connu SANS rien écrire)."""

    application_number: str
    tier_avant_code: str | None
    tier_avant_libelle: str | None
    tier_apres_code: str | None
    tier_apres_libelle: str | None
    jours_retard: int
    encours_actuel: int
    provision_avant: int
    provision_apres: int
    rattachement_manquant: str | None


@dataclass
class ApercuReclassement:
    """Ce que `executer_reclassification` FERAIT — aucune écriture, aucune mutation. Ne liste
    QUE les dossiers dont le palier changerait réellement (comme l'aperçu intérêts ne montre
    que ce qui reste à verser) : un dossier sain ne dit rien d'utile dans ce rapport."""

    dossiers_evalues: int = 0
    a_reclasser: int = 0
    rattachements_manquants: int = 0
    lignes: list[LigneApercuReclassement] = field(default_factory=list)


def previsualiser_reclassement(
    db: Session, *, aujourdhui: date | None = None
) -> ApercuReclassement:
    """Calcule ce que `executer_reclassification` ferait, SANS RIEN ÉCRIRE — voir docstring
    module pour pourquoi ce n'est pas un refactor de `reclasser_un_credit`. Détecte aussi, par
    dossier, un rattachement manquant qui ferait échouer ce dossier à l'exécution — mêmes 4
    contrôles que `reclasser_un_credit`, dupliqués ici en lecture seule (repère « MÊME
    CONTRÔLE » dans les deux fonctions si l'un des messages change)."""
    jour = aujourdhui
    if jour is None:
        jour = db.execute(text("SELECT CURRENT_DATE")).scalar_one()

    ids = list(
        db.execute(select(Application.id).where(Application.status == "decaisse")).scalars()
    )
    paliers = _libelles_paliers(db)
    apercu = ApercuReclassement()

    for application_id in ids:
        demande = db.get(Application, application_id)
        assert demande is not None
        apercu.dossiers_evalues += 1

        tier_avant = (
            db.get(DelinquencyTier, demande.delinquency_tier_id)
            if demande.delinquency_tier_id is not None
            else None
        )
        if tier_avant is not None and tier_avant.is_terminal:
            continue  # gelé — jamais reclassé automatiquement, même à l'aperçu

        jours = jours_de_retard(db, demande.id, aujourdhui=jour)
        tier_apres = palier_applicable(db, jours)
        id_avant = tier_avant.id if tier_avant is not None else None
        id_apres = tier_apres.id if tier_apres is not None else None
        if id_avant == id_apres:
            continue  # rien ne changerait pour ce dossier

        encours = encours_actuel(db, demande.id)

        rattachement_manquant: str | None = None
        # MÊME CONTRÔLE que reclasser_un_credit (compte d'encours du nouveau palier).
        if tier_apres is not None and tier_apres.compte_encours_id is None:
            rattachement_manquant = (
                f"le palier « {tier_apres.libelle} » n'a pas de compte d'encours rattaché "
                "(paramétrage)"
            )

        dernier_evenement = (
            db.execute(
                select(DelinquencyEvent)
                .where(DelinquencyEvent.application_id == demande.id)
                .order_by(DelinquencyEvent.executed_at.desc())
                .limit(1)
            )
            .scalars()
            .first()
        )
        provision_avant = dernier_evenement.provision_apres if dernier_evenement else 0
        taux_cible = tier_apres.taux_provision_bp if tier_apres is not None else 0
        provision_apres = (encours * taux_cible) // 10000

        # MÊME CONTRÔLE que reclasser_un_credit (reprise de la provision de l'ancien palier).
        if rattachement_manquant is None and provision_avant > 0:
            if tier_avant is None:
                rattachement_manquant = (
                    "provision positive sans palier antérieur connu — incohérence de données"
                )
            elif tier_avant.compte_provision_id is None or tier_avant.compte_reprise_id is None:
                rattachement_manquant = (
                    f"le palier « {tier_avant.libelle} » n'a pas de compte de provision/reprise "
                    "rattaché (paramétrage)"
                )

        # MÊME CONTRÔLE que reclasser_un_credit (dotation du nouveau palier).
        if rattachement_manquant is None and provision_apres > 0:
            assert tier_apres is not None
            if tier_apres.compte_dotation_id is None or tier_apres.compte_provision_id is None:
                rattachement_manquant = (
                    f"le palier « {tier_apres.libelle} » n'a pas de compte de dotation/provision "
                    "rattaché (paramétrage)"
                )

        if rattachement_manquant is not None:
            apercu.rattachements_manquants += 1
        apercu.a_reclasser += 1
        avant = paliers.get(id_avant) if id_avant else None
        apres = paliers.get(id_apres) if id_apres else None
        apercu.lignes.append(
            LigneApercuReclassement(
                application_number=demande.application_number,
                tier_avant_code=avant[0] if avant else None,
                tier_avant_libelle=avant[1] if avant else None,
                tier_apres_code=apres[0] if apres else None,
                tier_apres_libelle=apres[1] if apres else None,
                jours_retard=jours,
                encours_actuel=encours,
                provision_avant=provision_avant,
                provision_apres=provision_apres,
                rattachement_manquant=rattachement_manquant,
            )
        )
    return apercu
