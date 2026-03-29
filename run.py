#!/usr/bin/env python3
"""
onedesk – initialise database and start the development server.
Run: python run.py
"""
import os
from app import app, init_db

if __name__ == "__main__":
    # Ensure upload folder exists
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

    # Initialise DB + create admin user
    init_db()

    print("\n" + "="*50)
    print("  onedesk is running!")
    print("  Open: http://localhost:5000")
    print("="*50 + "\n")

    app.run(debug=True, host="0.0.0.0", port=5001)
