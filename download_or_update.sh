#!/bin/bash

# Configuration
RAW_URL="https://raw.githubusercontent.com/gabrielcdv/BetterSocEstimatorGX/main/battery_monitor.py"
TARGET_FILE="virtual_battery.py"

echo "Updating $TARGET_FILE..."

# Download the file using curl
# -s: Silent mode
# -S: Show errors if it fails
# -L: Follow redirects
# -o: Output to specific filename (overwrites existing)
if curl -sSL "$RAW_URL" -o "$TARGET_FILE"; then
    echo "Successfully updated to the latest version."
    chmod +x "$TARGET_FILE" # Optional: ensures the script remains executable
else
    echo "Error: Failed to download the file. Check your internet connection or the URL."
    exit 1
fi
