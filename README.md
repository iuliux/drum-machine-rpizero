# Setup

### 1. Flash Raspberry Pi OS Lite (64-bit)

### 2. Configure I2S

Add line to `/boot/firmware/config.txt`:

```
dtoverlay=hifiberry-dac
```

This configures the I2S pins (BCM 18, 19, 21 for BCLK, LRCLK, DATA).

### 3. Install dependencies

```
sudo apt update
sudo apt install pip git
sudo apt install i2c-tools  # optional but useful

# Avoid having to use a venv
sudo rm /usr/lib/python3.13/EXTERNALLY-MANAGED

sudo apt install python3-numpy
sudo apt install libportaudio2

git clone https://github.com/iuliux/drum-machine-rpizero.git
cd drum-machine-rpizero/
sudo pip install -r requirements.txt --break-system-packages
```

### 4. Run (and update) on startup

```
chmod +x /home/pi/drum-machine-rpizero/scripts/setup-service.sh
sudo sh /home/pi/drum-machine-rpizero/scripts/setup-service.sh
```

### 5. System optimizations

```
# Disable Bluetooth
sudo systemctl disable bluetooth
sudo systemctl stop bluetooth

# More to disable (to maybe improve boot time)
sudo systemctl disable ModemManager

# Disable all cloud-init services
sudo touch /etc/cloud/cloud-init.disabled

# Reduce swappiness
sudo sysctl vm.swappiness=10

# CPU performance mode
echo performance | sudo tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor
```

## Utils

### Check I2C devices:

```
sudo i2cdetect -y 1
```

### Stop the service loaded at boot:

```
sudo systemctl stop drummachine
```

### Manual run over SSH:

```
cd /home/pi/drum-machine-rpizero
sudo python drummachine.py
```
