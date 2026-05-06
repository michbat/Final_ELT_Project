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
BUSINESS_KEY: Final[list[str]] = ["production_id", "date", "time"]


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
		"wind_speed_100m",
		"wind_gusts_10m",
		"temperature_2m",
		"pressure_msl",
		"precipitation",
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
			col("m.wind_speed_100m").alias("wind_speed_100m"),
			col("m.wind_gusts_10m").alias("wind_gust_10m"),
			col("w.wind_direction").alias("wind_direction"),
			col("m.temperature_2m").alias("temperature_2m"),
			col("m.pressure_msl").alias("pressure_msl"),
			col("m.precipitation").alias("precipitation"),
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
		wind_speed_100m NUMERIC(6,2),
		wind_gust_10m NUMERIC(6,2),
		wind_direction VARCHAR(50),
		temperature_2m NUMERIC(5,2),
		pressure_msl NUMERIC(7,2),
		precipitation NUMERIC(8,2),
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
			logger.info(f"Table silver.windturbinepower_enriched créée")


def get_existing_business_keys(spark: SparkSession) -> DataFrame:
	"""Récupère les clés métier déjà présentes dans la table enrichie."""
	existing_df: DataFrame = spark.read.jdbc(
		url=JDBC_URL,
		table="silver.windturbinepower_enriched",
		properties=JDBC_PROPS,
	)
	return existing_df.select(
		"production_id",
		"date",
		F.date_format(col("time"), "HH:mm:ss").alias("time"),
	)


def filter_new_rows(transformed_df: DataFrame, spark: SparkSession) -> DataFrame:
	"""Retire les lignes déjà chargées selon la clé métier choisie."""
	logger.info(f"Filtrage des doublons sur la clé métier ['production_id', 'date', 'time']")

	try:
		existing_keys: DataFrame = get_existing_business_keys(spark)
		filtered_df: DataFrame = transformed_df.join(
			existing_keys,
			on=["production_id", "date", "time"],
			how="left_anti",
		)
	except Exception as error:
		logger.warning(
			f"Lecture de la table enrichie impossible ou table vide, insertion complète du lot: {error}"
		)
		filtered_df = transformed_df

	return filtered_df


def write_silver_data(filtered_df: DataFrame, spark: SparkSession) -> int:
	"""Trie puis insère les nouvelles lignes dans la table enrichie."""
	row_count: int = filtered_df.count()

	if row_count == 0:
		logger.info("Aucune nouvelle donnée à insérer")
		return 0

	logger.info(f"Préparation de {row_count} lignes à insérer")

	final_df: DataFrame = (
		filtered_df.withColumn("time", F.to_timestamp(col("time"), "HH:mm:ss"))
		.orderBy("date", "time", "region", "turbine_name")
	)

	final_df.write.jdbc(
		url=JDBC_URL,
		table="silver.windturbinepower_enriched",
		mode="append",
		properties=JDBC_PROPS,
	)
	logger.info(f"{row_count} lignes insérées avec succès")
 
    # Vérification finale du nombre total de lignes en base après insertion.
	final_count: int = spark.read.jdbc(
		url=JDBC_URL,
		table="silver.windturbinepower_enriched",
		properties=JDBC_PROPS,
	).count()
	logger.info(f"Total en base: {final_count} lignes")
	return row_count


def main() -> None:
	"""Orchestre l'ensemble du pipeline Silver enrichi."""
	spark: SparkSession = init_spark()

	try:
		wind_df, weather_df = read_silver_sources(spark)
		enriched_df: DataFrame = transform_data(wind_df, weather_df)
		total_count: int = enriched_df.count()
		logger.info("Lignes enrichies à traiter: %s", total_count)

		create_silver_table()
		filtered_df: DataFrame = filter_new_rows(enriched_df, spark)
		inserted_count: int = write_silver_data(filtered_df, spark)
		logger.info("Pipeline terminé. Nouvelles lignes insérées: %s", inserted_count)
	finally:
		spark.stop()
		logger.info("Session Spark fermée")


if __name__ == "__main__":
	main()
