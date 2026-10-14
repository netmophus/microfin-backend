"""Libellés français des documents générés par le serveur — SOURCE UNIQUE (le frontend n'en
garde aucun pour ces documents). Mentions provisoires : placeholders à valider avant déploiement."""

LIBELLES = {
    "decouper_ici": "✂ découper ici",
    "bandeau_provisoire": "Document provisoire — en-tête et mentions à valider avant déploiement.",
    "institution_absente": "Nom de l'institution (à renseigner)",
    "telephone": "Tél. :",
    "agrement": "Agrément n°",
    "agence": "Agence :",
    "journee_comptable": "Journée comptable :",
    "duplicata": "DUPLICATA",
    "signature_client_obligatoire": "Signature du client (obligatoire)",
    "signature_client_facultative": "Signature du client (facultative)",
    "signature_caissier": "Signature du caissier",
    "libelle": "Libellé",
    "valeur": "Valeur",
    "arrete_a": "Arrêté à la somme de :",
    "mention": (
        "Conservez ce document : il atteste de l'opération. Mention légale à valider avant "
        "déploiement."
    ),
    # --- Reçus de caisse / épargne / dépôt à terme (lot 2) -------------------------------------
    "caissier": "Caissier",
    "membre": "Membre",
    "titulaire": "Titulaire",
    "compte": "Compte",
    "solde_apres": "Nouveau solde",
    "numero_recu": "Reçu n° {numero}",
    "numero_bon": "Bon n° {numero}",
    "titre_versement": "Reçu de versement",
    "titre_retrait": "Reçu de retrait",
    "titre_placement": "Reçu de placement — dépôt à terme",
    "titre_denouement": "Reçu de dénouement — dépôt à terme",
    "titre_retrait_anticipe": "Reçu de retrait anticipé — dépôt à terme",
    "titre_bon": "Bon de dépôt à terme",
    "montant_verse": "Montant versé",
    "montant_retire": "Montant retiré",
    "capital_encaisse": "Capital encaissé",
    "total_remis": "Total remis au client",
    "capital": "Capital",
    "capital_place": "Capital placé",
    "duree": "Durée",
    "duree_valeur": "{mois} mois",
    "depart": "Départ",
    "echeance": "Échéance",
    "taux_annuel": "Taux annuel",
    "interets": "Intérêts",
    "interets_attendus": "Intérêts attendus à l'échéance",
    "interets_courus": "Intérêts courus",
    "interets_nets": "Intérêts versés",
    "penalite": "Pénalité de retrait anticipé",
    "penalite_valeur": "{pourcent} des intérêts courus",
    "clause_echeance": (
        "À l'échéance, le capital et les intérêts sont remis au titulaire. Les intérêts cessent de "
        "courir à l'échéance."
    ),
    "clause_retrait_anticipe": (
        "En cas de retrait avant l'échéance, {pourcent} des intérêts courus est retenu à titre de "
        "pénalité ; le capital est toujours restitué en totalité. Clause à valider avant "
        "déploiement."
    ),
    "mention_bon": (
        "Ce bon n'est ni négociable ni cessible. Mentions légales à valider avant déploiement."
    ),
}

EXEMPLAIRES = ("Exemplaire client", "Exemplaire souche")

# --- Reçus de parts sociales (lot 3) -------------------------------------------------------------
LIBELLES.update(
    {
        "titre_parts_souscription": "Reçu de souscription de parts sociales",
        "titre_parts_liberation": "Reçu de libération de parts sociales",
        "titre_parts_remboursement": "Reçu de remboursement de parts sociales",
        "parts_nombre": "Nombre de parts",
        "parts_valeur_unitaire": "Valeur d'une part",
        "parts_liberees_apres": "Parts libérées après l'opération",
        "parts_non_liberees_apres": "Parts souscrites non libérées après l'opération",
        "montant_souscrit": "Montant encaissé",
        "montant_libere": "Montant libéré",
        "montant_rembourse": "Total remis au client",
    }
)

# --- Lettre de demande d'explication d'un écart de caisse (lot 3) --------------------------------
LIBELLES.update(
    {
        "titre_lettre": "Lettre de demande d'explication",
        "numero_lettre": "Session {numero}",
        "lettre_objet": "Objet : demande d'explication — écart de caisse",
        "lettre_caissier": "Caissier concerné",
        "lettre_agence": "Agence",
        "lettre_compte": "Compte de caisse",
        "lettre_calcul": "Calcul de l'écart",
        "lettre_fonds_initial": "Fonds initial",
        "lettre_solde_theorique": "Solde théorique de clôture",
        "lettre_montant_compte": "Montant compté",
        "lettre_ecart": "Écart (manquant)",
        # Le délai (48 heures) est un PLACEHOLDER ajustable, pas une règle validée.
        "lettre_texte_formel": (
            "Un manquant de {montant} a été constaté à la fermeture de votre session de caisse "
            "du {date}. Nous vous demandons de bien vouloir fournir une explication écrite dans "
            "un délai de 48 heures à compter de la remise de la présente lettre."
        ),
        "lettre_reponse": "Réponse du caissier",
        "lettre_signature_responsable": "Signature et cachet du responsable",
        "lettre_date_signature": "Date",
    }
)

# --- États comptables de la journée (lot 4) ------------------------------------------------------
LIBELLES.update(
    {
        "titre_journal": "Journal du jour",
        "titre_balance": "Balance générale arrêtée à la journée",
        "brouillon_etat": (
            "BROUILLON — journée encore ouverte : ce document n'est pas figé et n'est pas archivé."
        ),
        "journal_ecritures": "Écritures de la journée : {nombre}",
        "journal_journal": "Journal {code} — {nom}",
        "journal_piece": "Pièce",
        "journal_compte": "Compte",
        "journal_libelle_compte": "Intitulé du compte",
        "journal_debit": "Débit (F)",
        "journal_credit": "Crédit (F)",
        "journal_total_journal": "Total du journal {code}",
        "journal_total_jour": "Total de la journée",
        "balance_mouvements": "Mouvements du jour",
        "balance_soldes": "Soldes cumulés",
        "balance_debiteur": "Débiteur (F)",
        "balance_crediteur": "Créditeur (F)",
        "balance_total": "Total",
        "balance_vide": "Aucun compte n'a de solde ni de mouvement à cette journée.",
        "journal_vide": "Aucune écriture validée n'a été passée pendant cette journée.",
    }
)
