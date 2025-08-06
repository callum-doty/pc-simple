#!/bin/bash
set -e

export RENDER=true

echo "Resetting database..."

# Clear storage directory
echo "Clearing storage directory..."
find dev_storage -mindepth 1 -delete

alembic downgrade base
alembic upgrade head
echo "Database reset complete."
