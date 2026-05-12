#!/usr/bin/env bash
set -euo pipefail  # Exit on error, undefined variable, or error in pipeline

echo "Ingestion couche Bronze - Données de production éolienne et météo"

# Affecter les variables d'environnement avec des valeurs par défaut si elles ne sont pas définies
DB_HOST="${DB_HOST:-localhost}"
DB_PORT="${DB_PORT:-5432}"
DB_USER="${DB_USER:-admin}"
DB_PASSWORD="${DB_PASSWORD:-}"
DB_NAME="${DB_NAME:-wind_turbine_power}"
DATASETS_DIR="${DATASETS_DIR:-/app/datasets}"

# Exiger exactement 3 arguments pour la date
if [[ $# -ne 3 ]]; then
  echo "ERREUR: Utilisation incorrecte. Fournir exactement 3 arguments." >&2
  echo "Usage: $0 [jour] [mois] [annee]" >&2
  echo "Exemple: $0 16 6 2024" >&2
  exit 1
fi

INGEST_JOUR="$1"
INGEST_MOIS="$2"
INGEST_ANNEE="$3"
echo "Arguments de ligne de commande détectés: ${INGEST_JOUR}/${INGEST_MOIS}/${INGEST_ANNEE}"

# Validation du format des arguments
if ! [[ "$INGEST_JOUR" =~ ^[0-9]{1,2}$ && "$INGEST_MOIS" =~ ^[0-9]{1,2}$ && "$INGEST_ANNEE" =~ ^[0-9]{4}$ ]]; then
  echo "Erreur: format invalide. Attendu: jour(1-2 chiffres) mois(1-2 chiffres) annee(4 chiffres)." >&2
  exit 1
fi

# Validation de la plage de dates (15/06/2024 à 03/08/2024 inclus)
DATE_SAISIE="${INGEST_ANNEE}$(printf '%02d' $INGEST_MOIS)$(printf '%02d' $INGEST_JOUR)"
DATE_MIN="20240615"  # 15/06/2024
DATE_MAX="20240803"  # 03/08/2024

if [[ "$DATE_SAISIE" -lt "$DATE_MIN" || "$DATE_SAISIE" -gt "$DATE_MAX" ]]; then
  echo "ERREUR: la date ${INGEST_JOUR}/${INGEST_MOIS}/${INGEST_ANNEE} est hors de l'intervalle autorisé." >&2
  echo "Plage acceptable: 15/06/2024 à 03/08/2024 (inclus)." >&2
  exit 1
fi

# Vérifier que les variables d'environnement critiques sont définies
if [ -z "${DB_USER}" ] || [ -z "${DB_PASSWORD}" ] || [ -z "${DB_NAME}" ]; then
	echo "ERREUR: les variables d'environnement DB_USER, DB_PASSWORD et DB_NAME doivent être définies" >&2
	exit 1
fi

echo "Configuration:"
echo "  - DB Host: ${DB_HOST}:${DB_PORT}"
echo "  - Database: ${DB_NAME}"
echo "  - Datasets: ${DATASETS_DIR}"
echo "  - Date API: ${INGEST_JOUR}/${INGEST_MOIS}/${INGEST_ANNEE}"
echo ""

# Export des variables pour que les scripts Python les utilisent via dotenv
export DB_HOST DB_PORT DB_USER DB_PASSWORD DB_NAME DATASETS_DIR

echo "[1/2] Ingestion des données de production éolienne (CSV)..."
python ingestion_windturbinepower_data.py --jour "${INGEST_JOUR}" --mois "${INGEST_MOIS}" --annee "${INGEST_ANNEE}"

echo ""
echo "[2/2] Ingestion des données météo via API Open-Meteo..."
python ingestion_api_data.py --jour "${INGEST_JOUR}" --mois "${INGEST_MOIS}" --annee "${INGEST_ANNEE}"

echo ""
echo "Ingestion terminée avec succès!"
