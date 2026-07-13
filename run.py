"""Startup script for Railway - runs the SEO research tool."""
from app import app
import os
port = int(os.environ.get('PORT', 5555))
print(f'[STARTUP] Starting on port {port}', flush=True)
app.run(host='0.0.0.0', port=port, debug=False)
