#!/bin/bash
set -e
cd /opt/m3_export_webapp
source venv/bin/activate
export $(grep -v '^#' .env | xargs)
exec gunicorn -w 2 -b 0.0.0.0:8080 app.app:app
