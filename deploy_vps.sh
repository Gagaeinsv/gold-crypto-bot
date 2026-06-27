#!/bin/bash

# Deployment script for Trading Analytics System on Ubuntu VPS
# Uses Python Virtual Environment to bypass PEP 668 restrictions

echo "=== Deploying Trading Analytics System Services ==="

# Get current project directory
PROJECT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
echo "Project Directory: $PROJECT_DIR"

# Install python3-venv if not present
echo "Ensuring python3-venv and system dependencies are installed..."
sudo apt update && sudo apt install -y python3-venv python3-pip

# Create virtual environment
VENV_DIR="$PROJECT_DIR/venv"
if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment at $VENV_DIR..."
    python3 -m venv "$VENV_DIR"
fi

# Install dependencies inside the virtual environment
echo "Installing dependencies inside venv..."
"$VENV_DIR/bin/pip" install --upgrade pip
"$VENV_DIR/bin/pip" install -r "$PROJECT_DIR/scalping_bot/requirements.txt" || true
"$VENV_DIR/bin/pip" install telethon streamlit yfinance pandas python-dotenv "moviepy<2.0" Pillow edge-tts google-api-python-client google-auth-httplib2 google-auth-oauthlib

# Create temporary services
TEMP_TRACKER="/tmp/analytics_tracker.service"
TEMP_DASHBOARD="/tmp/analytics_dashboard.service"

# Generate analytics_tracker.service content dynamically
cat <<EOF > "$TEMP_TRACKER"
[Unit]
Description=Trading Analytics System - Parser & Tracker
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$PROJECT_DIR
ExecStart=$VENV_DIR/bin/python main.py
Restart=always
RestartSec=5
StandardOutput=syslog
StandardError=syslog
SyslogIdentifier=analytics-tracker

[Install]
WantedBy=multi-user.target
EOF

# Generate analytics_dashboard.service content dynamically
cat <<EOF > "$TEMP_DASHBOARD"
[Unit]
Description=Trading Analytics System - Streamlit Dashboard
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$PROJECT_DIR
ExecStart=$VENV_DIR/bin/streamlit run app.py --server.port 8501 --server.address 0.0.0.0
Restart=always
RestartSec=5
StandardOutput=syslog
StandardError=syslog
SyslogIdentifier=analytics-dashboard

[Install]
WantedBy=multi-user.target
EOF

# Copy to systemd directory
echo "Copying service configurations..."
sudo cp "$TEMP_TRACKER" /etc/systemd/system/analytics_tracker.service
sudo cp "$TEMP_DASHBOARD" /etc/systemd/system/analytics_dashboard.service

# Reload systemd and enable services
echo "Reloading systemd and enabling services..."
sudo systemctl daemon-reload
sudo systemctl enable analytics_tracker.service
sudo systemctl enable analytics_dashboard.service

# Restart services
echo "Restarting services..."
sudo systemctl restart analytics_tracker.service
sudo systemctl restart analytics_dashboard.service

# Wait a brief moment for startup and check status
sleep 2
echo "Checking service status..."
sudo systemctl status analytics_tracker.service --no-pager
echo ""
sudo systemctl status analytics_dashboard.service --no-pager

echo "=== Deployment Completed Successfully! ==="
echo "Dashboard is running 24/7 on http://YOUR_VPS_IP:8501"
