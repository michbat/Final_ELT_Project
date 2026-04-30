**Proposition de projet final – Pipeline ELT cloudless & open source**  
*Data Engineering – Travail de fin de formation*


## 1. Contexte et objectif

Le projet consiste à concevoir pipeline ELT complet à partir de zéro, sans recours à des services cloud. L’objectif est de démontrer la maîtrise des briques open source essentielles du data engineering : orchestration, transformation, persistance et visualisation.

L’ensemble des outils sera conteneurisé avec Docker et orchestré localement.


## 2. Sources de données

| Source | Type | Usage |
|--------|------|-------|
| Jeux de données CSV (production d’éoliennes) | Fichiers statiques | Données métier principales |
| API Open-Meteo | API REST | Enrichissement météorologique (vent, température, pression) |

Les jeux de données CSV proviennent du dépôt GitHub de Mikail Altundas, formateur en Data.

---

## 3. Architecture technique envisagée

Composants principaux :

- **Orchestration** : Apache Airflow (dockerisé)  
- **Base de données** : PostgreSQL (persistance des données de 3 couches médailon)  
- **Traitement** : scripts Python exécutés par Airflow  
- **Visualisation** : Apache Superset

L’ensemble sera monté from scratch via Docker Compose.


## 4. Flux de traitement

1. Récupération orchestrée des fichiers CSV  
2. Appel à l’API Open-Meteo pour enrichir avec les données climatiques  
3. Chargement dans PostgreSQL (3 couches médaillon: bronze, silver, gold)  
4. Transformations Python pour construire une vue analytique  
5. Connexion de Superset à PostgreSQL pour le reporting


## 5. Livrables attendus

- Code source complet (Dockerfiles, DAGs Airflow, scripts de transformation,...)  
- Documentation technique décrivant l’architecture, le lancement et les choix d’implémentation  
- Jeu de dashboards sous Superset (production journalière, impact météo, disponibilité des éoliennes)  
- Guide de reproductibilité (lancement en local avec Docker)



## 6. Intérêt pédagogique

- Approche cloudless oblige à tout maîtriser en local, donc formateur
- Utilisation exclusive de technologies open source et libres  
- Pipeline complet allant de l’ingestion CSV/API au reporting  
- Reproductibilité garantie via la conteneurisation

