#!/bin/bash
set -e

export RENDER=true

echo "Resetting database..."

# Clear storage directories
DEV_STORAGE="dev_storage"
RENDER_STORAGE="/opt/render/project/storage"

if [ -d "$DEV_STORAGE" ]; then
  echo "Clearing development storage: $DEV_STORAGE"
  find "$DEV_STORAGE" -mindepth 1 -delete
fi

if [ -d "$RENDER_STORAGE" ]; then
  echo "Clearing Render storage: $RENDER_STORAGE"
  find "$RENDER_STORAGE" -mindepth 1 -delete
fi

alembic downgrade base
alembic upgrade head
echo "Database reset complete."
