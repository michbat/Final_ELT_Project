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
		bronze_df.withColumn("wind_speed_100m", round(col("wind_speed_100m"), 2))
		.withColumn("wind_gusts_10m", round(col("wind_gusts_10m"), 2))
		.withColumn("temperature_2m", round(col("temperature_2m"), 2))
		.withColumn("pressure_msl", round(col("pressure_msl"), 2))
		.withColumn("precipitation", round(col("precipitation"), 2))
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
		wind_speed_100m NUMERIC(6,2),
		wind_gusts_10m NUMERIC(6,2),
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


def get_existing_business_keys(spark: SparkSession) -> DataFrame:
	"""Récupère les clés métier déjà présentes dans la table Silver."""
	existing_df: DataFrame = spark.read.jdbc(
		url=JDBC_URL,
		table="silver.weatherforecastapi_cleaned",
		properties=JDBC_PROPS,
	)
	return existing_df.select(
		"date",
		F.date_format(col("time"), "HH:mm:ss").alias("time"),
		"region",
		"region_name",
	)


def filter_new_rows(transformed_df: DataFrame, spark: SparkSession) -> DataFrame:
	"""Retire les lignes déjà chargées et dédoublonne le lot courant sur la clé métier."""
	logger.info(f"Filtrage des doublons sur la clé métier {['date', 'time', 'region', 'region_name']}")
 
	try:
		existing_keys: DataFrame = get_existing_business_keys(spark)
		filtered_df: DataFrame = transformed_df.join(
			existing_keys,
			on=["date", "time", "region", "region_name"],
			how="left_anti",
		)
	except Exception as error:
		logger.warning(
			f"Lecture de la table Silver impossible ou table vide, insertion complète du lot: {error}"
		)
		filtered_df = transformed_df

	# Garde une protection locale si le lot source contient un doublon inattendu.
	return filtered_df.dropDuplicates(["date", "time", "region", "region_name"])


def write_silver_data(filtered_df: DataFrame) -> int:
	"""Trie puis insère les nouvelles lignes dans la table Silver."""
	row_count: int = filtered_df.count()

	if row_count == 0:
		logger.info("Aucune nouvelle donnée à insérer")
		return 0

	logger.info(f"Préparation de {row_count} lignes à insérer")

	# Le tri facilite la lecture en base et garde un ordre stable d'insertion.
	final_df: DataFrame = (
		filtered_df.withColumn("time", F.to_timestamp(col("time"), "HH:mm:ss"))
		.orderBy("date", "time", "region")
	)

	final_df.write.jdbc(
		url=JDBC_URL,
		table="silver.weatherforecastapi_cleaned",
		mode="append",
		properties=JDBC_PROPS,
	)
	logger.info(f"{row_count} lignes insérées avec succès")
	return row_count


def log_final_count(spark: SparkSession) -> None:
	"""Affiche le nombre total de lignes présentes en table Silver."""
	final_count: int = spark.read.jdbc(
		url=JDBC_URL,
		table="silver.weatherforecastapi_cleaned",
		properties=JDBC_PROPS,
	).count()
	logger.info(f"Total en base: {final_count} lignes")


def main() -> None:
	"""Orchestre l'ensemble du pipeline Silver pour les données météo API."""
	spark: SparkSession = init_spark()

	try:
		bronze_df: DataFrame = read_bronze_data(spark)
		transformed_df: DataFrame = transform_data(bronze_df)
		total_count: int = transformed_df.count()
		logger.info("Lignes à traiter: %s", total_count)

		create_silver_table()
		filtered_df: DataFrame = filter_new_rows(transformed_df, spark)
		inserted_count: int = write_silver_data(filtered_df)
		logger.info("Pipeline terminé. Nouvelles lignes insérées: %s", inserted_count)
		log_final_count(spark)
	finally:
		spark.stop()
		logger.info("Session Spark fermée")


if __name__ == "__main__":
	main()
