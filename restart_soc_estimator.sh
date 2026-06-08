#!/bin/bash

# 1. Kill any existing instances of the script
echo "Stopping existing battery_monitor.py prcesses..."
pkill -f battery_monitor.py

# Optional: short sleep to ensure resources are released
sleep 1

# 2. Start the virtual battery scrip, redirect output, and detach
echo "Starting battery_monitor.py and logging to log.txt..."
nohup python3 -u battery_monitor.py > log.txt 2>&1 &
echo "battery_monitor.py started in background with PID: $!"


