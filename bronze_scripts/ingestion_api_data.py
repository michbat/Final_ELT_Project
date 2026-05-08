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
import click
import psycopg
import openmeteo_requests
import pandas as pd
import numpy as np
import requests_cache
from retry_requests import retry
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
        "region_name": "Los Angeles, California, USA",
        "timezone": "America/Los_Angeles"
    },
    "Region B": {
        "latitude": 36.7783, 
        "longitude": -119.4179, 
        "region_name": "Fresno/Central Valley, California, USA",
        "timezone": "America/Los_Angeles"
    },
    "Region C": {
        "latitude": 40.7128, 
        "longitude": -74.006, 
        "region_name": "New York City, New York, USA",
        "timezone": "America/New_York"
    }
}

# Paramètres API Open-Meteo (Archive API utilise 'hourly' pour les données historiques)
WEATHER_FIELDS = [
    "wind_gusts_10m",  # Rafales (impact maintenance)
    "temperature_2m",  # Température maximale (impact maintenance)      
    "cloud_cover"      # Couverture nuageuse totale (impact maintenance)       
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
    StructField("wind_gusts_10m", FloatType(), True),
    StructField("temperature_2m", FloatType(), True),
    StructField("cloud_cover", FloatType(), True)
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
                        latitude NUMERIC(9,6) NOT NULL,
                        longitude NUMERIC(9,6) NOT NULL,
                        region VARCHAR(100) NOT NULL,
                        region_name VARCHAR(255) NOT NULL,
                        wind_gusts_10m NUMERIC(6,2),
                        temperature_2m NUMERIC(5,2),
                        cloud_cover NUMERIC(5,2),
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


def fetch_openmeteo_hourly(lat: float, lon: float, date: str, timezone: str, openmeteo_client) -> dict:
    """Appelle l'API Open-Meteo pour une région et une date donnée"""
    
    url = "https://archive-api.open-meteo.com/v1/archive"
    params = {
        "latitude": lat,
        "longitude": lon,
        "start_date": date,
        "end_date": date,
        "hourly": ",".join(OPENMETEO_PARAMS["hourly"]),
        "timezone": timezone
    }
    
    try:
        logger.info(f"Appel API pour ({lat}, {lon}) le {date}")
        responses = openmeteo_client.weather_api(url, params=params)
        if not responses:
            return {"success": False, "error": "Aucune réponse API"}
        return {"success": True, "response": responses[0], "params": params}
    except Exception as e:
        logger.error(f"Erreur API pour ({lat}, {lon}): {e}")
        return {"success": False, "error": str(e), "params": params}


def extract_hourly_data_pandas(response, timezone: str) -> pd.DataFrame:
    """
    Extrait les données horaires de la réponse API Open-Meteo et les organise dans un DataFrame pandas.
    """
    hourly = response.Hourly()
    if hourly is None:
        logger.warning("Données hourly indisponibles")
        return pd.DataFrame()

    # ÉTAPE 1 : Récupération des timestamps horaires (24 points)
    timestamps_hourly = pd.date_range(
        start=pd.to_datetime(hourly.Time(), unit="s", utc=True),
        end=pd.to_datetime(hourly.TimeEnd(), unit="s", utc=True),
        freq=pd.Timedelta(seconds=hourly.Interval()),
        inclusive="left"
    )
    n_hourly = len(timestamps_hourly)
    
    # ÉTAPE 2 : Extraction des 3 variables avec gestion d'erreurs
    # Variable 1: wind_gusts_10m
    try:
        var = hourly.Variables(0)
        wind_gusts_10m = var.ValuesAsNumpy() if var else np.full(n_hourly, np.nan, dtype=float)
    except Exception as e:
        logger.warning(f"Erreur extraction wind_gusts_10m: {e}")
        wind_gusts_10m = np.full(n_hourly, np.nan, dtype=float)
    
    # Variable 2: temperature_2m
    try:
        var = hourly.Variables(1)
        temperature_2m = var.ValuesAsNumpy() if var else np.full(n_hourly, np.nan, dtype=float)
    except Exception as e:
        logger.warning(f"Erreur extraction temperature_2m: {e}")
        temperature_2m = np.full(n_hourly, np.nan, dtype=float)
    
    # Variable 3: cloud_cover
    try:
        var = hourly.Variables(2)
        cloud_cover = var.ValuesAsNumpy() if var else np.full(n_hourly, np.nan, dtype=float)
    except Exception as e:
        logger.warning(f"Erreur extraction cloud_cover: {e}")
        cloud_cover = np.full(n_hourly, np.nan, dtype=float)

    # ÉTAPE 3 : Création DataFrame avec conversion timezone
    df_hourly = pd.DataFrame({
        'timestamp': timestamps_hourly,
        'wind_gusts_10m': wind_gusts_10m,
        'temperature_2m': temperature_2m,
        'cloud_cover': cloud_cover
    })
    df_hourly['timestamp'] = df_hourly['timestamp'].dt.tz_convert(timezone)
    
    return df_hourly


def interpolate_to_10min(df_hourly: pd.DataFrame, target_date: str, timezone: str, 
    lat: float, lon: float, region: str, region_name: str) -> list:
    """
    Interpole les données horaires pour créer des points toutes les 10 minutes (24 → 144 points).
    """
    # ÉTAPE 1 : Création des 144 timestamps 10-min (00:00 à 23:50)
    start_time = pd.Timestamp(target_date, tz=timezone)
    timestamps_10min = pd.date_range(
        start=start_time,
        end=start_time + pd.Timedelta(days=1) - pd.Timedelta(minutes=10),
        freq='10min'
    )
    df_10min = pd.DataFrame({'timestamp': timestamps_10min})
    
    # ÉTAPE 2 : Fusion timestamps horaires + 10-min pour interpolation
    df_combined = pd.concat([
        df_hourly[['timestamp']].assign(source='hourly'),
        df_10min.assign(source='interpolated')
    ]).sort_values('timestamp').reset_index(drop=True)
    
    df_combined = df_combined.merge(df_hourly, on='timestamp', how='left')
    
    # ÉTAPE 3 : Interpolation linéaire des 3 variables
    df_combined['wind_gusts_10m'] = df_combined['wind_gusts_10m'].interpolate(method='linear')
    df_combined['temperature_2m'] = df_combined['temperature_2m'].interpolate(method='linear')
    df_combined['cloud_cover'] = df_combined['cloud_cover'].interpolate(method='linear')
        
    # ÉTAPE 4 : Filtrage uniquement les timestamps 10-min (144 points)
    df_final = df_combined[df_combined['source'] == 'interpolated'].copy()
    
    # ÉTAPE 5 : Conversion en records pour Spark
    records = []
    for _, row in df_final.iterrows():
        ts = row['timestamp']
        records.append({
            "date": ts.strftime("%Y-%m-%d"),
            "time": ts.strftime("%H:%M:%S"),
            "latitude": float(lat),
            "longitude": float(lon),
            "region": region,
            "region_name": region_name,
            "wind_gusts_10m": float(row['wind_gusts_10m']) if pd.notna(row['wind_gusts_10m']) else None,
            "temperature_2m": float(row['temperature_2m']) if pd.notna(row['temperature_2m']) else None,
            "cloud_cover": float(row['cloud_cover']) if pd.notna(row['cloud_cover']) else None
        })

    logger.info(f"{len(records)} enregistrements (10-min) créés pour {region}")
    return records


def parse_api_response_to_10min(region: str, region_name: str, coords: dict, 
    api_result: dict, target_date: str) -> list:
    """
    Transforme la réponse API horaire en données 10-minutes (24 → 144 points).
    """
    # Vérification succès API
    if not api_result["success"]:
        logger.warning(f"API non disponible pour {region}")
        return []
    
    response = api_result["response"]
    
    # Extraction données horaires
    df_hourly = extract_hourly_data_pandas(response, coords["timezone"])
    if df_hourly.empty:
        logger.warning(f"Aucune donnée pour {region} le {target_date}")
        return []
    
    # Interpolation → 144 points 10-min
    records = interpolate_to_10min(
        df_hourly=df_hourly,
        target_date=target_date,
        timezone=coords["timezone"],
        lat=float(response.Latitude()),
        lon=float(response.Longitude()),
        region=region,
        region_name=region_name
    )
    
    return records


def ingest_weather_to_bronze(spark: SparkSession, target_date: str) -> int:
    """
    Récupère les données météo pour chaque région et les insère en Bronze.
    """
    logger.info(f"Ingestion météo pour la date : {target_date}")
    
    # ÉTAPE 1 : Configuration client API avec cache et retry
    cache_session = requests_cache.CachedSession('.cache', expire_after=3600)
    retry_session = retry(cache_session, retries=5, backoff_factor=0.2)
    openmeteo = openmeteo_requests.Client(session=retry_session) # type: ignore
    
    # ÉTAPE 2 : Collecte des données pour les 3 régions
    all_records = []
    for region, coords in REGIONS.items():
        # Appel API horaire
        result = fetch_openmeteo_hourly(
            lat=coords["latitude"],
            lon=coords["longitude"],
            date=target_date,
            timezone=coords["timezone"],
            openmeteo_client=openmeteo
        )
        
        # Parsing et interpolation (24 → 144 points)
        records = parse_api_response_to_10min(
            region=region,
            region_name=coords["region_name"],
            coords=coords,
            api_result=result,
            target_date=target_date
        )
        all_records.extend(records)
    
    if not all_records:
        logger.warning("Aucune donnée météo à ingérer")
        return 0
    
    logger.info(f"Total : {len(all_records)} enregistrements (attendu : 432 pour 3 régions)")
    
    # ÉTAPE 3 : Création DataFrame Spark
    df_weather = spark.createDataFrame(all_records, WEATHER_SCHEMA) \
        .withColumn("ingested_at", current_timestamp()) \
        .orderBy("date", "time", "region")
    
    # ÉTAPE 4 : Écriture PostgreSQL
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