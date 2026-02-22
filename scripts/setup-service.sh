#!/bin/bash

# Setup script to install the drum machine as a systemd service
# Run this once on the Raspberry Pi with: bash /path/to/setup-service.sh

set -e

echo "======================================"
echo "Drum Machine Systemd Service Setup"
echo "======================================"

# Check if running as root
if [ "${EUID:-$(id -u)}" -ne 0 ]; then
   echo "This script must be run as root (use: sudo bash setup-service.sh)"
   exit 1
fi

MAIN_SERVICE_FILE="/etc/systemd/system/drummachine.service"
UPDATE_SERVICE_FILE="/etc/systemd/system/drummachine-update.service"
MAIN_SOURCE_FILE="$(dirname "$0")/drummachine.service"
UPDATE_SOURCE_FILE="$(dirname "$0")/drummachine-update.service"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo ""
echo "Installing systemd services..."
echo ""

# Copy the main service file
if [ -f "$MAIN_SOURCE_FILE" ]; then
    cp "$MAIN_SOURCE_FILE" "$MAIN_SERVICE_FILE"
    echo "✓ Main service file copied: $MAIN_SERVICE_FILE"
else
    echo "✗ Error: drummachine.service not found at $MAIN_SOURCE_FILE"
    exit 1
fi

# Copy the update service file
if [ -f "$UPDATE_SOURCE_FILE" ]; then
    cp "$UPDATE_SOURCE_FILE" "$UPDATE_SERVICE_FILE"
    echo "✓ Update service file copied: $UPDATE_SERVICE_FILE"
else
    echo "✗ Error: drummachine-update.service not found at $UPDATE_SOURCE_FILE"
    exit 1
fi

# Set proper permissions
chmod 644 "$MAIN_SERVICE_FILE"
chmod 644 "$UPDATE_SERVICE_FILE"
echo "✓ Permissions set"

# Reload systemd daemon
systemctl daemon-reload
echo "✓ Systemd daemon reloaded"

# Enable the main service to start on boot
systemctl enable drummachine.service
echo "✓ Main service enabled for startup"

# Enable the update service to start on boot
systemctl enable drummachine-update.service
echo "✓ Update service enabled for startup"

# Show status
echo ""
echo "======================================"
echo "Service Installation Complete!"
echo "======================================"
echo ""
echo "Two services have been installed:"
echo "  - drummachine.service        (starts immediately on boot)"
echo "  - drummachine-update.service (updates code in background)"
echo ""
echo "Next steps:"
echo "  1. Test the main service:    sudo systemctl start drummachine"
echo "  2. Check status:             sudo systemctl status drummachine"
echo "  3. View logs:                journalctl -u drummachine -f"
echo "  4. Check update logs:        journalctl -u drummachine-update -f"
echo "  5. Stop service:             sudo systemctl stop drummachine"
echo "  6. Disable startup:          sudo systemctl disable drummachine"
echo ""
echo "The services will automatically start on next reboot."
echo "The drum machine will start quickly, and git updates will happen in the background."
echo ""
