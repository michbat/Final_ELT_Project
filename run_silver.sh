#!/usr/bin/env bash
set -euo pipefail  # Exit on error, undefined variable, or error in pipeline

echo "Transformation couche Silver - Données de production éolienne et météo"

# Affecter les variables d'environnement avec des valeurs par défaut si elles ne sont pas définies
DB_HOST="${DB_HOST:-localhost}"
DB_PORT="${DB_PORT:-5432}"
DB_USER="${DB_USER:-admin}"
DB_PASSWORD="${DB_PASSWORD:-}"
DB_NAME="${DB_NAME:-wind_turbine_power}"


# Vérifier que les variables d'environnement critiques sont définies
if [ -z "${DB_USER}" ] || [ -z "${DB_PASSWORD}" ] || [ -z "${DB_NAME}" ]; then
	echo "ERREUR: les variables d'environnement DB_USER, DB_PASSWORD et DB_NAME doivent être définies" >&2
	exit 1
fi

echo "Configuration:"
echo "  - DB Host: ${DB_HOST}:${DB_PORT}"
echo "  - Database: ${DB_NAME}"
echo ""

# Export des variables pour que les scripts Python les utilisent via dotenv
export DB_HOST DB_PORT DB_USER DB_PASSWORD DB_NAME DATASETS_DIR

echo "[1/3] Transformation des données de production de la table de la couche bronze (bronze.windturbinepower_raw)..."
python transform_windturbinepower_data.py 

echo ""
echo "[2/3] Transformation des données météo de la table de la couche bronze (bronze.weatherforecastapi_raw)..."
python transform_api_data.py 

echo ""
echo "[3/3] Création de la table de synthèse (silver.windturbinepower_enriched)..."
python transform_windturbinepower_enriched.py 

echo ""
echo "Transformation et enregistrement des tables dans la couche Silver terminée avec succès!"
