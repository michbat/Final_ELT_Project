#!/usr/bin/env python3
"""
Transformation Silver enrichie des données éoliennes + météo.
Ce script reprend la logique du notebook transform_windturbinepower_enriched.ipynb
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
from pyspark.sql.functions import col


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

WIND_SILVER_TABLE: Final[str] = "silver.windpowerturbine_cleaned"
WEATHER_SILVER_TABLE: Final[str] = "silver.weatherforecastapi_cleaned"
ENRICHED_SILVER_TABLE: Final[str] = "silver.windturbinepower_enriched"


def init_spark(app_name: str = "WindPowerEnrichedSilverTransformation") -> SparkSession:
	"""Initialise et retourne une session Spark avec le driver JDBC PostgreSQL."""
	return (
		SparkSession.builder.appName(app_name)
		.config("spark.jars.packages", "org.postgresql:postgresql:42.6.0")
		.config("spark.sql.legacy.timeParserPolicy", "LEGACY")
		.getOrCreate()
	)


def read_silver_sources(spark: SparkSession) -> tuple[DataFrame, DataFrame]:
	"""Lit les deux tables Silver sources dans des DataFrames Spark."""
	logger.info(f"Lecture de la table source silver.windpowerturbine_cleaned")
	windpt_df: DataFrame = spark.read.jdbc(
		url=JDBC_URL,
		table="silver.windpowerturbine_cleaned",
		properties=JDBC_PROPS,
	)

	logger.info(f"Lecture de la table source silver.weatherforecastapi_cleaned")
	weatherapi_df: DataFrame = spark.read.jdbc(
		url=JDBC_URL,
		table="silver.weatherforecastapi_cleaned",
		properties=JDBC_PROPS,
	)

	return windpt_df, weatherapi_df


def transform_data(windpt_df: DataFrame, weatherapi_df: DataFrame) -> DataFrame:
	"""Construit le DataFrame enrichi avec jointure sur date, time, region."""
	logger.info("Jointure et construction du DataFrame enrichi")

	windpt_selected_df: DataFrame = windpt_df.select(
		"production_id",
		"date",
		F.date_format(col("time"), "HH:mm:ss").alias("time"),
		"latitude",
		"longitude",
		"region",
		"turbine_name",
		"capacity",
		"status",
		"responsible_department",
		"energy_produced",
		"wind_speed",
		"wind_direction",
		"day",
		"month",
		"quarter",
		"year",
		"hour_of_day",
		"minute_of_hour",
		"second_of_minute",
		"time_period",
	)

	weatherapi_selected_df: DataFrame = weatherapi_df.select(
		"weather_id",
		"date",
		F.date_format(col("time"), "HH:mm:ss").alias("time"),
		"region",
		"region_name",
		"wind_gusts_10m",
		"temperature_2m",
		"cloud_cover",
	)

	enriched_df: DataFrame = (
		windpt_selected_df.alias("w")
		.join(weatherapi_selected_df.alias("m"), on=["date", "time", "region"], how="left")
		.select(
			col("w.production_id").alias("production_id"),
			col("m.weather_id").alias("weather_id"),
			col("w.date").alias("date"),
			col("w.time").alias("time"),
			col("w.latitude").alias("latitude"),
			col("w.longitude").alias("longitude"),
			col("w.region").alias("region"),
			col("m.region_name").alias("region_name"),
			col("w.turbine_name").alias("turbine_name"),
			col("w.capacity").alias("capacity"),
			col("w.status").alias("status"),
			col("w.responsible_department").alias("responsible_department"),
			col("w.energy_produced").alias("energy_produced"),
			col("w.wind_speed").alias("wind_speed"),
			col("m.wind_gusts_10m").alias("wind_gust_10m"),
			col("w.wind_direction").alias("wind_direction"),
			col("m.temperature_2m").alias("temperature_2m"),
			col("m.cloud_cover").alias("cloud_cover"),
			col("w.day").alias("day"),
			col("w.month").alias("month"),
			col("w.quarter").alias("quarter"),
			col("w.year").alias("year"),
			col("w.hour_of_day").alias("hour_of_day"),
			col("w.minute_of_hour").alias("minute_of_hour"),
			col("w.second_of_minute").alias("second_of_minute"),
			col("w.time_period").alias("time_period"),
		)
	)
	enriched_df = enriched_df.orderBy("production_id", "date", "time","region")  # Tri
	return enriched_df


def create_silver_table() -> None:
	"""Crée la table de synthèse Silver enrichie."""
	create_table_sql: str = """
	CREATE TABLE IF NOT EXISTS silver.windturbinepower_enriched (
		prod_enriched_id BIGSERIAL PRIMARY KEY,
		production_id INTEGER,
		weather_id BIGINT,
		date DATE,
		time TIME,
		latitude NUMERIC(9,6),
		longitude NUMERIC(9,6),
		region VARCHAR(100),
		region_name VARCHAR(255),
		turbine_name VARCHAR(100),
		capacity INTEGER,
		status VARCHAR(50),
		responsible_department VARCHAR(100),
		energy_produced NUMERIC(12,2),
		wind_speed NUMERIC(6,2),
		wind_gust_10m NUMERIC(6,2),
		wind_direction VARCHAR(50),
		temperature_2m NUMERIC(5,2),
		cloud_cover NUMERIC(5,2),
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

			# Contrainte UNIQUE nécessaire pour ON CONFLICT (production_id, date, time)
			try:
				cur.execute(
					"""
					ALTER TABLE silver.windturbinepower_enriched
					ADD CONSTRAINT unique_windturbinepower_enriched_business_key
					UNIQUE (production_id, date, time);
					"""
				)
				logger.info("Contrainte UNIQUE ajoutée sur (production_id, date, time)")
			except (psycopg.errors.DuplicateObject, psycopg.errors.DuplicateTable):
				logger.info("La contrainte UNIQUE existe déjà")

			logger.info("Table silver.windturbinepower_enriched prête")


def upsert_silver_data(enriched_df: DataFrame) -> None:
	"""Insère ou met à jour les données enrichies via UPSERT PostgreSQL."""
	row_count: int = enriched_df.count()
	if row_count == 0:
		logger.info("Aucune donnée à traiter")
		return

	logger.info("Lignes enrichies à traiter: %s", row_count)

	# Conversion Spark -> pandas pour l'UPSERT ligne par ligne.
	enriched_df_pd: pd.DataFrame = enriched_df.toPandas()

	upsert_sql = """
		INSERT INTO silver.windturbinepower_enriched (
			production_id,
			weather_id,
			date,
			time,
			latitude,
			longitude,
			region,
			region_name,
			turbine_name,
			capacity,
			status,
			responsible_department,
			energy_produced,
			wind_speed,
			wind_gust_10m,
			wind_direction,
			temperature_2m,
			cloud_cover,
			day,
			month,
			quarter,
			year,
			hour_of_day,
			minute_of_hour,
			second_of_minute,
			time_period
		)
		VALUES (%(production_id)s, %(weather_id)s, %(date)s, %(time)s, %(latitude)s, %(longitude)s, %(region)s, %(region_name)s, %(turbine_name)s, %(capacity)s, %(status)s, %(responsible_department)s, %(energy_produced)s, %(wind_speed)s, %(wind_gust_10m)s, %(wind_direction)s, %(temperature_2m)s, %(cloud_cover)s, %(day)s, %(month)s, %(quarter)s, %(year)s, %(hour_of_day)s, %(minute_of_hour)s, %(second_of_minute)s, %(time_period)s)
		ON CONFLICT ON CONSTRAINT unique_windturbinepower_enriched_business_key DO UPDATE SET
			weather_id = EXCLUDED.weather_id,
			latitude = EXCLUDED.latitude,
			longitude = EXCLUDED.longitude,
			region = EXCLUDED.region,
			region_name = EXCLUDED.region_name,
			turbine_name = EXCLUDED.turbine_name,
			capacity = EXCLUDED.capacity,
			status = EXCLUDED.status,
			responsible_department = EXCLUDED.responsible_department,
			energy_produced = EXCLUDED.energy_produced,
			wind_speed = EXCLUDED.wind_speed,
			wind_gust_10m = EXCLUDED.wind_gust_10m,
			wind_direction = EXCLUDED.wind_direction,
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
			# Compter les lignes avant UPSERT.
			cur.execute("SELECT COUNT(*) FROM silver.windturbinepower_enriched")
			result_before = cur.fetchone()
			count_before: int = result_before[0] if result_before else 0

			# Exécuter l'UPSERT pour chaque ligne.
			for _, row in enriched_df_pd.iterrows():
				params = {str(k): v for k, v in row.items()}
				cur.execute(upsert_sql, params)

			# Compter les lignes après UPSERT.
			cur.execute("SELECT COUNT(*) FROM silver.windturbinepower_enriched")
			result_after = cur.fetchone()
			count_after: int = result_after[0] if result_after else 0

	new_rows_count: int = count_after - count_before
	duplicates_avoided_count: int = row_count - new_rows_count

	logger.info("%s lignes traitées avec UPSERT", row_count)
	logger.info("Nouvelles lignes: %s", new_rows_count)
	logger.info("Doublons évités: %s", duplicates_avoided_count)


def main() -> None:
	"""Orchestre l'ensemble du pipeline Silver enrichi."""
	spark: SparkSession = init_spark()

	try:
		wind_df, weather_df = read_silver_sources(spark)
		enriched_df: DataFrame = transform_data(wind_df, weather_df)
		create_silver_table()
		upsert_silver_data(enriched_df)
	finally:
		spark.stop()
		logger.info("Session Spark fermée")


if __name__ == "__main__":
	main()
