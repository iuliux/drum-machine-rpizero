#!/bin/bash

# Navigate to your project directory
cd /home/pi/drum-machine-rpizero

# Configure git to allow this directory (owned by pi) to be accessed by root
git config --global --add safe.directory /home/pi/drum-machine-rpizero

# Pull latest code from git (non-blocking - continue even if it fails)
git pull origin master || echo "Warning: git pull failed, continuing with current code"

# Wait a moment for system to stabilize
sleep 2

# Configure GPIO environment for systemd service context
# Use BCM factory (direct GPIO access) instead of auto-detection
export GPIOZERO_PIN_FACTORY=lgpio

# Run the drum machine with error handling
# Run directly as the service user (systemd will apply realtime scheduling)
exec /usr/bin/python3 /home/pi/drum-machine-rpizero/drummachine.py
