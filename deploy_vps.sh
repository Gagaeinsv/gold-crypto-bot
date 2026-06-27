#!/bin/bash

# Deployment script for Trading Analytics System on Ubuntu VPS
# Run this script on the VPS as root or with sudo

echo "=== Deploying Trading Analytics System Services ==="

# Get current project directory
PROJECT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
echo "Project Directory: $PROJECT_DIR"

# Install required system packages
echo "Installing pip requirements..."
pip3 install -r "$PROJECT_DIR/scalping_bot/requirements.txt" || true
pip3 install telethon streamlit yfinance pandas python-dotenv || true

# Copy systemd service files and update WorkingDirectory
echo "Configuring systemd services..."
TEMP_TRACKER="/tmp/analytics_tracker.service"
TEMP_DASHBOARD="/tmp/analytics_dashboard.service"

cp "$PROJECT_DIR/vps_services/analytics_tracker.service" "$TEMP_TRACKER"
cp "$PROJECT_DIR/vps_services/analytics_dashboard.service" "$TEMP_DASHBOARD"

# Update paths dynamically based on current folder
sed -i "s|WorkingDirectory=.*|WorkingDirectory=$PROJECT_DIR|g" "$TEMP_TRACKER"
sed -i "s|WorkingDirectory=.*|WorkingDirectory=$PROJECT_DIR|g" "$TEMP_DASHBOARD"

# Detect streamlit binary path
STREAMLIT_PATH=$(which streamlit)
if [ -z "$STREAMLIT_PATH" ]; then
    STREAMLIT_PATH="/usr/local/bin/streamlit"
fi
sed -i "s|ExecStart=.*streamlit|ExecStart=$STREAMLIT_PATH|g" "$TEMP_DASHBOARD"

# Copy to system systemd folder
sudo cp "$TEMP_TRACKER" /etc/systemd/system/analytics_tracker.service
sudo cp "$TEMP_DASHBOARD" /etc/systemd/system/analytics_dashboard.service

# Reload systemd
echo "Reloading systemd daemon..."
sudo systemctl daemon-reload

# Enable services to run on boot
echo "Enabling services on boot..."
sudo systemctl enable analytics_tracker.service
sudo systemctl enable analytics_dashboard.service

# Restart services
echo "Starting services..."
sudo systemctl restart analytics_tracker.service
sudo systemctl restart analytics_dashboard.service

# Check status
echo "Checking service status..."
sudo systemctl status analytics_tracker.service --no-pager
sudo systemctl status analytics_dashboard.service --no-pager

echo "=== Deployment Completed Successfully! ==="
echo "Streamlit is now running 24/7 on port 8501!"
