Added to `/boot/firmware/config.txt`:

```
dtoverlay=hifiberry-dac
```

This configures the I2S pins (BCM 18, 19, 21 for BCLK, LRCLK, DATA).


```
sudo apt update
sudo apt install pip git

# Avoid having to use a venv
sudo rm /usr/lib/python3.11/EXTERNALLY-MANAGED

sudo apt install python3-numpy
sudo apt install libportaudio2
sudo pip install -r requirements.txt --break-system-packages
```
