"""Liste FERMÉE de mots neutres pour les mots de passe provisoires lisibles.

⚠️ CETTE LISTE EST LA FRONTIÈRE DE SÉCURITÉ. À LIRE AVANT DE LA MODIFIER.

Un mot de passe provisoire doit se dicter au téléphone et se recopier sur papier sans
erreur. Un générateur de syllabes ALÉATOIRES produirait tôt ou tard, par hasard, un mot réel
malheureux — insulte ou grossièreté en français, haoussa, zarma, bambara ou une autre langue
de la région. Un administrateur qui doit dicter cela à une caissière, c'est gênant, et cela
décrédibilise le logiciel. Ce n'est pas théorique : c'est un défaut connu des générateurs
prononçables, et il se découvre CHEZ LE CLIENT, pas en test.

La parade : ne RIEN générer librement. Les mots de passe sont assemblés à partir de CETTE
liste seule. Une grossièreté ne peut donc apparaître que si elle EST dans la liste — ce que
la relecture humaine de la liste, faite une fois, garantit qu'elle n'est pas. Les mots sont
séparés par des tirets : rien ne se forme entre deux mots, et la dictée se fait mot par mot.

RÈGLES POUR TOUTE MODIFICATION :
  - n'ajouter que des mots NEUTRES et COURANTS (nature, objets, animaux, couleurs) ;
  - sans accent ni cédille (« é » et « e » se confondent à la dictée, et on reste en ASCII) ;
  - 3 à 6 lettres, faciles à dire ;
  - au moindre doute sur un sens second, dans quelque langue que ce soit, NE PAS l'ajouter ;
  - agrandir la liste AUGMENTE le risque qu'un mot passe au travers : pour plus d'entropie,
    préférer un mot de plus dans le mot de passe (mots_de_passe.py), pas une liste plus
    grosse. La taille de la liste est un compromis sécurité, pas une variable à maximiser.

Un test vérifie que chaque entrée est en minuscules ASCII, sans doublon, dans la longueur
attendue — un garde-fou de forme, pas de sens (le sens, lui, relève de la relecture).
"""

# Mots neutres, vérifiés à la main. Rangés par thème pour faciliter la relecture.
MOTS_LISIBLES: tuple[str, ...] = (
    # Nature et paysage
    "sable",
    "pont",
    "rive",
    "champ",
    "roche",
    "mont",
    "bois",
    "lac",
    "mer",
    "val",
    "dune",
    "cap",
    "plage",
    "terre",
    "ciel",
    "nuage",
    "pluie",
    "vent",
    "colline",
    "foret",
    "plaine",
    "vallon",
    "source",
    "chemin",
    "sentier",
    "prairie",
    "rocher",
    "falaise",
    # Ciel et temps
    "soleil",
    "lune",
    "aube",
    "midi",
    "brise",
    "givre",
    "neige",
    "orage",
    "eclair",
    "arc",
    # Plantes
    "arbre",
    "fleur",
    "herbe",
    "racine",
    "graine",
    "feuille",
    "branche",
    "tronc",
    "palme",
    "cactus",
    "roseau",
    "menthe",
    "trefle",
    "lierre",
    "sapin",
    "cedre",
    "chene",
    "olivier",
    # Fruits et aliments neutres
    "pomme",
    "poire",
    "prune",
    "raisin",
    "olive",
    "datte",
    "mangue",
    "citron",
    "melon",
    "mais",
    "riz",
    "pain",
    "sel",
    "sucre",
    "miel",
    "lait",
    "farine",
    "beurre",
    "tomate",
    "carotte",
    "oignon",
    "haricot",
    "gombo",
    "arachide",
    # Animaux
    "chat",
    "chien",
    "cheval",
    "mouton",
    "chevre",
    "lapin",
    "poule",
    "canard",
    "pigeon",
    "zebu",
    "chameau",
    "girafe",
    "lion",
    "tigre",
    "singe",
    "souris",
    "tortue",
    "poisson",
    "dauphin",
    "crabe",
    "abeille",
    "fourmi",
    "hibou",
    "aigle",
    "heron",
    "cygne",
    "faucon",
    "gazelle",
    "antilope",
    "buffle",
    "hyene",
    "renard",
    "loutre",
    # Objets courants
    "table",
    "chaise",
    "porte",
    "toit",
    "banc",
    "lampe",
    "livre",
    "page",
    "stylo",
    "regle",
    "sac",
    "boite",
    "panier",
    "seau",
    "corde",
    "planche",
    "roue",
    "velo",
    "barque",
    "voile",
    "rame",
    "filet",
    "cloche",
    "tambour",
    "flute",
    "panneau",
    "lanterne",
    "bougie",
    "cadre",
    # Couleurs
    "rouge",
    "vert",
    "bleu",
    "jaune",
    "blanc",
    "noir",
    "brun",
    "gris",
    "rose",
    "ocre",
)
