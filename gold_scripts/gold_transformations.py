#!/usr/bin/env python3
"""
Transformation Gold des données wind turbine + météo.
Ce script reprend la logique du notebook gold_tranformations.ipynb
et l'organise en fonctions simples et maintenables.
"""

import logging
import os
import sys
import pandas as pd
from typing import Any, Final
import psycopg
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

SOURCE_TABLE: Final[str] = "silver.windturbinepower_enriched"


def init_spark(app_name: str = "WindPowerGoldTransformation") -> SparkSession:
    """Initialise et retourne une session Spark avec le driver JDBC PostgreSQL."""
    return (
        SparkSession.builder.appName(app_name)
        .config("spark.jars.packages", "org.postgresql:postgresql:42.6.0")
        .config("spark.sql.legacy.timeParserPolicy", "LEGACY")
        .getOrCreate()
    )


def read_source_data(spark: SparkSession) -> DataFrame:
    """Lit la table Silver enrichie source."""
    logger.info("Lecture de la table source %s", SOURCE_TABLE)
    return spark.read.jdbc(
        url=JDBC_URL,
        table=SOURCE_TABLE,
        properties=JDBC_PROPS,
    )


def build_dimensions(df: DataFrame) -> tuple[DataFrame, DataFrame, DataFrame, DataFrame, DataFrame]:
    """Construit les dimensions avec des cles stables basees sur les attributs naturels."""
    date_dim: DataFrame = (
        df.select("date", "day", "month", "quarter", "year")
        .distinct()
        .withColumnRenamed("date", "date_id")
        .select("date_id", "day", "month", "quarter", "year")
    )

    time_dim: DataFrame = (
        df.select("time", "hour_of_day", "minute_of_hour", "second_of_minute", "time_period")
        .distinct()
        .withColumnRenamed("time", "time_id")
        .select("time_id", "hour_of_day", "minute_of_hour", "second_of_minute", "time_period")
    )

    turbine_dim: DataFrame = (
        df.select("turbine_name", "capacity", "latitude", "longitude", "region", "region_name")
        .distinct()
        .withColumn("turbine_id", F.abs(F.hash(F.concat_ws("_", F.col("turbine_name"), F.col("region")))))
        .select("turbine_id", "turbine_name", "capacity", "latitude", "longitude", "region", "region_name")
    )

    operational_status_dim: DataFrame = (
        df.select("status", "responsible_department")
        .distinct()
        .withColumn(
            "status_id",
            F.abs(F.hash(F.concat_ws("_", F.col("status"), F.col("responsible_department")))),
        )
        .select("status_id", "status", "responsible_department")
    )

    location_dim: DataFrame = (
        df.select("latitude", "longitude", "region", "region_name")
        .distinct()
        .withColumn(
            "location_id",
            F.abs(F.hash(F.concat_ws("_", F.col("region"), F.col("latitude"), F.col("longitude")))),
        )
        .select("location_id", "latitude", "longitude", "region", "region_name")
    )

    return date_dim, time_dim, turbine_dim, operational_status_dim, location_dim


def build_facts(df: DataFrame,turbine_dim: DataFrame,operational_status_dim: DataFrame,location_dim: DataFrame) -> tuple[DataFrame, DataFrame]:
    """Construit les faits en injectant les cles etrangeres des dimensions."""
    df_with_keys: DataFrame = (
        df.join(
            turbine_dim.select(
                "turbine_id",
                "turbine_name",
                "capacity",
                "latitude",
                "longitude",
                "region",
                "region_name",
            ),
            ["turbine_name", "capacity", "latitude", "longitude", "region", "region_name"],
            "left",
        )
        .join(
            operational_status_dim.select("status_id", "status", "responsible_department"),
            ["status", "responsible_department"],
            "left",
        )
        .join(
            location_dim.select("location_id", "latitude", "longitude", "region", "region_name"),
            ["latitude", "longitude", "region", "region_name"],
            "left",
        )
    )

    fact_energy_production: DataFrame = (
        df_with_keys.select(
            col("production_id").alias("fact_production_id"),
            col("date").alias("date_id"),
            col("time").alias("time_id"),
            "turbine_id",
            "status_id",
            "energy_produced",
            "wind_speed_100m",
            "wind_direction",
        )
        .select(
            "fact_production_id",
            "date_id",
            "time_id",
            "turbine_id",
            "status_id",
            "energy_produced",
            "wind_speed_100m",
            "wind_direction",
        )
        .orderBy("date_id", "time_id", "turbine_id")
    )

    fact_weather_conditions: DataFrame = (
        df_with_keys.select(
            col("weather_id").alias("fact_weather_id"),
            col("date").alias("date_id"),
            col("time").alias("time_id"),
            "location_id",
            "temperature_2m",
            "pressure_msl",
            "precipitation",
            "wind_gust_10m",
            "wind_speed_100m",
        )
        .select(
            "fact_weather_id",
            "date_id",
            "time_id",
            "location_id",
            "temperature_2m",
            "pressure_msl",
            "precipitation",
            "wind_gust_10m",
            "wind_speed_100m",
        )
        .distinct()
        .orderBy("date_id", "time_id", "location_id")
    )

    return fact_energy_production, fact_weather_conditions


def create_gold_schema_and_tables() -> None:
    """Cree le schema gold et les tables cibles si elles n'existent pas."""
    with psycopg.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
        autocommit=True,
    ) as conn:
        with conn.cursor() as cur:
            cur.execute("CREATE SCHEMA IF NOT EXISTS gold;")

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS gold.date_dim (
                    date_id DATE PRIMARY KEY,
                    day INTEGER NOT NULL,
                    month INTEGER NOT NULL,
                    quarter INTEGER NOT NULL,
                    year INTEGER NOT NULL
                );
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS gold.time_dim (
                    time_id TIME PRIMARY KEY,
                    hour_of_day INTEGER NOT NULL,
                    minute_of_hour INTEGER NOT NULL,
                    second_of_minute INTEGER NOT NULL,
                    time_period VARCHAR(20) NOT NULL
                );
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS gold.turbine_dim (
                    turbine_id BIGINT PRIMARY KEY,
                    turbine_name VARCHAR(100) NOT NULL,
                    capacity INTEGER,
                    latitude NUMERIC(9,6),
                    longitude NUMERIC(9,6),
                    region VARCHAR(50),
                    region_name VARCHAR(200)
                );
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS gold.operational_status_dim (
                    status_id BIGINT PRIMARY KEY,
                    status VARCHAR(100) NOT NULL,
                    responsible_department VARCHAR(100)
                );
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS gold.location_dim (
                    location_id BIGINT PRIMARY KEY,
                    latitude NUMERIC(9,6),
                    longitude NUMERIC(9,6),
                    region VARCHAR(50),
                    region_name VARCHAR(200)
                );
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS gold.fact_energy_production (
                    fact_production_id BIGINT PRIMARY KEY,
                    date_id DATE NOT NULL,
                    time_id TIME NOT NULL,
                    turbine_id BIGINT NOT NULL,
                    status_id BIGINT NOT NULL,
                    energy_produced NUMERIC(12,2),
                    wind_speed_100m NUMERIC(6,2),
                    wind_direction VARCHAR(10),
                    FOREIGN KEY (date_id) REFERENCES gold.date_dim(date_id),
                    FOREIGN KEY (time_id) REFERENCES gold.time_dim(time_id),
                    FOREIGN KEY (turbine_id) REFERENCES gold.turbine_dim(turbine_id),
                    FOREIGN KEY (status_id) REFERENCES gold.operational_status_dim(status_id)
                );
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS gold.fact_weather_conditions (
                    fact_weather_id BIGINT PRIMARY KEY,
                    date_id DATE NOT NULL,
                    time_id TIME NOT NULL,
                    location_id BIGINT NOT NULL,
                    temperature_2m NUMERIC(5,2),
                    pressure_msl NUMERIC(7,2),
                    precipitation NUMERIC(8,2),
                    wind_gust_10m NUMERIC(6,2),
                    wind_speed_100m NUMERIC(6,2),
                    FOREIGN KEY (date_id) REFERENCES gold.date_dim(date_id),
                    FOREIGN KEY (time_id) REFERENCES gold.time_dim(time_id),
                    FOREIGN KEY (location_id) REFERENCES gold.location_dim(location_id)
                );
                """
            )
    logger.info("Schema et tables gold pretes")


def cast_time_column_for_upsert(df: DataFrame, time_col: str) -> DataFrame:
    """Convertit une colonne time au format timestamp pour l'UPSERT psycopg."""
    return df.withColumn(time_col, F.to_timestamp(F.date_format(F.col(time_col), "HH:mm:ss"), "HH:mm:ss"))


def upsert_rows(
    cur: psycopg.Cursor[Any],
    sql: Any,
    dataframe: pd.DataFrame,
    columns: list[str],
) -> int:
    """Execute un UPSERT ligne a ligne et retourne le nombre de lignes traitees."""
    for _, row in dataframe.iterrows():
        cur.execute(sql, tuple(row[column] for column in columns))
    return len(dataframe)


def upsert_dimensions(
    date_dim: DataFrame,
    time_dim: DataFrame,
    turbine_dim: DataFrame,
    operational_status_dim: DataFrame,
    location_dim: DataFrame,
) -> dict[str, int]:
    """Charge les dimensions en mode UPSERT et renvoie les compteurs."""
    date_pd: pd.DataFrame = date_dim.toPandas()
    time_pd: pd.DataFrame = cast_time_column_for_upsert(time_dim, "time_id").toPandas()
    turbine_pd: pd.DataFrame = turbine_dim.toPandas()
    status_pd: pd.DataFrame = operational_status_dim.toPandas()
    location_pd: pd.DataFrame = location_dim.toPandas()

    date_sql: str = """
        INSERT INTO gold.date_dim (date_id, day, month, quarter, year)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (date_id) DO UPDATE SET
            day = EXCLUDED.day,
            month = EXCLUDED.month,
            quarter = EXCLUDED.quarter,
            year = EXCLUDED.year;
    """
    time_sql: str = """
        INSERT INTO gold.time_dim (time_id, hour_of_day, minute_of_hour, second_of_minute, time_period)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (time_id) DO UPDATE SET
            hour_of_day = EXCLUDED.hour_of_day,
            minute_of_hour = EXCLUDED.minute_of_hour,
            second_of_minute = EXCLUDED.second_of_minute,
            time_period = EXCLUDED.time_period;
    """
    turbine_sql: str = """
        INSERT INTO gold.turbine_dim (turbine_id, turbine_name, capacity, latitude, longitude, region, region_name)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (turbine_id) DO UPDATE SET
            turbine_name = EXCLUDED.turbine_name,
            capacity = EXCLUDED.capacity,
            latitude = EXCLUDED.latitude,
            longitude = EXCLUDED.longitude,
            region = EXCLUDED.region,
            region_name = EXCLUDED.region_name;
    """
    status_sql: str = """
        INSERT INTO gold.operational_status_dim (status_id, status, responsible_department)
        VALUES (%s, %s, %s)
        ON CONFLICT (status_id) DO UPDATE SET
            status = EXCLUDED.status,
            responsible_department = EXCLUDED.responsible_department;
    """
    location_sql: str = """
        INSERT INTO gold.location_dim (location_id, latitude, longitude, region, region_name)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (location_id) DO UPDATE SET
            latitude = EXCLUDED.latitude,
            longitude = EXCLUDED.longitude,
            region = EXCLUDED.region,
            region_name = EXCLUDED.region_name;
    """

    with psycopg.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
    ) as conn:
        with conn.cursor() as cur:
            counts: dict[str, int] = {
                "date_dim": upsert_rows(cur, date_sql, date_pd, ["date_id", "day", "month", "quarter", "year"]),
                "time_dim": upsert_rows(
                    cur,
                    time_sql,
                    time_pd,
                    ["time_id", "hour_of_day", "minute_of_hour", "second_of_minute", "time_period"],
                ),
                "turbine_dim": upsert_rows(
                    cur,
                    turbine_sql,
                    turbine_pd,
                    ["turbine_id", "turbine_name", "capacity", "latitude", "longitude", "region", "region_name"],
                ),
                "operational_status_dim": upsert_rows(
                    cur,
                    status_sql,
                    status_pd,
                    ["status_id", "status", "responsible_department"],
                ),
                "location_dim": upsert_rows(
                    cur,
                    location_sql,
                    location_pd,
                    ["location_id", "latitude", "longitude", "region", "region_name"],
                ),
            }
        conn.commit()

    return counts


def upsert_facts(fact_energy_production: DataFrame, fact_weather_conditions: DataFrame) -> dict[str, int]:
    """Charge les tables de faits en mode UPSERT et renvoie les compteurs."""
    fact_energy_pd: pd.DataFrame = cast_time_column_for_upsert(fact_energy_production, "time_id").toPandas()
    fact_weather_pd: pd.DataFrame = cast_time_column_for_upsert(fact_weather_conditions, "time_id").toPandas()

    fact_energy_sql: str = """
        INSERT INTO gold.fact_energy_production
        (fact_production_id, date_id, time_id, turbine_id, status_id,
        energy_produced, wind_speed_100m, wind_direction)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (fact_production_id) DO UPDATE SET
            date_id = EXCLUDED.date_id,
            time_id = EXCLUDED.time_id,
            turbine_id = EXCLUDED.turbine_id,
            status_id = EXCLUDED.status_id,
            energy_produced = EXCLUDED.energy_produced,
            wind_speed_100m = EXCLUDED.wind_speed_100m,
            wind_direction = EXCLUDED.wind_direction;
    """
    fact_weather_sql: str = """
        INSERT INTO gold.fact_weather_conditions
        (fact_weather_id, date_id, time_id, location_id,
        temperature_2m, pressure_msl, precipitation, wind_gust_10m, wind_speed_100m)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (fact_weather_id) DO UPDATE SET
            date_id = EXCLUDED.date_id,
            time_id = EXCLUDED.time_id,
            location_id = EXCLUDED.location_id,
            temperature_2m = EXCLUDED.temperature_2m,
            pressure_msl = EXCLUDED.pressure_msl,
            precipitation = EXCLUDED.precipitation,
            wind_gust_10m = EXCLUDED.wind_gust_10m,
            wind_speed_100m = EXCLUDED.wind_speed_100m;
    """

    with psycopg.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
    ) as conn:
        with conn.cursor() as cur:
            counts: dict[str, int] = {
                "fact_energy_production": upsert_rows(
                    cur,
                    fact_energy_sql,
                    fact_energy_pd,
                    [
                        "fact_production_id",
                        "date_id",
                        "time_id",
                        "turbine_id",
                        "status_id",
                        "energy_produced",
                        "wind_speed_100m",
                        "wind_direction",
                    ],
                ),
                "fact_weather_conditions": upsert_rows(
                    cur,
                    fact_weather_sql,
                    fact_weather_pd,
                    [
                        "fact_weather_id",
                        "date_id",
                        "time_id",
                        "location_id",
                        "temperature_2m",
                        "pressure_msl",
                        "precipitation",
                        "wind_gust_10m",
                        "wind_speed_100m",
                    ],
                ),
            }
        conn.commit()

    return counts


def log_counts(
    date_dim: DataFrame,
    time_dim: DataFrame,
    turbine_dim: DataFrame,
    operational_status_dim: DataFrame,
    location_dim: DataFrame,
    fact_energy_production: DataFrame,
    fact_weather_conditions: DataFrame,
) -> None:
    """Affiche les compteurs des DataFrames construits avant chargement."""
    logger.info("Tables de dimension créées:")
    logger.info("- date_dim: %s lignes", date_dim.count())
    logger.info("- time_dim: %s lignes", time_dim.count())
    logger.info("- turbine_dim: %s lignes", turbine_dim.count())
    logger.info("- operational_status_dim: %s lignes", operational_status_dim.count())
    logger.info("- location_dim: %s lignes", location_dim.count())

    logger.info("Tables de faits créées:")
    logger.info("- fact_energy_production: %s lignes", fact_energy_production.count())
    logger.info("- fact_weather_conditions: %s lignes", fact_weather_conditions.count())


def log_upsert_summary(dim_counts: dict[str, int], fact_counts: dict[str, int]) -> None:
    """Affiche un résumé de chargement après UPSERT."""
    logger.info("Dimensions chargées avec succès en mode UPSERT (idempotent)")
    logger.info("date_dim: %s lignes insérées/mises à jour", dim_counts["date_dim"])
    logger.info("time_dim: %s lignes insérées/mises à jour", dim_counts["time_dim"])
    logger.info("turbine_dim: %s lignes insérées/mises à jour", dim_counts["turbine_dim"])
    logger.info(
        "operational_status_dim: %s lignes insérées/mises à jour",
        dim_counts["operational_status_dim"],
    )
    logger.info("location_dim: %s lignes insérées/mises à jour", dim_counts["location_dim"])

    logger.info("Insertion des tables de faits en mode UPSERT")
    logger.info(
        "fact_energy_production: %s lignes insérées/mises à jour",
        fact_counts["fact_energy_production"],
    )
    logger.info(
        "fact_weather_conditions: %s lignes insérées/mises à jour",
        fact_counts["fact_weather_conditions"],
    )

    logger.info("Résumé des données chargées:")
    logger.info("- %s dates", dim_counts["date_dim"])
    logger.info("- %s heures", dim_counts["time_dim"])
    logger.info("- %s turbines", dim_counts["turbine_dim"])
    logger.info("- %s statuts opérationnels", dim_counts["operational_status_dim"])
    logger.info("- %s localisations", dim_counts["location_dim"])
    logger.info("- %s enregistrements de production", fact_counts["fact_energy_production"])
    logger.info("- %s observations météo", fact_counts["fact_weather_conditions"])


def main() -> None:
    """Orchestre le pipeline Gold: lecture, transformation, creation schema, UPSERT."""
    spark: SparkSession = init_spark()
    try:
        source_df: DataFrame = read_source_data(spark)

        date_dim, time_dim, turbine_dim, operational_status_dim, location_dim = build_dimensions(source_df)
        fact_energy_production, fact_weather_conditions = build_facts(
            source_df,
            turbine_dim,
            operational_status_dim,
            location_dim,
        )

        log_counts(
            date_dim,
            time_dim,
            turbine_dim,
            operational_status_dim,
            location_dim,
            fact_energy_production,
            fact_weather_conditions,
        )

        create_gold_schema_and_tables()
        logger.info("Début de l'insertion des données en mode UPSERT")

        dim_counts: dict[str, int] = upsert_dimensions(
            date_dim,
            time_dim,
            turbine_dim,
            operational_status_dim,
            location_dim,
        )
        fact_counts: dict[str, int] = upsert_facts(fact_energy_production, fact_weather_conditions)

        log_upsert_summary(dim_counts, fact_counts)
    finally:
        spark.stop()
        logger.info("Session Spark fermée")


if __name__ == "__main__":
    main()
