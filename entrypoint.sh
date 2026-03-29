#!/bin/sh
set -e

# Ensure upload folder exists (volume may be freshly mounted)
mkdir -p /app/static/uploads /app/instance

# Run database migrations and create admin user if needed
python - <<'EOF'
from app import app, init_db
with app.app_context():
    init_db()
EOF

exec gunicorn \
    --bind 0.0.0.0:5000 \
    --workers 2 \
    --timeout 120 \
    --access-logfile - \
    --error-logfile - \
    "app:app"
