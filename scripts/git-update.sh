#!/bin/bash

cd /home/pi/drum-machine-rpizero

# Configure git safe directory
git config --global --add safe.directory /home/pi/drum-machine-rpizero

# Pull latest - ready for next boot
git pull origin master || echo "Warning: git pull failed"

echo "Update complete - new version ready for next restart"
