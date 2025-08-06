#!/bin/bash
set -e

echo "Resetting database..."
alembic downgrade base
alembic upgrade head
echo "Database reset complete."
