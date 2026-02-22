#!/bin/bash

cd /home/pi/drum-machine-rpizero

# Configure git to allow this directory (owned by pi) to be accessed by root
git config --global --add safe.directory /home/pi/drum-machine-rpizero

# Brief stabilization delay
sleep 1

# Configure GPIO
export GPIOZERO_PIN_FACTORY=lgpio

# Run the drum machine with error handling
# Run directly as the service user (systemd will apply realtime scheduling)
exec /usr/bin/python3 /home/pi/drum-machine-rpizero/drummachine.py
