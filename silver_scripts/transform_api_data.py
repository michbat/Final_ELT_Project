#!/usr/bin/env python3
"""
Transformation Silver des données météo API.
Ce script reprend la logique du notebook transform_api_data.ipynb
et l'organise en fonctions simples pour un usage scriptable.
"""

import logging
import os
import sys
import psycopg
from typing import Final
import pandas as pd
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
	"""Retourne une variable d'environnement obligatoire ou arrete le script."""

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


def init_spark(app_name: str = "WeatherAPISilverTransformation") -> SparkSession:
	"""Initialise et retourne une session Spark avec le driver JDBC PostgreSQL."""
	return (
		SparkSession.builder.appName(app_name)
		.config("spark.jars.packages", "org.postgresql:postgresql:42.6.0")
		.config("spark.sql.legacy.timeParserPolicy", "LEGACY")
		.getOrCreate()
	)


def read_bronze_data(spark: SparkSession) -> DataFrame:
	"""Lit la table Bronze source dans un DataFrame Spark."""
	logger.info("Lecture de la table Bronze %s", "bronze.weatherforecastapi_raw")
	return spark.read.jdbc(url=JDBC_URL, table="bronze.weatherforecastapi_raw", properties=JDBC_PROPS)


def transform_data(bronze_df: DataFrame) -> DataFrame:
	"""Nettoie, arrondit et enrichit les données météo avec des attributs temporels."""
	logger.info("Transformation et enrichissement des données météo")

	transformed_df: DataFrame = (
		bronze_df
  		.withColumn("wind_gusts_10m", round(col("wind_gusts_10m"), 2))
		.withColumn("temperature_2m", round(col("temperature_2m"), 2))
		.withColumn("cloud_cover", round(col("cloud_cover"), 2))
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
		.drop("ingested_at", "source_api")
	)

	return transformed_df


def create_silver_table() -> None:
	"""Cree la table Silver et garantit la contrainte d'unicité métier."""
	create_table_sql: str = """
	CREATE TABLE IF NOT EXISTS silver.weatherforecastapi_cleaned (
		weather_id BIGSERIAL PRIMARY KEY,
		date DATE,
		time TIME,
		latitude NUMERIC(9,6),
		longitude NUMERIC(9,6),
		region VARCHAR(100),
		region_name VARCHAR(255),
		wind_gusts_10m NUMERIC(6,2),
		temperature_2m NUMERIC(5,2),
		cloud_cover NUMERIC(5,2),
		day INTEGER,
		month INTEGER,
		quarter INTEGER,
		year INTEGER,
		hour_of_day INTEGER,
		minute_of_hour INTEGER,
		second_of_minute INTEGER,
		time_period VARCHAR(20),
		CONSTRAINT unique_weather_record UNIQUE (date, time, region, region_name)
	);
	"""

	# Le script cree la table si necessaire puis laisse PostgreSQL gerer l'unicite.
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

			try:
				cur.execute(
					"""
					ALTER TABLE silver.weatherforecastapi_cleaned
					ADD CONSTRAINT unique_weather_record
					UNIQUE (date, time, region, region_name);
					"""
				)
				logger.info(
					"Contrainte UNIQUE ajoutée sur (date, time, region, region_name)"
				)
			except (psycopg.errors.DuplicateTable, psycopg.errors.DuplicateObject):
				logger.info("La contrainte UNIQUE existe déjà")


def upsert_silver_data(transformed_df: DataFrame) -> None:
	"""Insère ou met à jour les données Silver via UPSERT PostgreSQL."""
	row_count: int = transformed_df.count()
	if row_count == 0:
		logger.info("Aucune donnée à traiter")
		return # On sort de la fonction sans faire d'UPSERT si il n'y a aucune donnée à traiter

	logger.info("Lignes à traiter: %s", row_count)

	# On convertit le DataFrame Spark en Pandas pour faciliter l'itération et l'insertion ligne par ligne.
	df_transformed_pd: pd.DataFrame = transformed_df.toPandas()

	upsert_sql = """
		INSERT INTO silver.weatherforecastapi_cleaned (
			date, time, latitude, longitude, region, region_name,
			wind_gusts_10m, temperature_2m, cloud_cover, day, month, quarter, year,
			hour_of_day, minute_of_hour, second_of_minute, time_period
		) VALUES (%(date)s, %(time)s, %(latitude)s, %(longitude)s, %(region)s, %(region_name)s,
                %(wind_gusts_10m)s, %(temperature_2m)s, %(cloud_cover)s, %(day)s, %(month)s,%(quarter)s, %(year)s,%(hour_of_day)s, %(minute_of_hour)s, %(second_of_minute)s, %(time_period)s)
		ON CONFLICT (date, time, region, region_name) DO UPDATE SET
			latitude = EXCLUDED.latitude,
			longitude = EXCLUDED.longitude,
			wind_gusts_10m = EXCLUDED.wind_gusts_10m,
			temperature_2m = EXCLUDED.temperature_2m,
			cloud_cover = EXCLUDED.cloud_cover,
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
			cur.execute("SELECT COUNT(*) FROM silver.weatherforecastapi_cleaned")
			result_before = cur.fetchone()
			count_before: int = result_before[0] if result_before else 0

			# Exécuter l'UPSERT pour chaque ligne
			for _, row in df_transformed_pd.iterrows():
				params = {str(k): v for k, v in row.items()}
				cur.execute(upsert_sql, params)

			# Compter les lignes APRÈS UPSERT
			cur.execute("SELECT COUNT(*) FROM silver.weatherforecastapi_cleaned")
			result_after = cur.fetchone()
			count_after: int = result_after[0] if result_after else 0

	new_rows_count: int = count_after - count_before
	duplicates_avoided_count: int = row_count - new_rows_count

	logger.info("%s lignes traitées avec UPSERT", row_count)
	logger.info("Nouvelles lignes: %s", new_rows_count)
	logger.info("Doublons évités: %s", duplicates_avoided_count)
	

def main() -> None:
	"""Orchestre l'ensemble du pipeline Silver pour les données météo API."""
	spark: SparkSession = init_spark()

	try:
		# ETAPE 1 : Lecture Bronze + transformation
		bronze_df: DataFrame = read_bronze_data(spark)
		transformed_df: DataFrame = transform_data(bronze_df)

		# ETAPE 2 : Preparation table Silver
		create_silver_table()

		# ETAPE 3 : UPSERT en base
		upsert_silver_data(transformed_df)
	
	finally:
		spark.stop()
		logger.info("Session Spark fermée")


if __name__ == "__main__":
	main()
