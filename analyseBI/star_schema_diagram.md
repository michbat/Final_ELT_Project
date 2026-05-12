<div style="background: #fff; color: #000; padding: 20px; border-radius: 2px; box-shadow: 0 2px 12px 0 rgba(0,0,0,0.07);">

# Schéma en Étoile - Data Warehouse Gold Layer

## Diagramme Entité-Relations



```mermaid
%%{init: {'theme':'base', 'themeVariables': { 'primaryColor':'#e0e0e0', 'edgeLabelBackground':'#fff', 'secondaryColor':'#f5f5f5', 'tertiaryColor':'#bdbdbd', 'primaryTextColor':'#333', 'lineColor':'#888', 'primaryBorderColor':'#888'}}}%%
erDiagram
    date_dim ||--o{ fact_energy_production : "1:N"
    date_dim ||--o{ fact_weather_conditions : "1:N"
    time_dim ||--o{ fact_energy_production : "1:N"
    time_dim ||--o{ fact_weather_conditions : "1:N"
    turbine_dim ||--o{ fact_energy_production : "1:N"
    operational_status_dim ||--o{ fact_energy_production : "1:N"
    location_dim ||--o{ fact_weather_conditions : "1:N"

    date_dim {
        DATE date_id PK
        INTEGER day
        INTEGER month
        INTEGER quarter
        INTEGER year
    }

    time_dim {
        TIME time_id PK
        INTEGER hour_of_day
        INTEGER minute_of_hour
        INTEGER second_of_minute
        VARCHAR time_period
    }

    turbine_dim {
        BIGINT turbine_id PK
        VARCHAR turbine_name
        INTEGER capacity
        NUMERIC latitude
        NUMERIC longitude
        VARCHAR region
        VARCHAR region_name
    }

    operational_status_dim {
        BIGINT status_id PK
        VARCHAR status
        VARCHAR responsible_department
    }

    location_dim {
        BIGINT location_id PK
        NUMERIC latitude
        NUMERIC longitude
        VARCHAR region
        VARCHAR region_name
    }

    fact_energy_production {
        BIGINT fact_production_id PK
        DATE date_id FK
        TIME time_id FK
        BIGINT turbine_id FK
        BIGINT status_id FK
        NUMERIC energy_produced
        NUMERIC wind_speed
        VARCHAR wind_direction
        NUMERIC cloud_cover
    }

    fact_weather_conditions {
        BIGINT fact_weather_id PK
        DATE date_id FK
        TIME time_id FK
        BIGINT location_id FK
        NUMERIC wind_speed
        NUMERIC wind_gust_10m
        NUMERIC temperature_2m
        NUMERIC cloud_cover
    }


```


## Description des Relations

### Tables de Dimension (5)

1. **date_dim** - Dimension temporelle (dates)
   - Clé primaire : `date_id` (date naturelle)
   - Contient les attributs calendaires

2. **time_dim** - Dimension temporelle (heures)
   - Clé primaire : `time_id` (heure naturelle)
   - Contient les attributs horaires

3. **turbine_dim** - Dimension des turbines
   - Clé primaire : `turbine_id` (clé stable générée par hash)
   - Contient les caractéristiques des turbines

4. **operational_status_dim** - Dimension des statuts opérationnels
   - Clé primaire : `status_id` (clé stable générée par hash)
   - Contient les statuts et départements responsables

5. **location_dim** - Dimension géographique
   - Clé primaire : `location_id` (clé stable générée par hash)
   - Contient les coordonnées et régions

### Tables de Faits (2)

1. **fact_energy_production** - Fait de production d'énergie
   - Mesure la production d'énergie par turbine
   - Dimensions : date, time, turbine, operational_status
   - Métriques : energy_produced, wind_speed_100m, wind_direction

2. **fact_weather_conditions** - Fait des conditions météorologiques
   - Mesure les conditions météo par localisation
   - Dimensions : date, time, location
   - Métriques : temperature_2m, pressure_msl, precipitation, wind_gust_10m, wind_speed_100m

## Cardinalités

- **1:N** (Un-à-Plusieurs) : Une ligne de dimension peut être référencée par plusieurs lignes de faits
- Chaque enregistrement de fait référence exactement une valeur par dimension (via FK)
- Les dimensions sont partagées entre les tables de faits (conformed dimensions)

## Clés

- **Clés naturelles** : date_dim (date), time_dim (heure)
- **Clés stables par hash** : turbine_dim, operational_status_dim, location_dim
  - Générées via `F.abs(F.hash(F.concat_ws()))` pour garantir la stabilité
 
</div>