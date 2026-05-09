"""
DAG ELT Wind Turbine Power
Orchestre les scripts Bronze → Silver → Gold via BashOperator.

Paramètres du DAG (configurables au déclenchement manuel) :
    - date_ingestion : date à ingérer (YYYY-MM-DD)
"""


import os
from datetime import date, timedelta
from airflow import DAG
from airflow.providers.standard.operators.bash import BashOperator
from airflow.models import Param
from dotenv import load_dotenv
import pendulum
import openmeteo_requests

# Racine projet montée dans le conteneur Airflow
PROJECT_ROOT = "/opt/airflow/project"

# Charge les variables d'environnement depuis les fichiers montés.
# etl.env est chargé en dernier pour que les valeurs runtime du conteneur gagnent.
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))
load_dotenv(os.path.join(PROJECT_ROOT, "etl.env"), override=True)


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ValueError(f"La variable d'environnement {name} est obligatoire")
    return value


def _allowed_dates(start: date, end: date) -> list[str]:
    """Retourne toutes les dates autorisées au format YYYY-MM-DD (bornes incluses)."""
    values: list[str] = []
    current = start
    while current <= end:
        values.append(current.isoformat())
        current += timedelta(days=1)
    return values


ALLOWED_INGESTION_DATES = _allowed_dates(date(2024, 6, 15), date(2024, 8, 3))


# Variables d'environnement transmises aux tâches
PYSPARK_PYTHON_BIN = "/home/airflow/.local/bin/python3"

ETL_ENV = {
    "DB_HOST": _required_env("DB_HOST"),
    "DB_PORT": _required_env("DB_PORT"),
    "DB_USER": _required_env("DB_USER"),
    "DB_PASSWORD": _required_env("DB_PASSWORD"),
    "DB_NAME": _required_env("DB_NAME"),
    "DATASETS_DIR": f"{PROJECT_ROOT}/datasets",
    "PYSPARK_PYTHON": PYSPARK_PYTHON_BIN,
    "PYSPARK_DRIVER_PYTHON": PYSPARK_PYTHON_BIN,
}


# type: ignore
with DAG(
    dag_id="elt_wind_turbine_pipeline",
    description="Pipeline ELT : Bronze → Silver → Gold (PySpark + PostgreSQL)",
    start_date=pendulum.datetime(2024, 6, 15, tz="UTC"),
    schedule=None,  # Déclenchement manuel uniquement
    catchup=False,
    tags=["elt", "wind-turbine", "pyspark"],
    params={
        "date_ingestion": Param(
            "2024-06-15",
            type="string",
            format="date",
            enum=ALLOWED_INGESTION_DATES,
            description="Date à ingérer (intervalle autorisé: 2024-06-15 à 2024-08-03)",
        ),
    }  # pyright: ignore[reportArgumentType]
) as dag:

    # COUCHE BRONZE 
    bronze_windturbine = BashOperator(
        task_id="bronze_windturbine",
        bash_command=(
            "python bronze_scripts/ingestion_windturbinepower_data.py"
            " --jour {{ params.date_ingestion[8:10] }}"
            " --mois {{ params.date_ingestion[5:7] }}"
            " --annee {{ params.date_ingestion[0:4] }}"
        ),
        env=ETL_ENV,
        append_env=True,
        cwd=PROJECT_ROOT,
    )

    bronze_api = BashOperator(
        task_id="bronze_api",
        bash_command=(
            "python bronze_scripts/ingestion_api_data.py"
            " --jour {{ params.date_ingestion[8:10] }}"
            " --mois {{ params.date_ingestion[5:7] }}"
            " --annee {{ params.date_ingestion[0:4] }}"
        ),
        env=ETL_ENV,
        append_env=True,
        cwd=PROJECT_ROOT,
    )

    # COUCHE SILVER

    silver_windturbine = BashOperator(
        task_id="silver_windturbine",
        bash_command="python silver_scripts/transform_windturbinepower_data.py",
        env=ETL_ENV,
        append_env=True,
        cwd=PROJECT_ROOT,
    )

    silver_api = BashOperator(
        task_id="silver_api",
        bash_command="python silver_scripts/transform_api_data.py",
        env=ETL_ENV,
        append_env=True,
        cwd=PROJECT_ROOT,
    )

    silver_enriched = BashOperator(
        task_id="silver_enriched",
        bash_command="python silver_scripts/transform_windturbinepower_enriched.py",
        env=ETL_ENV,
        append_env=True,
        cwd=PROJECT_ROOT,
    )

    # COUCHE GOLD

    gold = BashOperator(
        task_id="gold",
        bash_command="python gold_scripts/gold_transformations.py",
        env=ETL_ENV,
        append_env=True,
        cwd=PROJECT_ROOT,
    )

    # DÉPENDANCES
    #
    #  bronze_windturbine ─┐
    #                      ├─► silver_windturbine ─┐
    #  bronze_api         ─┘                       ├─► silver_enriched ─► gold
    #                      ├─► silver_api         ─┘
    #

    silver_windturbine.set_upstream([bronze_windturbine, bronze_api])
    silver_api.set_upstream([bronze_windturbine, bronze_api])
    silver_enriched.set_upstream([silver_windturbine, silver_api])
    gold.set_upstream(silver_enriched)
