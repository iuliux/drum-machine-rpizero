#!/bin/bash

# Setup script to install the drum machine as a systemd service
# Run this once on the Raspberry Pi with: bash /path/to/setup-service.sh

set -e

echo "======================================"
echo "Drum Machine Systemd Service Setup"
echo "======================================"

# Check if running as root
if [ "$EUID" -ne 0 ]; then
   echo "This script must be run as root (use: sudo bash setup-service.sh)"
   exit 1
fi

SERVICE_FILE="/etc/systemd/system/drummachine.service"
SOURCE_FILE="$(dirname "$0")/drummachine.service"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo ""
echo "Installing systemd service from: $SOURCE_FILE"
echo "Target location: $SERVICE_FILE"
echo ""

# Copy the service file
if [ -f "$SOURCE_FILE" ]; then
    cp "$SOURCE_FILE" "$SERVICE_FILE"
    echo "✓ Service file copied"
else
    echo "✗ Error: drummachine.service not found at $SOURCE_FILE"
    exit 1
fi

# Set proper permissions
chmod 644 "$SERVICE_FILE"
echo "✓ Permissions set"

# Reload systemd daemon
systemctl daemon-reload
echo "✓ Systemd daemon reloaded"

# Enable the service to start on boot
systemctl enable drummachine.service
echo "✓ Service enabled for startup"

# Show status
echo ""
echo "======================================"
echo "Service Installation Complete!"
echo "======================================"
echo ""
echo "Next steps:"
echo "  1. Test the service: sudo systemctl start drummachine"
echo "  2. Check status:     sudo systemctl status drummachine"
echo "  3. View logs:        journalctl -u drummachine -f"
echo "  4. Stop service:     sudo systemctl stop drummachine"
echo "  5. Disable startup:  sudo systemctl disable drummachine"
echo ""
echo "The service will automatically start on next reboot."
echo ""
