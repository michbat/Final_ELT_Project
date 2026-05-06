#!/usr/bin/env python3
"""
ingestion_api_data.py - Ingestion données météo Open-Meteo (couche Bronze)

OBJECTIF :
    Récupérer des données météo historiques pour enrichir les analyses de production éolienne.

WORKFLOW :
    1. Appel API Open-Meteo pour 3 régions (données horaires)
    2. Interpolation linéaire → création de points toutes les 10 minutes
    3. Écriture dans PostgreSQL (table bronze.weatherforecastapi_raw)

POURQUOI L'INTERPOLATION ?
    - Open-Meteo fournit des données horaires (24 points/jour)
    - Les données éoliennes sont à 10 minutes (144 points/jour)
    - L'interpolation permet d'aligner les granularités temporelles

STACK : PySpark + PostgreSQL JDBC + requests
"""

import os
import sys
import logging
import requests
import click
import psycopg
from dotenv import load_dotenv
from pyspark.sql import SparkSession
from pyspark.sql.types import (
    StructType, StructField, StringType, FloatType, 
    DoubleType
)
from pyspark.sql.functions import current_timestamp

# CONFIGURATION

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)


def get_env_var(name: str) -> str:
    value = os.getenv(name)
    if not value:
        logger.error(f"La variable d'environnement {name} est obligatoire (voir .env)")
        sys.exit(1)
    return value


DB_HOST = get_env_var("DB_HOST")
DB_PORT = get_env_var("DB_PORT")
DB_NAME = get_env_var("DB_NAME")
DB_USER = get_env_var("DB_USER")
DB_PASSWORD = get_env_var("DB_PASSWORD")

# Coordonnées des 3 régions avec leurs noms
REGIONS = {
    "Region A": {
        "latitude": 34.0522, 
        "longitude": -118.2437, 
        "region_name": "Los Angeles, California, USA"
    },
    "Region B": {
        "latitude": 36.7783, 
        "longitude": -119.4179, 
        "region_name": "Fresno/Central Valley, California, USA"
    },
    "Region C": {
        "latitude": 40.7128, 
        "longitude": -74.006, 
        "region_name": "New York City, New York, USA"
    }
}

# Paramètres API Open-Meteo (sélection ciblée pour enrichissement futur)
WEATHER_FIELDS = [
    "wind_speed_100m",       # Vitesse à hauteur de hub (~100m)
    "wind_gusts_10m",        # Rafales (impact maintenance)
    "temperature_2m",        # Température (densité de l'air)
    "pressure_msl",          # Pression (calcul densité air)
    "precipitation"          # Précipitations (impact opérationnel)
]

OPENMETEO_PARAMS = {
    "hourly": WEATHER_FIELDS
}

# Schéma Spark pour les données météo
WEATHER_SCHEMA = StructType([
    StructField("date", StringType(), False),
    StructField("time", StringType(), False),
    StructField("latitude", DoubleType(), False),
    StructField("longitude", DoubleType(), False),
    StructField("region", StringType(), False),
    StructField("region_name", StringType(), False),
    StructField("wind_speed_100m", FloatType(), True),
    StructField("wind_gusts_10m", FloatType(), True),
    StructField("temperature_2m", FloatType(), True),
    StructField("pressure_msl", FloatType(), True),
    StructField("precipitation", FloatType(), True)
])

JDBC_URL = f"jdbc:postgresql://{DB_HOST}:{DB_PORT}/{DB_NAME}"
JDBC_PROPS = {
    "user": DB_USER,
    "password": DB_PASSWORD,
    "driver": "org.postgresql.Driver"
}


def init_spark(app_name: str = "WeatherAPIBronzeIngestion") -> SparkSession:
    """Initialise la session Spark avec le driver PostgreSQL."""
    return SparkSession.builder \
        .appName(app_name) \
        .config("spark.jars.packages", "org.postgresql:postgresql:42.6.0") \
        .config("spark.sql.legacy.timeParserPolicy", "LEGACY") \
        .getOrCreate()


def create_weather_table() -> None:
    """Crée la table bronze.weatherforecastapi_raw"""
    logger.info("Création de la table bronze.weatherforecastapi_raw...")
    
    try:
        with psycopg.connect(
            host=DB_HOST,
            port=DB_PORT,
            dbname=DB_NAME,
            user=DB_USER,
            password=DB_PASSWORD
        ) as conn:
            with conn.cursor() as cur:
                cur.execute("CREATE SCHEMA IF NOT EXISTS bronze")
                cur.execute("DROP TABLE IF EXISTS bronze.weatherforecastapi_raw")
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS bronze.weatherforecastapi_raw (
                        date DATE NOT NULL,
                        time TIME NOT NULL,
                        latitude DOUBLE PRECISION NOT NULL,
                        longitude DOUBLE PRECISION NOT NULL,
                        region VARCHAR(100) NOT NULL,
                        region_name VARCHAR(255) NOT NULL,
                        wind_speed_100m FLOAT,
                        wind_gusts_10m FLOAT,
                        temperature_2m FLOAT,
                        pressure_msl FLOAT,
                        precipitation FLOAT,
                        ingested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        source_api VARCHAR(100) DEFAULT 'Open-Meteo',
                        UNIQUE(region, date, time)
                    )
                """)
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_weather_region_datetime 
                    ON bronze.weatherforecastapi_raw(region, date, time)
                """)
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_weather_coordinates 
                    ON bronze.weatherforecastapi_raw(latitude, longitude)
                """)
                conn.commit()
                logger.info("Table créée avec succès")
    except Exception as e:
        logger.error(f"Erreur création table : {e}")
        raise


def fetch_openmeteo_hourly(lat: float, lon: float, date: str) -> dict:
    """Appelle l'API Open-Meteo pour une région et une date donnée"""
    
    url = "https://archive-api.open-meteo.com/v1/archive"
    params = {
        "latitude": lat,
        "longitude": lon,
        "start_date": date,
        "end_date": date,
        "hourly": ",".join(OPENMETEO_PARAMS["hourly"]),
        "timezone": "UTC"
    }
    
    try:
        logger.info(f"Appel API pour ({lat}, {lon}) le {date}")
        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
        return {"success": True, "data": response.json(), "params": params}
    except requests.RequestException as e:
        logger.error(f"Erreur API pour ({lat}, {lon}): {e}")
        return {"success": False, "error": str(e), "params": params}


def interpolate_value(val1: float | None, val2: float | None, fraction: float) -> float | None:
    """Interpole linéairement entre deux valeurs (gère les valeurs None)"""
    if val1 is None or val2 is None:
        return val1 if val1 is not None else val2
    return val1 + (val2 - val1) * fraction


def extract_hourly_data_by_hour(api_data: dict, target_date: str) -> dict:
    """
    Extrait les données horaires de l'API et les organise par heure (0-23).
    
    Retourne un dict : {0: {field: value}, 1: {field: value}, ...}
    """
    hourly = api_data.get("hourly", {})
    times = hourly.get("time", [])
    
    # Créer un dictionnaire avec toutes les valeurs pour chaque champ
    hourly_by_field = {field: hourly.get(field, []) for field in WEATHER_FIELDS}
    
    # Organiser par heure
    data_by_hour = {}
    for i, timestamp_str in enumerate(times):
        if timestamp_str.startswith(target_date):
            hour = int(timestamp_str.split("T")[1].split(":")[0])
            data_by_hour[hour] = {
                field: values[i] for field, values in hourly_by_field.items()
            }
    
    return data_by_hour


def interpolate_weather_fields(current_data: dict, next_data: dict, fraction: float) -> dict:
    """
    Interpole tous les champs météo entre deux heures.
    
    Args:
        current_data: Données de l'heure actuelle {field: value}
        next_data: Données de l'heure suivante {field: value}
        fraction: Position entre les deux heures (0.0 à ~0.83 pour 0-50min)
    
    Returns:
        Dict avec les valeurs interpolées pour chaque champ
    """
    interpolated = {}
    for field in WEATHER_FIELDS:
        current_val = current_data.get(field)
        next_val = next_data.get(field)
        interpolated[field] = interpolate_value(current_val, next_val, fraction)
    
    return interpolated


def create_weather_record(date: str, hour: int, minute: int,  region: str, region_name: str, lat: float, lon: float, 
    weather_values: dict) -> dict:
    """
    Crée un enregistrement météo complet pour un instant donné.
    """
    time_str = f"{hour:02d}:{minute:02d}:00"
    
    record = {
        "date": date,
        "time": time_str,
        "latitude": lat,
        "longitude": lon,
        "region": region,
        "region_name": region_name
    }
    
    # Ajouter tous les champs météo
    record.update(weather_values)
    
    return record


def generate_10min_intervals(data_by_hour: dict, target_date: str,region: str, region_name: str, lat: float, lon: float) -> list:
    """
    Génère des enregistrements toutes les 10 minutes (00:00 à 23:50)
    en interpolant entre les données horaires.
    """
    records = []
    
    for hour in range(24):
        for minute in range(0, 60, 10):
            # Récupérer les données de l'heure actuelle et suivante
            current_data = data_by_hour.get(hour, {})
            next_hour = (hour + 1) % 24
            next_data = data_by_hour.get(next_hour, {})
            
            # Calculer la fraction pour l'interpolation (0.0 = début heure, 0.833 = 50min)
            fraction = minute / 60.0
            
            # Interpoler tous les champs météo
            weather_values = interpolate_weather_fields(current_data, next_data, fraction)
            
            # Créer l'enregistrement complet
            record = create_weather_record(
                date=target_date,
                hour=hour,
                minute=minute,
                region=region,
                region_name=region_name,
                lat=lat,
                lon=lon,
                weather_values=weather_values
            )
            
            records.append(record)
    
    return records


def parse_api_response_to_10min(region: str, region_name: str, api_result: dict, target_date: str) -> list:
    """
    Transforme la réponse API horaire en données toutes les 10 minutes (00:00 à 23:50).
    
    Étapes :
    1. Vérifie le succès de l'appel API
    2. Extrait et organise les données horaires
    3. Génère 144 points interpolés (24h × 6 intervalles de 10min)
    """
    # Vérifier le succès de l'appel API
    if not api_result["success"]:
        logger.warning(f"API non disponible pour {region}")
        return []
    
    api_data = api_result["data"]
    
    # Extraire les données horaires organisées par heure (0-23)
    data_by_hour = extract_hourly_data_by_hour(api_data, target_date)
    
    if not data_by_hour:
        logger.warning(f"Aucune donnée pour {region} le {target_date}")
        return []
    
    # Générer les intervalles de 10 minutes avec interpolation
    records = generate_10min_intervals(
        data_by_hour=data_by_hour,
        target_date=target_date,
        region=region,
        region_name=region_name,
        lat=api_data["latitude"],
        lon=api_data["longitude"]
    )
    
    logger.info(f"{len(records)} enregistrements interpolés pour {region}")
    return records


def ingest_weather_to_bronze(spark: SparkSession, target_date: str) -> int:
    """
    Récupère les données météo pour chaque région et les insère en Bronze.
    
    Workflow :
    1. Appel API Open-Meteo pour chaque région
    2. Interpolation des données horaires → intervalles de 10min
    3. Création DataFrame Spark
    4. Écriture PostgreSQL
    """
    logger.info(f"Ingestion météo pour la date : {target_date}")
    
    # Collecter les données de toutes les régions
    all_records = []
    for region, coords in REGIONS.items():
        # Appel API
        result = fetch_openmeteo_hourly(
            lat=coords["latitude"],
            lon=coords["longitude"],
            date=target_date
        )
        
        # Parsing et interpolation
        records = parse_api_response_to_10min(
            region=region,
            region_name=coords["region_name"],
            api_result=result,
            target_date=target_date
        )
        all_records.extend(records)
    
    if not all_records:
        logger.warning("Aucune donnée météo à ingérer")
        return 0
    
    # Création DataFrame avec le schéma défini
    df_weather = spark.createDataFrame(all_records, WEATHER_SCHEMA) \
        .withColumn("ingested_at", current_timestamp()) \
        .orderBy("date", "time", "region")
    
    # Écriture PostgreSQL
    logger.info(f"Écriture de {len(all_records)} enregistrements dans bronze.weatherforecastapi_raw...")
    
    df_weather.write \
        .format("jdbc") \
        .option("url", JDBC_URL) \
        .option("dbtable", "bronze.weatherforecastapi_raw") \
        .option("stringtype", "unspecified") \
        .options(**JDBC_PROPS) \
        .mode("append") \
        .save()
    
    row_count = df_weather.count()
    logger.info(f"{row_count} enregistrements météo ingérés avec succès")
    return row_count


# INTERFACE CLI

@click.command()
@click.option('--jour', type=int, required=True, help='Jour à ingérer (ex: 15)')
@click.option('--mois', type=int, required=True, help='Mois à ingérer (ex: 6)')
@click.option('--annee', type=int, required=True, help='Année à ingérer (ex: 2024)')
def main(jour, mois, annee):
    """
    Point d'entrée principal du script.
    Ingère les données météo pour une date spécifique via l'API Open-Meteo.
    
    Exemple: uv run python ingestion_api_data.py --jour 15 --mois 6 --annee 2024
    """
    logger.info("Démarrage ingestion_api_data.py")
    
    # Construction de la date
    target_date = f"{annee:04d}-{mois:02d}-{jour:02d}"
    logger.info(f"Date cible : {target_date}")
    
    spark = init_spark()
    
    try:
        create_weather_table()
        ingest_weather_to_bronze(spark, target_date)
        logger.info("Ingestion API terminée avec succès !")
    except Exception as e:
        logger.error(f"Erreur critique : {e}", exc_info=True)
        sys.exit(1)
    finally:
        spark.stop()
        logger.info("Session Spark fermée")


if __name__ == "__main__":
    main()