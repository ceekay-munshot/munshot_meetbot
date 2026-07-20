"""conftest.py -- pytest path setup for calendar-service unit tests."""
import sys
import os

SERVICE_ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, SERVICE_ROOT)

MEETING_API = os.path.join(os.path.dirname(__file__), "..", "..", "meeting-api")
sys.path.insert(0, MEETING_API)

ADMIN_MODELS = os.path.join(os.path.dirname(__file__), "..", "..", "..", "libs", "admin-models")
sys.path.insert(0, ADMIN_MODELS)
