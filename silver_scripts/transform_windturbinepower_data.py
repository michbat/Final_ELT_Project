#!/usr/bin/env python3
"""
Transformation Silver des données éoliennes.
Ce script reprend la logique du notebook transform_windturbinepower_data.ipynb
et l'organise en fonctions simples pour un usage scriptable.
"""

import logging
import os
import sys
import psycopg
import pandas as pd
from typing import Final
from dotenv import load_dotenv
from pyspark.sql import DataFrame, SparkSession
from pyspark.sql import functions as F
from pyspark.sql.functions import col, dayofmonth, month, quarter, round, when, year


load_dotenv()

logging.basicConfig(
	level=logging.INFO,
	format="%(asctime)s - %(levelname)s - %(message)s",
	handlers=[logging.StreamHandler(sys.stdout)],
)
logger: logging.Logger = logging.getLogger(__name__)


def get_env_var(name: str) -> str:
	"""Retourne une variable d'environnement obligatoire ou arrête le script."""
	value: str | None = os.getenv(name)
	if not value:
		logger.error("La variable d'environnement %s est obligatoire.", name)
		sys.exit(1)
	return value


# Final indique que ces variables ne doivent pas être modifiées après leur initialisation.
DB_HOST: Final[str] = get_env_var("DB_HOST")
DB_PORT: Final[str] = get_env_var("DB_PORT")
DB_NAME: Final[str] = get_env_var("DB_NAME")
DB_USER: Final[str] = get_env_var("DB_USER")
DB_PASSWORD: Final[str] = get_env_var("DB_PASSWORD")

JDBC_URL: Final[str] = f"jdbc:postgresql://{DB_HOST}:{DB_PORT}/{DB_NAME}"
JDBC_PROPS: Final[dict[str, str]] = {
	"user": DB_USER,
	"password": DB_PASSWORD,
	"driver": "org.postgresql.Driver",
}


def init_spark(app_name: str = "WindPowerSilverTransformation") -> SparkSession:
	"""Initialise et retourne une session Spark avec le driver JDBC PostgreSQL."""
	return (
		SparkSession.builder.appName(app_name)
		.config("spark.jars.packages", "org.postgresql:postgresql:42.6.0")
		.config("spark.sql.legacy.timeParserPolicy", "LEGACY")
		.getOrCreate()
	)

def read_bronze_data(spark: SparkSession) -> DataFrame:
	"""Lit la table Bronze source dans un DataFrame Spark."""
	logger.info("Lecture de la table Bronze bronze.windturbinepower_raw")
	return spark.read.jdbc(url=JDBC_URL, table="bronze.windturbinepower_raw", properties=JDBC_PROPS)

def transform_data(bronze_df: DataFrame) -> DataFrame:
	"""Nettoie, arrondit et enrichit les données éoliennes avec des attributs temporels."""
	logger.info("Transformation et enrichissement des données éoliennes")

	transformed_df: DataFrame = (
		bronze_df
		.withColumn("wind_speed", round(col("wind_speed"), 2))
		.withColumn("energy_produced", round(col("energy_produced"), 2))
		.withColumn("day", dayofmonth(col("date")))
		.withColumn("month", month(col("date")))
		.withColumn("quarter", quarter(col("date")))
		.withColumn("year", year(col("date")))
		.withColumn("time", F.date_format(col("time"), "HH:mm:ss"))
		.withColumn("hour_of_day", F.hour(col("time")))
		.withColumn("minute_of_hour", F.minute(col("time")))
		.withColumn("second_of_minute", F.second(col("time")))
		.withColumn(
			"time_period",
			when((col("hour_of_day") >= 5) & (col("hour_of_day") < 12), "Morning")
			.when((col("hour_of_day") >= 12) & (col("hour_of_day") < 17), "Afternoon")
			.when((col("hour_of_day") >= 17) & (col("hour_of_day") < 21), "Evening")
			.otherwise("Night"),
		)
		.drop("ingested_at", "source_file", "batch_id", "measured_at")
	)

	return transformed_df


def create_silver_table() -> None:
	"""Crée la table Silver et ajoute production_id comme clé primaire."""
	create_table_sql: str = """
	CREATE TABLE IF NOT EXISTS silver.windpowerturbine_cleaned (
		production_id INTEGER,
		date DATE,
		time TIME,
		turbine_name VARCHAR(100),
		capacity INTEGER,
		location_name VARCHAR(100),
		latitude NUMERIC(9,6),
		longitude NUMERIC(9,6),
		region VARCHAR(100),
		status VARCHAR(50),
		responsible_department VARCHAR(100),
		wind_speed NUMERIC(6,2),
		wind_direction VARCHAR(50),
		energy_produced NUMERIC(12,2),
		day INTEGER,
		month INTEGER,
		quarter INTEGER,
		year INTEGER,
		hour_of_day INTEGER,
		minute_of_hour INTEGER,
		second_of_minute INTEGER,
		time_period VARCHAR(20)
	);
	"""

	# Le script crée la table si nécessaire, puis ajoute la clé primaire sur production_id.
	with psycopg.connect(
		host=DB_HOST,
		port=DB_PORT,
		dbname=DB_NAME,
		user=DB_USER,
		password=DB_PASSWORD,
		autocommit=True,
	) as conn:
		with conn.cursor() as cur:
			cur.execute("CREATE SCHEMA IF NOT EXISTS silver;")
			cur.execute(create_table_sql)
			logger.info("Schéma silver et table windpowerturbine_cleaned créés")

			try:
				cur.execute(
					"""
					ALTER TABLE silver.windpowerturbine_cleaned
					ADD CONSTRAINT windpowerturbine_cleaned_pkey PRIMARY KEY (production_id);
					"""
				)
				logger.info("Clé primaire ajoutée sur (production_id)")
			except (
				psycopg.errors.DuplicateTable,
				psycopg.errors.DuplicateObject,
				psycopg.errors.InvalidTableDefinition,
			):
				logger.info("La clé primaire existe déjà")


def upsert_silver_data(transformed_df: DataFrame) -> None:
	"""Insère ou met à jour les données Silver via UPSERT PostgreSQL."""
	row_count: int = transformed_df.count()
	if row_count == 0:
		logger.info("Aucune donnée à traiter")
		return # On sort de la fonction sans faire d'UPSERT si il n'y a aucune donnée à traiter

	logger.info("Lignes à traiter: %s", row_count)

	# Conversion du DataFrame Spark en Pandas pour l'upsert ligne par ligne.
	df_transformed_pd: pd.DataFrame = transformed_df.toPandas()

	upsert_sql = """
		INSERT INTO silver.windpowerturbine_cleaned (
			production_id, date, time, turbine_name, capacity, location_name, latitude, longitude, region, status,
			responsible_department, wind_speed, wind_direction, energy_produced, day, month, quarter, year,
			hour_of_day, minute_of_hour, second_of_minute, time_period
		) VALUES (
			%(production_id)s, %(date)s, %(time)s, %(turbine_name)s, %(capacity)s, %(location_name)s,
			%(latitude)s, %(longitude)s, %(region)s, %(status)s, %(responsible_department)s,
			%(wind_speed)s, %(wind_direction)s, %(energy_produced)s, %(day)s, %(month)s,
			%(quarter)s, %(year)s, %(hour_of_day)s, %(minute_of_hour)s,
			%(second_of_minute)s, %(time_period)s
		)
		ON CONFLICT (production_id) DO UPDATE SET
			date = EXCLUDED.date,
			time = EXCLUDED.time,
			turbine_name = EXCLUDED.turbine_name,
			capacity = EXCLUDED.capacity,
			location_name = EXCLUDED.location_name,
			latitude = EXCLUDED.latitude,
			longitude = EXCLUDED.longitude,
			region = EXCLUDED.region,
			status = EXCLUDED.status,
			responsible_department = EXCLUDED.responsible_department,
			wind_speed = EXCLUDED.wind_speed,
			wind_direction = EXCLUDED.wind_direction,
			energy_produced = EXCLUDED.energy_produced,
			day = EXCLUDED.day,
			month = EXCLUDED.month,
			quarter = EXCLUDED.quarter,
			year = EXCLUDED.year,
			hour_of_day = EXCLUDED.hour_of_day,
			minute_of_hour = EXCLUDED.minute_of_hour,
			second_of_minute = EXCLUDED.second_of_minute,
			time_period = EXCLUDED.time_period;
	"""

	with psycopg.connect(
		host=DB_HOST,
		port=DB_PORT,
		dbname=DB_NAME,
		user=DB_USER,
		password=DB_PASSWORD,
		autocommit=True,
	) as conn:
		with conn.cursor() as cur:
			# Compter les lignes AVANT UPSERT
			cur.execute("SELECT COUNT(*) FROM silver.windpowerturbine_cleaned")
			result_before = cur.fetchone()
			count_before: int = result_before[0] if result_before else 0

			# Exécuter l'UPSERT pour chaque ligne
			for _, row in df_transformed_pd.iterrows():
				params = {str(k): v for k, v in row.items()}
				cur.execute(upsert_sql, params)

			# Compter les lignes APRÈS UPSERT
			cur.execute("SELECT COUNT(*) FROM silver.windpowerturbine_cleaned")
			result_after = cur.fetchone()
			count_after: int = result_after[0] if result_after else 0

	new_rows_count: int = count_after - count_before
	duplicates_avoided_count: int = row_count - new_rows_count

	logger.info("%s lignes traitées avec UPSERT", row_count)
	logger.info("Nouvelles lignes: %s", new_rows_count)
	logger.info("Doublons évités: %s", duplicates_avoided_count)
	
	


def main() -> None:
	"""Orchestre l'ensemble du pipeline Silver pour les données éoliennes."""
	spark: SparkSession = init_spark()

	try:
		# ETAPE 1 : Lecture Bronze + transformation
		bronze_df: DataFrame = read_bronze_data(spark)
		transformed_df: DataFrame = transform_data(bronze_df)

		# ETAPE 2 : Préparation table Silver
		create_silver_table()

		# ETAPE 3 : UPSERT en base
		upsert_silver_data(transformed_df)
	
	finally:
		spark.stop()
		logger.info("Session Spark fermée")


if __name__ == "__main__":
	main()
