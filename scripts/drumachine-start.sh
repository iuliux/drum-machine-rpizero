#!/bin/bash

# Navigate to your project directory
cd /home/pi/drum-machine-rpizero

# Pull latest code from git
git pull origin master

# Wait a moment for system to stabilize
sleep 2

# Run the drum machine
sudo /usr/bin/python3 /home/pi/drum-machine-rpizero/drummachine.py
