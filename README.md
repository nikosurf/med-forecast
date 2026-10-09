# Houle Med

Prévisions de houle surfable pour quelques spots méditerranéens, en croisant plusieurs modèles.

## Fonctionnement

Trois fois par jour, GitHub Actions lance `forecast/fetch.py`, qui :

1. récupère, pour chaque spot de `config/spots.json`, les prévisions de vagues de tous les modèles disponibles sur Open-Meteo (Météo-France MFWAM, ECMWF WAM, GFS Wave/WW3, DWD EWAM et GWAM) et le vent (Arome, Arpège, ECMWF IFS, GFS, ICON) ;
2. estime la taille au spot à partir de la hauteur au large, de la période et de l'orientation de la houle par rapport à la fenêtre du spot ;
3. mesure l'accord entre modèles (indice de confiance) et la tendance par rapport au run précédent ;
4. écrit `docs/data/latest.json`, lu par l'app, et archive toutes les prévisions brutes dans `data/archive/`, pour la calibration future (bouées, sessions).

L'app est une page web installable (PWA) servie par GitHub Pages depuis `docs/`.

## Mise en route

1. **Settings > Pages** : Source « Deploy from a branch », branche `main`, dossier `/docs`.
2. **Actions > Prévisions > Run workflow** pour lancer une première collecte sans attendre l'horaire.
3. Sur l'iPhone, ouvrir `https://nikosurf.github.io/med-forecast/` dans Safari, puis Partager > Sur l'écran d'accueil.

## Régler un spot

Dans `config/spots.json` :

- `swell_window` : directions de houle (d'où elle vient, en degrés) qui rentrent sur le spot ;
- `offshore_wind` : directions de vent offshore ;
- `min_period` : période en dessous de laquelle le spot ne marche presque pas.

Les valeurs actuelles sont **provisoires**, à corriger au fil des sessions.

## Formule de taille (provisoire)

`taille ≈ Hs au large × facteur période × facteur direction`. Le facteur direction tombe à zéro à 35° hors de la fenêtre. Une note de 0 à 4 est attribuée selon la taille, avec une pénalité si le vent est onshore. Ces coefficients seront recalés grâce aux archives et aux mesures de bouées.

## À venir

- Mesures de bouées via The Buoy API (La Bouée), une fois les identifiants obtenus, stockés dans les secrets GitHub (`LABOUEE_API_KEY`).
- Calcul du biais de chaque modèle par spot, à partir des archives.
- Carnet de sessions.
