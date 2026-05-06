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
		capacity DOUBLE PRECISION,
		location_name VARCHAR(100),
		latitude DOUBLE PRECISION,
		longitude DOUBLE PRECISION,
		region VARCHAR(100),
		status VARCHAR(50),
		responsible_department VARCHAR(100),
		wind_speed DOUBLE PRECISION,
		wind_direction VARCHAR(50),
		energy_produced DOUBLE PRECISION,
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


def get_existing_business_keys(spark: SparkSession) -> DataFrame:
	"""Récupère les clés primaires déjà présentes dans la table Silver."""
	existing_df: DataFrame = spark.read.jdbc(
		url=JDBC_URL,
		table="silver.windpowerturbine_cleaned",
		properties=JDBC_PROPS,
	)
	return existing_df.select("production_id")


def filter_new_rows(transformed_df: DataFrame, spark: SparkSession) -> DataFrame:
	"""Retire les lignes déjà chargées et dédoublonne le lot courant sur production_id."""
	logger.info("Filtrage des doublons sur la clé primaire production_id")

	try:
		existing_keys: DataFrame = get_existing_business_keys(spark)
		filtered_df: DataFrame = transformed_df.join(
			existing_keys,
			on=["production_id"],
			how="left_anti",
		)
	except Exception as error:
		logger.warning(
			f"Lecture de la table Silver impossible ou table vide, insertion complète du lot: {error}"
		)
		filtered_df = transformed_df

	# Sécurité supplémentaire: dédoublonnage interne du lot sur production_id.
	return filtered_df.dropDuplicates(["production_id"])


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
		.orderBy("date", "time", "turbine_name")
	)

	final_df.write.jdbc(
		url=JDBC_URL,
		table="silver.windpowerturbine_cleaned",
		mode="append",
		properties=JDBC_PROPS,
	)
	logger.info(f"{row_count} lignes insérées avec succès")
	return row_count


def log_final_count(spark: SparkSession) -> None:
	"""Affiche le nombre total de lignes présentes en table Silver."""
	final_count: int = spark.read.jdbc(
		url=JDBC_URL,
		table="silver.windpowerturbine_cleaned",
		properties=JDBC_PROPS,
	).count()
	logger.info(f"Total en base: {final_count} lignes")


def main() -> None:
	"""Orchestre l'ensemble du pipeline Silver pour les données éoliennes."""
	spark: SparkSession = init_spark()

	try:
		bronze_df: DataFrame = read_bronze_data(spark)
		transformed_df: DataFrame = transform_data(bronze_df)
		total_count: int = transformed_df.count()
		logger.info(f"Lignes à traiter: {total_count}")
		create_silver_table()
		filtered_df: DataFrame = filter_new_rows(transformed_df, spark)
		inserted_count: int = write_silver_data(filtered_df)
		logger.info(f"Pipeline terminé. Nouvelles lignes insérées: {inserted_count}")
		log_final_count(spark)
	finally:
		spark.stop()
		logger.info("Session Spark fermée")


if __name__ == "__main__":
	main()
