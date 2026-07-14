"""Thin wrapper — re-exports root app.py as the single production source."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from app import app, db, User, GoogleOAuthCredentials, SelectedSheet
from app import get_flow, get_google_connection_status, get_current_user, get_user_credentials
from app import encrypt_token, decrypt_token, get_sheets_service_for_user
from app import write_to_google_sheets, generate_excel
