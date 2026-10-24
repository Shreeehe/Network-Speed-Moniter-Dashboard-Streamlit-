"""
streamlit_app.py - Root entry point for Streamlit Community Cloud.

Streamlit Community Cloud automatically looks for `streamlit_app.py` in the root repository.
This file ensures deployment works instantly without needing to modify the main file path,
while also keeping `dashboard/app.py` available for custom paths.
"""
import os
import sys

# Add dashboard directory to sys.path so internal imports resolve
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DASHBOARD_DIR = os.path.join(BASE_DIR, "dashboard")
if DASHBOARD_DIR not in sys.path:
    sys.path.insert(0, DASHBOARD_DIR)

# Execute dashboard/app.py
app_script_path = os.path.join(DASHBOARD_DIR, "app.py")
with open(app_script_path, "r", encoding="utf-8") as f:
    code = compile(f.read(), app_script_path, "exec")
    exec(code, globals())
