#!/usr/bin/env python3
"""
ingestion_windpower_data.py
Ingestion couche Bronze : Données production éoliennes (CSV Mikail Altundas)
Stack : PySpark + PostgreSQL JDBC
"""

import os
import sys
import glob
import uuid
import logging
import click
import psycopg
from dotenv import load_dotenv
from pyspark.sql import SparkSession
from pyspark.sql.types import (
    StructType, StructField, IntegerType, StringType, FloatType, DoubleType
)
from pyspark.sql.functions import (
    col, to_timestamp, concat_ws, lit, current_timestamp, regexp_replace, to_date, date_format
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)



# ---------------------- CONFIGURATION ----------------------

load_dotenv()
print("JAVA_HOME:", os.environ.get("JAVA_HOME"))


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
DATASETS_DIR = get_env_var("DATASETS_DIR")

JDBC_URL = f"jdbc:postgresql://{DB_HOST}:{DB_PORT}/{DB_NAME}"
JDBC_PROPS = {
    "user": DB_USER,
    "password": DB_PASSWORD,
    "driver": "org.postgresql.Driver"
}



def init_spark(app_name: str = "WindPowerBronzeIngestion") -> SparkSession:
    """
    Initialise la session Spark avec le driver PostgreSQL.
    """
    return SparkSession.builder \
        .appName(app_name) \
        .config("spark.jars.packages", "org.postgresql:postgresql:42.6.0") \
        .config("spark.sql.legacy.timeParserPolicy", "LEGACY") \
        .getOrCreate()



# def create_bronze_schema_and_table(reset_table: bool = False) -> None:
def create_bronze_schema_and_table() -> None:
    """
    Crée le schéma et la table bronze.windpowerturbinepower_raw dans PostgreSQL.
    Si reset_table=True, la table est supprimée puis recréée.
    Sinon, elle est créée uniquement si elle n'existe pas.
    """
    logger.info("Création du schéma et de la table bronze.windturbinepower_raw...")
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
                cur.execute("DROP TABLE IF EXISTS bronze.windturbinepower_raw")
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS bronze.windturbinepower_raw (
                        production_id BIGINT,
                        date DATE,
                        time TIME,
                        turbine_name VARCHAR(100),
                        capacity INT,
                        location_name VARCHAR(100),
                        latitude DOUBLE PRECISION,
                        longitude DOUBLE PRECISION,
                        region VARCHAR(100),
                        status VARCHAR(50),
                        responsible_department VARCHAR(100),
                        wind_speed FLOAT,
                        wind_direction VARCHAR(10),
                        energy_produced FLOAT,
                        measured_at TIMESTAMP,
                        ingested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        source_file VARCHAR(255),
                        batch_id VARCHAR(100)
                    )
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_windturbinepower_raw_turbine_time ON bronze.windturbinepower_raw(turbine_name, measured_at)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_windturbinepower_raw_location ON bronze.windturbinepower_raw(latitude, longitude)")
                conn.commit()
                logger.info("Schéma et table créés avec succès.")
    except Exception as e:
        logger.error(f"Erreur lors de la création du schéma/table : {e}")
        raise



def ingest_csv_to_bronze(spark: SparkSession, csv_path: str) -> int:
    """
    Lit un fichier CSV et l'insère dans la table bronze.windturbinepower_raw.
    Retourne le nombre de lignes insérées.
    """
    logger.info(f"Lecture du CSV : {csv_path}")

    # Définition du schéma attendu
    csv_schema = StructType([
        StructField("production_id", IntegerType(), True),
        StructField("date", StringType(), True),
        StructField("time", StringType(), True),
        StructField("turbine_name", StringType(), True),
        StructField("capacity", IntegerType(), True),
        StructField("location_name", StringType(), True),
        StructField("latitude", DoubleType(), True),
        StructField("longitude", DoubleType(), True),
        StructField("region", StringType(), True),
        StructField("status", StringType(), True),
        StructField("responsible_department", StringType(), True),
        StructField("wind_speed", FloatType(), True),
        StructField("wind_direction", StringType(), True),
        StructField("energy_produced", FloatType(), True)
    ])

    # Lecture du CSV dans un DataFrame Spark
    df_raw = spark.read \
        .option("header", "true") \
        .schema(csv_schema) \
        .option("mode", "PERMISSIVE") \
        .option("columnNameOfCorruptRecord", "_corrupt_record") \
        .csv(csv_path)

    # Ajout des colonnes de métadonnées et transformation
    batch_id = str(uuid.uuid4())
    df_bronze = (
        df_raw
        .withColumn(
            "measured_at",
            to_timestamp(
                concat_ws(" ", col("date"), regexp_replace(col("time"), lit("-"), lit(":")))
            )
        )
        .withColumn("date", to_date(col("date"), "yyyy-MM-dd"))
        .withColumn("time", date_format(to_timestamp(col("time"), "HH-mm-ss"), "HH:mm:ss"))
        .withColumn("ingested_at", current_timestamp())
        .withColumn("source_file", lit(os.path.basename(csv_path)))
        .withColumn("batch_id", lit(batch_id))
    )

    # Sélection stricte des colonnes dans l'ordre attendu
    columns_table = [
        "production_id", "date", "time", "turbine_name", "capacity", "location_name", "latitude", "longitude", "region", "status", "responsible_department", "wind_speed", "wind_direction", "energy_produced", "measured_at", "ingested_at", "source_file", "batch_id"
    ]
    df_bronze = df_bronze.select(*columns_table)

    # Insertion dans PostgreSQL
    logger.info("Écriture dans bronze.windturbinepower_raw...")
    df_bronze.write \
        .format("jdbc") \
        .option("url", JDBC_URL) \
        .option("dbtable", "bronze.windturbinepower_raw") \
        .option("stringtype", "unspecified") \
        .options(**JDBC_PROPS) \
        .mode("append") \
        .save()

    row_count = df_bronze.count()
    logger.info(f"{row_count} lignes ingérées dans bronze.windturbinepower_raw")
    return row_count

@click.command()
@click.option('--jour', type=int, help='Jour du fichier à ingérer (ex: 24)')
@click.option('--mois', type=int, help='Mois du fichier à ingérer (ex: 6)')
@click.option('--annee', type=int, help='Année du fichier à ingérer (ex: 2024)')

def main(jour, mois, annee):
    """
    Point d'entrée principal du script.
    Si une date est fournie, ingère le fichier correspondant.
    Sinon, ingère tous les fichiers du dossier datasets/.
    """
    logger.info("Démarrage ingestion_windturbinepower_data.py")
    spark = init_spark()
    try:
        # create_bronze_schema_and_table(reset_table=reset_table)
        create_bronze_schema_and_table()
        if jour and mois and annee:
            # Ingestion d'un fichier spécifique
            date_str = f"{annee:04d}{mois:02d}{jour:02d}"
            csv_path = os.path.join(DATASETS_DIR, f"{date_str}_wind_power_data.csv")
            if not os.path.exists(csv_path):
                logger.error(f"Le fichier {csv_path} n'existe pas.")
                sys.exit(1)
            logger.info(f"Ingestion du fichier : {csv_path}")
            ingest_csv_to_bronze(spark, csv_path)
        else:
            # Ingestion de tous les fichiers CSV du dossier datasets/
            csv_files = sorted(glob.glob(os.path.join(DATASETS_DIR, "*.csv")))
            logger.info(f"Fichiers à ingérer : {csv_files}")
            for csv_path in csv_files:
                try:
                    ingest_csv_to_bronze(spark, csv_path)
                except Exception as e:
                    logger.error(f"Erreur lors de l'ingestion du fichier {csv_path} : {e}")
            logger.info("Ingestion de tous les fichiers CSV terminée avec succès.")
    except Exception as e:
        logger.error(f"Erreur critique : {e}", exc_info=True)
        sys.exit(1)
    finally:
        spark.stop()
        logger.info("Session Spark fermée.")


if __name__ == "__main__":
    main()