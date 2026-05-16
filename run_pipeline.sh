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

# Validation de la plage de dates (15/06/2024 à 03/08/2024 inclus)
 # printf '%02d' : formater le nombre pour avoir au moins 2 chiffres
DATE_SAISIE="${ANNEE}$(printf '%02d' $((10#$MOIS)))$(printf '%02d' $((10#$JOUR)))"
DATE_MIN="20240615"  # 15/06/2024
DATE_MAX="20240803"  # 03/08/2024

if [[ "$DATE_SAISIE" -lt "$DATE_MIN" || "$DATE_SAISIE" -gt "$DATE_MAX" ]]; then
  echo "Erreur: la date ${JOUR}/${MOIS}/${ANNEE} est hors de l'intervalle autorisé."
  echo "Plage acceptable: 15/06/2024 à 03/08/2024 (inclus)."
  exit 1
fi

echo "[1/3] Lancement de bronze pour la date ${JOUR}/${MOIS}/${ANNEE}..."
docker compose run --rm bronze "$JOUR" "$MOIS" "$ANNEE"

echo "[2/3] Lancement de silver..."
docker compose run --rm silver

echo "[3/3] Lancement de gold..."
docker compose run --rm gold

echo "Pipeline terminé avec succès."
