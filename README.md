## Configure I2S

Add line to `/boot/firmware/config.txt`:

```
dtoverlay=hifiberry-dac
```

This configures the I2S pins (BCM 18, 19, 21 for BCLK, LRCLK, DATA).

## Install dependencies

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

## Utils

To check I2C devices:

```
sudo i2cdetect -y 1
```

## Run (and update) on startup

```
chmod +x /home/pi/drum-machine-rpizero/scripts/setup-service.sh
sudo sh /home/pi/drum-machine-rpizero/scripts/setup-service.sh
```
