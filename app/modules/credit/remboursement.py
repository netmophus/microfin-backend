"""Crédit CR4/CR5b/CR5c — remboursements : encaisser une échéance, D CAISSE / C <compte
courant de l'encours> / C PRODUITS_INTERETS. Paiement PARTIEL (CR5b, migration 0037) : un ou
plusieurs versements peuvent régler une même échéance, jusqu'à solde.

COMPTE COURANT DE L'ENCOURS (CR5c, migration 0038) : la ligne capital ne crédite plus toujours
`compte_credit_id` (l'ancrage figé au décaissement) — elle crédite `compte_encours_courant()`,
qui vaut le compte du palier de souffrance si le dossier est classé, sinon cet ancrage. Sans ce
changement, un remboursement continuerait de créditer le compte sain même après un passage en
souffrance : le solde de la classe 29 (créances en souffrance) ne s'apurerait jamais, même quand
le client rembourse réellement. Voir `app/modules/credit/reclassification.py`.

PAS DE GATE KYC ICI, à la différence du décaissement : encaisser de l'argent qui RENTRE ne
présente aucun risque (même logique que le refus toujours possible en CR1). Un tiers suspendu
peut rembourser.

SOURCE DU DÉBIT PARAMÉTRABLE (CR5d, migration 0039) : `compte_source_id`/`journal_code`
permettent à un appelant EXTERNE (credit/prelevement.py, le prélèvement automatique) de débiter
un compte d'épargne du tiers plutôt que la caisse — D EPARGNE / journal OD au lieu de D CAISSE /
journal CA. PAR DÉFAUT (les deux à None/CODE_JOURNAL), le comportement du guichet CR6d est
STRICTEMENT INCHANGÉ : aucune ligne de ce module n'a bougé pour ce cas, seul un paramètre
optionnel a été ajouté (voir test_remboursement_guichet_defaut_inchange). La ventilation
intérêts-d'abord, `compte_encours_courant` (CR5c) et le registre Repayment sont IDENTIQUES dans
les deux cas — c'est tout l'intérêt de réutiliser cette fonction plutôt que d'en écrire une
seconde (décision CR5d : « pas une nouvelle logique »).

SESSION DE CAISSE (Bloc C6) : le guichet volontaire (`compte_source_id is None`) exige une
session OUVERTE pour `par` — le compte ANCRÉ de CETTE session reçoit le débit, jamais celui de
l'agence. Le prélèvement automatique fournit TOUJOURS `compte_source_id`, donc n'atteint
structurellement JAMAIS cette branche, donc jamais ce gate — pas un cas spécial ajouté après
coup, une CONSÉQUENCE de où le contrôle est posé (même principe que `resoudre_session_active`,
voir caisse/service.py). Un versement PARTIEL (CR5b) passe par le MÊME appel que le versement
complet : rien ne distingue les deux ici, le gate s'applique pareil aux deux.

PIÈCE CONSTRUITE DIRECTEMENT (ecritures.creer_brouillon/valider), PAS via le moteur générique
poser_depuis_schema : ce dernier applique un même montant à toutes les lignes d'un modèle à
nombre de lignes fixe — un remboursement a des montants DIFFÉRENTS par ligne (capital ≠
intérêts) et un nombre de lignes VARIABLE (la ligne CREDIT ou la ligne PRODUITS_INTERETS peut
être OMISE si ce versement ne couvre que l'autre part, le moteur d'écriture refuse tout montant
<= 0). Décision : ne pas complexifier un moteur partagé par 3 modules pour un cas
structurellement différent (voir migration 0034).

VENTILATION INTÉRÊTS D'ABORD (défaut PROVISOIRE, convention bancaire courante — à valider,
voir docs/conformite-credit.md). Déduite de `montant_paye` (avant CE versement), AUCUNE colonne
dédiée : les intérêts déjà couverts sont `min(montant_paye_avant, echeance.interets)`, ce
versement couvre le reliquat d'intérêts en priorité, puis le capital.

PLAFOND = solde_du (`echeance.total - echeance.montant_paye`), PAS `echeance.total` : un
versement qui dépasse ce qui reste dû est refusé, même s'il reste inférieur au total d'origine.
GUICHET (CR6d) : envoie TOUJOURS solde_du en entier (paiement volontaire partiel EXCLU au
comptoir — décision produit, pas une limite technique). Le partiel sert le prélèvement
automatique (CR5d, à venir) qui encaisse ce qu'il trouve quand le solde du compte est
insuffisant.

TRANSACTION UNIQUE : la pièce, le passage de statut et le registre Repayment vivent dans la
même session, sans commit intermédiaire (ecritures.creer_brouillon/valider ne font que flush).
Si une étape lève après que la pièce a été posée, l'appelant rollback : rien ne persiste.
"""

import uuid
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.modules.audit.service import CONTEXTE_VIDE, ContexteRequete, ecrire_audit
from app.modules.caisse.service import resoudre_session_active
from app.modules.comptabilite import ecritures
from app.modules.comptabilite.ecritures import LigneSaisie
from app.modules.comptabilite.models import Journal, JournalEntry
from app.modules.credit.decaissement import RattachementManquantError
from app.modules.credit.demandes import RESSOURCE, CreditError
from app.modules.credit.echeancier import calculer_interets_courus
from app.modules.credit.models import Application, DelinquencyTier, Installment, Product, Repayment

CODE_JOURNAL = "CA"  # journal de caisse — même journal que le décaissement


class AucuneEcheanceAReglerError(CreditError):
    """Ce crédit n'est pas décaissé, ou toutes ses échéances sont déjà payées."""


class MontantIncorrectError(CreditError):
    """Le montant versé dépasse le solde restant dû de la prochaine échéance."""


@dataclass(frozen=True)
class ResultatRemboursement:
    """CE versement — pas l'échéance entière : `montant`/`montant_capital`/`montant_interets`
    décrivent uniquement ce que CE paiement a couvert (un versement partiel n'est qu'une partie
    du total dû). `paid_at` est l'instant de CE versement, distinct de `echeance.paid_at` qui
    ne se pose que lorsque l'échéance est intégralement soldée."""

    echeance: Installment
    montant: int
    montant_capital: int
    montant_interets: int
    paid_at: datetime
    solde_du: int
    echeance_soldee: bool
    # La pièce posée — CR5d en a besoin pour comptabiliser le débit côté epargne.accounts avec
    # LE MÊME journal_entry_id (voir credit/prelevement.py). Jamais exposé à l'API (le router
    # construit RemboursementRecu champ par champ, ce n'en fait pas partie).
    entry_id: uuid.UUID


def prochaine_echeance(db: Session, application_id: uuid.UUID) -> Installment | None:
    """La prochaine échéance NON SOLDÉE d'un crédit (à échoir OU partiellement payée), ou None
    si tout est réglé. `status != 'paye'`, PAS `status == 'a_echoir'` (CR5b) — une échéance
    partiellement payée n'est ni l'un ni l'autre au sens strict, elle doit rester trouvée.
    Publique : réutilisée par consultation.rechercher_remboursables (guichet CR6d) pour
    afficher le SOLDE exact à régler avant tout appel serveur — jamais un montant deviné."""
    return db.execute(
        select(Installment)
        .where(Installment.application_id == application_id, Installment.status != "paye")
        .order_by(Installment.numero)
        .limit(1)
    ).scalar_one_or_none()


def compte_encours_courant(db: Session, demande: Application) -> uuid.UUID:
    """Le compte qui porte ACTUELLEMENT l'encours de ce crédit (CR5c) : celui du palier de
    souffrance si le dossier est classé ET RÉELLEMENT PROVISIONNÉ (`taux_provision_bp > 0`),
    sinon l'ancrage `compte_credit_id` figé au décaissement (CR3). Sert à la fois à
    `rembourser()` (créditer le bon compte) et au job de reclassification (compte D'ORIGINE du
    transfert, lu AVANT mise à jour de `delinquency_tier_id`).

    Un palier à `taux_provision_bp = 0` (ex. simple retard, supervision — chantier souffrance
    lot 1) n'exige AUCUN compte : il étiquette un retard, il ne comptabilise rien. L'encours
    reste alors sur l'ancrage initial exactement comme pour un dossier sain — c'est pour ça
    qu'un dossier classé dans un tel palier ne doit JAMAIS exiger `compte_encours_id`.

    Refuse plutôt que deviner si le palier actuel EST provisionné mais n'a pas de compte
    d'encours rattaché — un paramétrage incomplet ne doit jamais faire créditer silencieusement
    le mauvais compte."""
    if demande.delinquency_tier_id is None:
        assert demande.compte_credit_id is not None
        return demande.compte_credit_id
    compte_encours: uuid.UUID | None
    taux_provision_bp: int
    compte_encours, taux_provision_bp = db.execute(
        select(DelinquencyTier.compte_encours_id, DelinquencyTier.taux_provision_bp).where(
            DelinquencyTier.id == demande.delinquency_tier_id
        )
    ).one()
    if taux_provision_bp == 0:
        assert demande.compte_credit_id is not None
        return demande.compte_credit_id
    if compte_encours is None:
        raise RattachementManquantError(
            "le palier de souffrance actuel de ce crédit n'a pas de compte d'encours rattaché "
            "(paramétrage)"
        )
    return compte_encours


def encours_actuel(db: Session, application_id: uuid.UUID) -> int:
    """Capital restant dû TOTAL de ce crédit à cet instant — 0 si intégralement soldé. Tient
    compte d'un versement partiel CR5b sur l'échéance en cours (la part capital déjà versée
    dessus est déduite, dérivée de montant_paye/interets, aucune colonne dédiée — même
    discipline que la ventilation de rembourser()).

    Déplacée ici depuis `reclassification.py` (chantier remboursement anticipé) : CR5c
    l'utilisait déjà, et `solder_par_anticipation()` (ci-dessous) en a besoin aussi — la placer
    dans CE module évite un import circulaire (`reclassification.py` importe déjà
    `compte_encours_courant`/`prochaine_echeance` d'ICI)."""
    echeance = prochaine_echeance(db, application_id)
    if echeance is None:
        return 0
    encours_avant_cette_echeance = echeance.capital_restant_du + echeance.capital
    part_capital_deja_versee = max(0, echeance.montant_paye - echeance.interets)
    return encours_avant_cette_echeance - part_capital_deja_versee


def rembourser(
    db: Session,
    demande: Application,
    *,
    montant: int,
    par: uuid.UUID | None,
    entry_date: date | None = None,
    contexte: ContexteRequete = CONTEXTE_VIDE,
    compte_source_id: uuid.UUID | None = None,
    journal_code: str = CODE_JOURNAL,
) -> ResultatRemboursement:
    """Encaisse un versement sur la PROCHAINE échéance non soldée d'un crédit décaissé — jusqu'à
    concurrence de son solde restant dû (PAS son total d'origine : une échéance déjà
    partiellement payée peut recevoir un versement complémentaire plus petit que son total).

    Refuse si le crédit n'est pas décaissé ou déjà entièrement soldé (aucune échéance non
    'paye'), si le montant dépasse le solde restant dû (message actionnable, nommant CE solde),
    si un rattachement comptable manque (compte d'encours ou, si ce versement couvre des
    intérêts, compte produits d'intérêts du produit), ou — guichet volontaire seulement,
    `compte_source_id is None` — si aucune session de caisse n'est ouverte pour `par` (Bloc C6).

    compte_source_id/journal_code : réservés à un appelant EXTERNE (CR5d, voir docstring module)
    — None/CODE_JOURNAL (défaut) préserve EXACTEMENT le comportement du guichet CR6d, ET
    n'atteint jamais le gate de session (voir docstring module)."""
    if demande.status != "decaisse":
        raise AucuneEcheanceAReglerError(
            f"Cette demande ({demande.application_number}) n'est pas décaissée : "
            "aucune échéance à régler."
        )

    echeance = prochaine_echeance(db, demande.id)
    if echeance is None:
        raise AucuneEcheanceAReglerError(
            f"Ce crédit ({demande.application_number}) est déjà entièrement soldé : "
            "aucune échéance à régler."
        )

    montant_paye_avant = echeance.montant_paye
    solde_du = echeance.total - montant_paye_avant
    if montant > solde_du:
        raise MontantIncorrectError(
            f"Le solde restant de cette échéance est de {solde_du} F, vous avez saisi {montant} F."
        )

    # Ventilation intérêts d'abord (PROVISOIRE) : déduite de montant_paye_avant, aucune colonne
    # dédiée — voir docstring module.
    interets_deja_couverts = min(montant_paye_avant, echeance.interets)
    interets_restants = echeance.interets - interets_deja_couverts
    part_interets = min(montant, interets_restants)
    part_capital = montant - part_interets

    # D CAISSE (guichet, défaut) ou D le compte fourni par l'appelant (CR5d : le collectif
    # épargne du tiers) — voir docstring module. Bloc C6 : le guichet volontaire (cette
    # branche SEULE) exige une session de caisse OUVERTE pour le caissier (`par`), et c'est le
    # compte ANCRÉ de CETTE session qui reçoit le débit, jamais celui de l'agence. Le
    # prélèvement automatique (CR5d) fournit TOUJOURS `compte_source_id` explicitement et
    # n'entre structurellement JAMAIS dans cette branche — donc jamais ce gate — même pour un
    # paiement partiel (aucune distinction ici entre versement complet et partiel : c'est le
    # MÊME appel, la même branche, quel que soit `montant`).
    compte_debit = compte_source_id
    prefixe_libelle = "Remboursement crédit"
    if compte_debit is None:
        compte_debit = resoudre_session_active(db, par).compte_caisse_id
    else:
        prefixe_libelle = "Prélèvement automatique crédit"

    journal_id = db.execute(select(Journal.id).where(Journal.code == journal_code)).scalar_one()

    reference = f"{prefixe_libelle} {demande.application_number} #{echeance.numero}"
    lignes = [
        LigneSaisie(account_id=compte_debit, side="D", amount=montant, label=reference),
    ]
    if part_capital > 0:
        lignes.append(
            LigneSaisie(
                account_id=compte_encours_courant(db, demande),
                side="C",
                amount=part_capital,
                label=f"{reference} (capital)",
            )
        )
    if part_interets > 0:
        produit = db.get(Product, demande.product_id)
        if produit is None or produit.compte_produits_interets_id is None:
            raise RattachementManquantError(
                "ce produit de crédit n'a pas de compte de produits d'intérêts rattaché "
                "(plan comptable)"
            )
        lignes.append(
            LigneSaisie(
                account_id=produit.compte_produits_interets_id,
                side="C",
                amount=part_interets,
                label=f"{reference} (intérêts)",
            )
        )

    jour = entry_date
    if jour is None:
        jour = db.execute(text("SELECT CURRENT_DATE")).scalar_one()

    entry: JournalEntry = ecritures.creer_brouillon(
        db,
        journal_id=journal_id,
        entry_date=jour,
        description=reference,
        lignes=lignes,
        par=par,
    )
    ecritures.valider(db, entry, par, contexte=contexte)

    maintenant = db.execute(text("SELECT NOW()")).scalar_one()
    nouveau_montant_paye = montant_paye_avant + montant
    echeance_soldee = nouveau_montant_paye == echeance.total

    avant_statut = echeance.status
    echeance.montant_paye = nouveau_montant_paye
    if echeance_soldee:
        echeance.status = "paye"
        echeance.paid_at = maintenant
        echeance.paid_by = par
    else:
        echeance.status = "partiellement_paye"
    db.flush()

    db.add(
        Repayment(
            installment_id=echeance.id,
            application_id=demande.id,
            montant_capital=part_capital,
            montant_interets=part_interets,
            montant_total=montant,
            entry_id=entry.id,
            paid_by=par,
        )
    )
    db.flush()

    ecrire_audit(
        db,
        action="credit.echeance.payee",
        contexte=contexte,
        acteur_id=par,
        resource_type=RESSOURCE,
        resource_id=demande.id,
        agency_id=demande.agency_id,
        old_values={"status": avant_statut, "montant_paye": montant_paye_avant},
        new_values={
            "numero": echeance.numero,
            "montant_capital": part_capital,
            "montant_interets": part_interets,
            "montant_total": montant,
            "montant_paye": nouveau_montant_paye,
            "status": echeance.status,
            "entry_number": entry.entry_number,
        },
    )
    return ResultatRemboursement(
        echeance=echeance,
        montant=montant,
        montant_capital=part_capital,
        montant_interets=part_interets,
        paid_at=maintenant,
        solde_du=echeance.total - nouveau_montant_paye,
        echeance_soldee=echeance_soldee,
        entry_id=entry.id,
    )


# --- Solde anticipé (clôture totale avant terme, migration 0051) --------------------------
#
# TOTAL SEULEMENT (arbitrage acté) : pas de partiel anticipé, qui exigerait de réécrire
# l'échéancier restant. Le client paie le CAPITAL RESTANT DÛ + les INTÉRÊTS COURUS jusqu'à la
# date du solde ; les intérêts des échéances FUTURES sont ANNULÉS, AUCUNE pénalité — décision
# du banquier, favorable au client (protection consommateur UEMOA).
#
# LES `Installment` FUTURES NE SONT JAMAIS TOUCHÉES (arbitrage acté, option c du cadrage) : le
# plan reste un témoin historique intact, jamais réécrit — ça évite tout conflit avec son CHECK
# `statut_coherent_avec_montant_paye` (migration 0037), qui exigerait `montant_paye = total`
# pour marquer une échéance 'paye', alors qu'on paie MOINS que la somme des `total` futurs
# (intérêts annulés). La SEULE trace du solde anticipé est `Application.status = 'solde'` +
# `solde_at`/`solde_by` (migration 0051) + l'écriture comptable + l'audit — jamais une
# Installment ni un Repayment (son FK `installment_id` est NOT NULL, ce règlement ne concerne
# structurellement aucune échéance unique).
#
# CONSÉQUENCE GRATUITE : `rembourser()` (ci-dessus, refuse si `status != 'decaisse'`) et
# `executer_reclassification()` (`reclassification.py`, ne sélectionne que `status == 'decaisse'`)
# excluent DÉJÀ tout statut différent de 'decaisse' — passer à 'solde' les coupe TOUS LES DEUX
# sans toucher une ligne de ces deux fichiers.
#
# ÉCHÉANCE COURANTE DÉJÀ PARTIELLEMENT PAYÉE (CR5b) : REFUSÉE en v1 (arbitrage acté) — éviter
# d'avoir à nettre l'intérêt déjà encaissé sur ce versement partiel contre l'intérêt couru
# recalculé, un cas plus subtil laissé à une itération future si le besoin se confirme.


class EcheanceEnCoursDejaVerseeError(CreditError):
    """L'échéance en cours porte déjà un versement partiel (CR5b) : solde anticipé refusé en
    v1 — rembourser cette échéance jusqu'à solde d'abord, ou attendre la suivante."""


@dataclass(frozen=True)
class ResultatSoldeAnticipe:
    demande: Application
    capital_regle: int
    interets_courus: int
    montant_total: int
    jours_courus: int
    solde_at: datetime
    entry_id: uuid.UUID


@dataclass(frozen=True)
class DetailSoldeAnticipe:
    """Ce qu'un solde anticipé COÛTERAIT à `jour` — capital restant + intérêts courus, AUCUNE
    écriture. Renvoyée par `apercevoir_solde_anticipe` (lecture) ET calculée en interne par
    `solder_par_anticipation` (action) via `_detail_solde_anticipe` : UNE SEULE fonction de
    calcul, jamais deux implémentations qui pourraient diverger — même discipline que
    `_dater_echeances`, partagée par `decaisser()`/`generer_apercu()` dans decaissement.py."""

    capital_restant: int
    interets_courus: int
    montant_total: int
    date_reference: date
    jours_courus: int


def _date_reference_interets_courus(db: Session, demande: Application) -> date:
    """Le point de départ du prorata (voir `echeancier.calculer_interets_courus`) : la
    due_date de la dernière `Installment` au statut 'paye', ou la date de décaissement si
    aucune échéance n'a encore été intégralement payée."""
    derniere_payee = db.execute(
        select(Installment.due_date)
        .where(Installment.application_id == demande.id, Installment.status == "paye")
        .order_by(Installment.numero.desc())
        .limit(1)
    ).scalar_one_or_none()
    if derniere_payee is not None:
        return derniere_payee
    assert demande.disbursed_at is not None
    return demande.disbursed_at.date()


def _detail_solde_anticipe(db: Session, demande: Application, *, jour: date) -> DetailSoldeAnticipe:
    """Calcule PUREMENT (aucune écriture, aucune mutation) ce que coûterait un solde anticipé
    à `jour` — PARTAGÉE par `apercevoir_solde_anticipe` et `solder_par_anticipation` (voir
    `DetailSoldeAnticipe`).

    Refuse si le crédit n'est pas décaissé ou déjà soldé (`AucuneEcheanceAReglerError`, même
    erreur que `rembourser()` — même situation de fond : rien à régler dans cet état), si
    l'échéance en cours porte déjà un versement partiel (`EcheanceEnCoursDejaVerseeError`, v1),
    ou si le produit n'a pas de compte de produits d'intérêts rattaché ALORS QUE des intérêts
    courus sont dus (`RattachementManquantError` — jamais exigé si `interets_courus == 0`,
    même discipline que `rembourser()`) — revérifié ICI, pas seulement à l'action, pour qu'un
    aperçu ne promette jamais un solde qui échouerait ensuite pour cette seule raison."""
    if demande.status != "decaisse":
        raise AucuneEcheanceAReglerError(
            f"Cette demande ({demande.application_number}) n'est pas décaissée : "
            "aucune échéance à régler."
        )

    echeance_courante = prochaine_echeance(db, demande.id)
    if echeance_courante is None:
        raise AucuneEcheanceAReglerError(
            f"Ce crédit ({demande.application_number}) est déjà entièrement soldé : "
            "aucune échéance à régler."
        )
    if echeance_courante.montant_paye > 0:
        raise EcheanceEnCoursDejaVerseeError(
            f"L'échéance #{echeance_courante.numero} de ce crédit porte déjà un versement "
            "partiel : le solde anticipé n'est pas possible en l'état."
        )

    capital_restant = encours_actuel(db, demande.id)

    produit = db.get(Product, demande.product_id)
    assert produit is not None  # FK NOT NULL depuis demandes.creer_demande

    date_reference = _date_reference_interets_courus(db, demande)
    jours = (jour - date_reference).days
    interets_courus = calculer_interets_courus(
        capital_restant=capital_restant,
        taux_bp=produit.taux_bp,
        jours=jours,
        base_jours=produit.base_jours,
        regle_arrondi=produit.regle_arrondi,
    )
    if interets_courus > 0 and produit.compte_produits_interets_id is None:
        raise RattachementManquantError(
            "ce produit de crédit n'a pas de compte de produits d'intérêts rattaché "
            "(plan comptable)"
        )

    return DetailSoldeAnticipe(
        capital_restant=capital_restant,
        interets_courus=interets_courus,
        montant_total=capital_restant + interets_courus,
        date_reference=date_reference,
        jours_courus=jours,
    )


def apercevoir_solde_anticipe(
    db: Session, demande: Application, *, jour: date | None = None
) -> DetailSoldeAnticipe:
    """Aperçu PUR (CR6b-like) de ce que coûterait un solde anticipé AUJOURD'HUI (ou `jour`) —
    RIEN N'EST ÉCRIT EN BASE, aucun db.add, aucun db.commit. Les montants sont GARANTIS
    identiques à ceux réellement posés par `solder_par_anticipation` LE MÊME JOUR (même
    fonction de calcul, voir `_detail_solde_anticipe`) — un jour différent recalcule sur SA
    propre date, l'aperçu n'est qu'indicatif au-delà d'aujourd'hui."""
    if jour is None:
        jour = db.execute(text("SELECT CURRENT_DATE")).scalar_one()
    return _detail_solde_anticipe(db, demande, jour=jour)


def solder_par_anticipation(
    db: Session,
    demande: Application,
    *,
    par: uuid.UUID | None,
    entry_date: date | None = None,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> ResultatSoldeAnticipe:
    """Clôture TOTALE et anticipée d'un crédit décaissé : encaisse le capital restant dû +
    les intérêts courus jusqu'à aujourd'hui (ou `entry_date`), annule les intérêts des
    échéances futures (elles ne sont jamais réécrites, voir docstring de section ci-dessus),
    passe la demande à 'solde'. AUCUNE pénalité.

    Ne fait JAMAIS confiance à un montant transmis par l'appelant : `_detail_solde_anticipe`
    est TOUJOURS recalculé ici, à SA date (`entry_date` ou aujourd'hui) — voir
    `apercevoir_solde_anticipe` pour l'aperçu en lecture seule, mêmes refus, mêmes montants
    SI la date est identique.

    Exige une session de caisse OUVERTE pour `par` (guichet, même gate que `rembourser()` en
    mode volontaire) — aucun mode `compte_source_id` externe ici, ce n'est pas un prélèvement
    automatique."""
    jour = entry_date
    if jour is None:
        jour = db.execute(text("SELECT CURRENT_DATE")).scalar_one()

    detail = _detail_solde_anticipe(db, demande, jour=jour)
    capital_restant = detail.capital_restant
    interets_courus = detail.interets_courus
    montant_total = detail.montant_total
    jours = detail.jours_courus

    produit = db.get(Product, demande.product_id)
    assert produit is not None  # déjà chargé et vérifié par _detail_solde_anticipe

    compte_debit = resoudre_session_active(db, par).compte_caisse_id
    reference = f"Solde anticipé crédit {demande.application_number}"
    lignes = [
        LigneSaisie(account_id=compte_debit, side="D", amount=montant_total, label=reference),
        LigneSaisie(
            account_id=compte_encours_courant(db, demande),
            side="C",
            amount=capital_restant,
            label=f"{reference} (capital)",
        ),
    ]
    if interets_courus > 0:
        # Rattachement déjà vérifié par _detail_solde_anticipe (RattachementManquantError sinon).
        assert produit.compte_produits_interets_id is not None
        lignes.append(
            LigneSaisie(
                account_id=produit.compte_produits_interets_id,
                side="C",
                amount=interets_courus,
                label=f"{reference} (intérêts courus)",
            )
        )

    journal_id = db.execute(select(Journal.id).where(Journal.code == CODE_JOURNAL)).scalar_one()
    entry: JournalEntry = ecritures.creer_brouillon(
        db,
        journal_id=journal_id,
        entry_date=jour,
        description=reference,
        lignes=lignes,
        par=par,
    )
    ecritures.valider(db, entry, par, contexte=contexte)

    maintenant = db.execute(text("SELECT NOW()")).scalar_one()
    avant = {"status": demande.status}
    demande.status = "solde"
    demande.solde_at = maintenant
    demande.solde_by = par
    demande.updated_by = par
    db.flush()

    ecrire_audit(
        db,
        action="credit.demande.soldee_anticipee",
        contexte=contexte,
        acteur_id=par,
        resource_type=RESSOURCE,
        resource_id=demande.id,
        agency_id=demande.agency_id,
        old_values=avant,
        new_values={
            "status": "solde",
            "capital_regle": capital_restant,
            "interets_courus": interets_courus,
            "montant_total": montant_total,
            "jours_courus": jours,
            "entry_number": entry.entry_number,
        },
    )
    return ResultatSoldeAnticipe(
        demande=demande,
        capital_regle=capital_restant,
        interets_courus=interets_courus,
        montant_total=montant_total,
        jours_courus=jours,
        solde_at=maintenant,
        entry_id=entry.id,
    )
