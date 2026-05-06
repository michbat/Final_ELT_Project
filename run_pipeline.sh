#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 3 ]]; then
  echo "Usage: $0 <jour> <mois> <annee>"
  echo "Exemple: $0 15 6 2024"
  exit 1
fi

JOUR="$1"
MOIS="$2"
ANNEE="$3"

if ! [[ "$JOUR" =~ ^[0-9]{1,2}$ && "$MOIS" =~ ^[0-9]{1,2}$ && "$ANNEE" =~ ^[0-9]{4}$ ]]; then
  echo "Erreur: format invalide. Attendu: jour(1-2 chiffres) mois(1-2 chiffres) annee(4 chiffres)."
  exit 1
fi

echo "[1/3] Lancement de bronze pour la date ${JOUR}/${MOIS}/${ANNEE}..."
INGEST_JOUR="$JOUR" INGEST_MOIS="$MOIS" INGEST_ANNEE="$ANNEE" docker compose up bronze

echo "[2/3] Lancement de silver..."
docker compose up silver

echo "[3/3] Lancement de gold..."
docker compose up gold

echo "Pipeline terminé avec succès."
