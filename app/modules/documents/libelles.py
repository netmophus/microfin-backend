"""Libellés français des documents générés par le serveur — SOURCE UNIQUE (le frontend n'en
garde aucun pour ces documents). Mêmes mentions provisoires que les anciens documents navigateur :
placeholders à valider avant déploiement."""

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
