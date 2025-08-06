#!/bin/bash
set -e

export RENDER=true

echo "Resetting database..."
alembic downgrade base
alembic upgrade head
echo "Database reset complete."
