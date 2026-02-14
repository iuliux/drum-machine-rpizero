#!/usr/bin/env python3
"""
Simple 8-step, 3-instrument drum machine sequencer for Raspberry Pi Zero 2 W
"""

import os
import sys
import numpy as np
import sounddevice as sd
import soundfile as sf
import time
import threading
import random
from pathlib import Path

# --- GPIO/Hardware Setup for Systemd Service ---
# Set gpiozero factory before any GPIO imports if running as systemd service
if os.geteuid() == 0:  # Running as root (systemd service)
    try:
        os.environ.setdefault('GPIOZERO_PIN_FACTORY', 'lgpio')
    except Exception as e:
        print(f"Warning: Could not set GPIO pin factory: {e}")

# --- Hardware Import Try/Except Blocks ---
try:
    import board
    import busio
    import adafruit_mpr121
    MPR121_AVAILABLE = True
except (ImportError, NotImplementedError) as e:
    MPR121_AVAILABLE = False
    print(f"Warning: MPR121 libraries not available: {e}")

try:
    import neopixel
    NEOPIXEL_AVAILABLE = True
except (ImportError, NotImplementedError) as e:
    NEOPIXEL_AVAILABLE = False
    print(f"Warning: NeoPixel library not available: {e}")

try:
    from luma.core.interface.serial import i2c
    from luma.oled.device import ssd1306
    from luma.core.render import canvas
    from PIL import Image, ImageDraw, ImageFont
    OLED_AVAILABLE = True
except (ImportError, NotImplementedError) as e:
    OLED_AVAILABLE = False
    print(f"Warning: OLED libraries not available: {e}")

try:
    from gpiozero import RotaryEncoder as GPIOZeroRotaryEncoder, Button as GPIOZeroButton
    GPIOZERO_AVAILABLE = True
except (ImportError, RuntimeError) as e:
    GPIOZERO_AVAILABLE = False
    print(f"Warning: gpiozero not available: {e}")

# Audio configuration
SAMPLE_RATE = 44100
BLOCK_SIZE = 512  # Smaller buffer = lower latency (less lag before snare hits)

# Sequencer configuration
NUM_STEPS = 8
AUDIO_BASE_PATH = Path(__file__).parent / "audio"  # Folder with samples
INSTRUMENT_NAMES = ['kick', 'snare', 'hihat']
NUM_INSTRUMENTS = len(INSTRUMENT_NAMES)

# MPR121 configuration
MPR121_ADDR_1 = 0x5A  # First MPR121 - left half (steps 0-3)
MPR121_ADDR_2 = 0x5B  # Second MPR121 - right half (steps 4-7)
MPR121_IRQ_PIN_1 = 4  # GPIO 4 - IRQ for sensor 1
MPR121_IRQ_PIN_2 = 5  # GPIO 5 - IRQ for sensor 2

# NeoPixel configuration
NEOPIXEL_PIN = board.D12 if NEOPIXEL_AVAILABLE else None  # GPIO 12 (PWM0 - alternative)
NUM_PIXELS = 24  # 8 LEDs per instrument × 3 instruments
PIXEL_BRIGHTNESS = 0.3  # 0.0 to 1.0

# Step Indicator Strip configuration
STEP_INDICATOR_PIN = board.D13 if NEOPIXEL_AVAILABLE else None  # GPIO 13 (PWM1)
NUM_STEP_LEDS = 15  # Physical LEDs on strip (using every other one = 8 steps)
STEP_BRIGHTNESS = 0.5  # 0.0 to 1.0

# Rotary Encoder configuration
ENCODER_CLK_PIN = 17  # GPIO 17 for CLK (A pin)
ENCODER_DT_PIN = 27   # GPIO 27 for DT (B pin)
ENCODER_SW_PIN = 22   # GPIO 22 for switch (optional)

# OLED configuration
OLED_I2C_ADDR = 0x3C  # I2C address for OLED display
OLED_WIDTH = 128
OLED_HEIGHT = 64

# Color scheme for instruments
COLORS = {
    'kick': (255, 0, 0),      # Red
    'snare': (0, 255, 0),     # Green
    'hihat': (0, 100, 255),   # Blue
    'current': (255, 255, 255)  # White for current step indicator
}

# --- Classes ---

class DrumSample:
    """Represents a single drum sample"""
    def __init__(self, filepath):
        self.filepath = filepath
        self.data = None
        self.load()
    
    def load(self):
        """Load WAV file into numpy array"""
        try:
            data, samplerate = sf.read(self.filepath, dtype='float32')
            
            # Resample if needed (basic approach)
            if samplerate != SAMPLE_RATE:
                print(f"Warning: {self.filepath.name} is {samplerate}Hz, expected {SAMPLE_RATE}Hz")
                # Simple resampling - for production use scipy.signal.resample
                ratio = SAMPLE_RATE / samplerate
                new_length = int(len(data) * ratio)
                data = np.interp(
                    np.linspace(0, len(data), new_length),
                    np.arange(len(data)),
                    data if data.ndim == 1 else data[:, 0]
                )
            
            # Convert stereo to mono if needed
            if data.ndim == 2:
                data = np.mean(data, axis=1)

            # Apply fade-out at the end to prevent clicks (last 5ms)
            fade_samples = int(0.005 * SAMPLE_RATE)  # 5ms fade
            if len(data) > fade_samples:
                fade_curve = np.linspace(1.0, 0.0, fade_samples)
                data[-fade_samples:] *= fade_curve
            
            self.data = data.astype(np.float32)
            print(f"Loaded: {self.filepath.name} ({len(self.data)} samples)")
            
        except Exception as e:
            print(f"Error loading {self.filepath}: {e}")
            # Create silent fallback
            self.data = np.zeros(1000, dtype=np.float32)


class RotaryEncoder:
    """
    Handles quadrature rotary encoder using gpiozero for reliable detection.
    Features:
    - Rotate: Changes value based on current mode
    - Short Press: Toggle Play/Stop
    - Long Press: Cycle Modes (BPM -> VOL -> FX)
    """
    def __init__(self, sequencer, clk_pin=ENCODER_CLK_PIN, dt_pin=ENCODER_DT_PIN, sw_pin=ENCODER_SW_PIN):
        self.sequencer = sequencer
        self.clk_pin = clk_pin
        self.dt_pin = dt_pin
        self.sw_pin = sw_pin
        
        self.bpm_step = 5  # BPM change per detent
        self.encoder = None
        
        # Button state for long press detection
        self.button_pressed = False
        self.button_press_time = 0
        self.long_press_triggered = False
        self.LONG_PRESS_THRESHOLD = 0.6  # seconds
        
        if not GPIOZERO_AVAILABLE:
            print("Rotary encoder disabled - gpiozero not available")
            return
        
        try:
            # Create rotary encoder with debouncing (critical for reliability)
            # bounce_time helps filter out jitter from mechanical bouncing
            self.encoder = GPIOZeroRotaryEncoder(
                a=clk_pin,
                b=dt_pin,
                bounce_time=0.01  # 10ms debounce window
            )
            
            # Set up rotation event handlers
            self.encoder.when_rotated_clockwise = self._on_rotate_clockwise
            self.encoder.when_rotated_counter_clockwise = self._on_rotate_counter_clockwise
            
            # Set up button if available
            if self.sw_pin:
                from gpiozero import Button as GPIOZeroButton
                self.button = GPIOZeroButton(
                    sw_pin,
                    bounce_time=0.02,
                    hold_time=self.LONG_PRESS_THRESHOLD
                )
                self.button.when_pressed = self._button_pressed
                self.button.when_released = self._button_released
                self.button.when_held = self._button_held
            
            print(f"Rotary encoder initialized on GPIO {clk_pin}/{dt_pin} (gpiozero mode)")
            
        except Exception as e:
            print(f"Error initializing rotary encoder: {e}")
            self.encoder = None
    
    def _on_rotate_clockwise(self):
        """Handle clockwise rotation"""
        self._handle_rotation(1)
    
    def _on_rotate_counter_clockwise(self):
        """Handle counter-clockwise rotation"""
        self._handle_rotation(-1)
    
    def _handle_rotation(self, direction):
        """Process rotation in a given direction"""
        try:
            if self.sequencer.mode == 'BPM':
                new_bpm = self.sequencer.bpm + direction * self.bpm_step
                self.sequencer.set_bpm(new_bpm)
            elif self.sequencer.mode == 'VOL':
                # Change volume by 5%
                new_vol = round(self.sequencer.volume + (direction * 0.05), 2)
                self.sequencer.set_volume(new_vol)
            elif self.sequencer.mode == 'DIST':
                # Control distortion amount
                self.sequencer.distortion = max(0.0, self.sequencer.distortion + direction * 0.05)
                self.sequencer.set_distortion(self.sequencer.distortion)
                print(f"Distortion: {self.sequencer.distortion:.2f}")
        except Exception as e:
            print(f"Error handling rotation: {e}")
    
    def _button_pressed(self):
        """Called when button is initially pressed"""
        self.button_pressed = True
        self.long_press_triggered = False
    
    def _button_released(self):
        """Called when button is released"""
        self.button_pressed = False
        # If we released and haven't triggered long press yet, it's a short press
        if not self.long_press_triggered:
            # Short Press: Toggle Play/Stop
            if self.sequencer.is_playing:
                self.sequencer.stop()
            else:
                self.sequencer.start()
    
    def _button_held(self):
        """Called when button has been held for hold_time"""
        if not self.long_press_triggered:
            self.long_press_triggered = True
            # Switch modes
            self.sequencer.cycle_mode()
            print(f"Mode switched to: {self.sequencer.mode}")
    
    def cleanup(self):
        """Clean up GPIO resources"""
        if GPIOZERO_AVAILABLE and self.encoder is not None:
            try:
                self.encoder.close()
                if hasattr(self, 'button'):
                    self.button.close()
            except:
                pass


class StepIndicatorHandler:
    """Handles WS2812B step indicator strip (edge lighting)"""
    def __init__(self, sequencer):
        self.sequencer = sequencer
        self.pixels = None
        self.last_step = -1  # Track last step to avoid unnecessary updates
        
        if not NEOPIXEL_AVAILABLE:
            print("Step indicator disabled - NeoPixel library not available")
            return
        
        try:
            # Initialize step indicator strip
            self.pixels = neopixel.NeoPixel(
                STEP_INDICATOR_PIN,
                NUM_STEP_LEDS,
                brightness=STEP_BRIGHTNESS,
                auto_write=False,
                pixel_order=neopixel.GRB
            )
            
            # Clear all pixels
            self.pixels.fill((0, 0, 0))
            self.pixels.show()
            
            print(f"Step indicator initialized: {NUM_STEP_LEDS} LEDs on pin {STEP_INDICATOR_PIN}")
            
        except Exception as e:
            print(f"Error initializing step indicator: {e}")
            self.pixels = None
    
    def update(self):
        """Update step indicator - only when step changes"""
        if self.pixels is None:
            return
        
        # Display the step that was just triggered (current_step has already advanced)
        display_step = (self.sequencer.current_step - 1) % NUM_STEPS
        
        # Only update if step changed
        if display_step == self.last_step:
            return
        
        self.last_step = display_step
        
        try:
            # Clear all LEDs
            self.pixels.fill((0, 0, 0))
            
            # Light up current step (every other LED: 0, 2, 4, 6, 8, 10, 12, 14)
            if self.sequencer.is_playing:
                led_index = display_step * 2  # Map step 0-7 to LED 0,2,4,6,8,10,12,14
                self.pixels[led_index] = (255, 255, 255)  # White
            
            self.pixels.show()
            
        except Exception as e:
            print(f"Error updating step indicator: {e}")


class LEDHandler:
    """Handles WS2812B NeoPixel LED strip display"""
    def __init__(self, sequencer):
        self.sequencer = sequencer
        self.pixels = None
        
        if not NEOPIXEL_AVAILABLE:
            print("LEDs disabled - NeoPixel library not available")
            return
        
        try:
            # Initialize NeoPixel strip
            self.pixels = neopixel.NeoPixel(
                NEOPIXEL_PIN,
                NUM_PIXELS,
                brightness=PIXEL_BRIGHTNESS,
                auto_write=False,
                pixel_order=neopixel.GRB
            )
            
            # Clear all pixels
            self.pixels.fill((0, 0, 0))
            self.pixels.show()
            
            print(f"NeoPixel strip initialized: {NUM_PIXELS} LEDs on pin {NEOPIXEL_PIN}")
            
        except Exception as e:
            print(f"Error initializing NeoPixels: {e}")
    
    def get_pixel_index(self, instrument_idx, step_idx):
        """
        Map instrument and step to LED index.
        Physical layout (daisy-chained serpentine):
        - Row 0 (Kick):   LEDs 0-7   (left to right)
        - Row 1 (Snare):  LEDs 15-8  (right to left, reversed)
        - Row 2 (Hihat):  LEDs 16-23 (left to right)
        """
        if instrument_idx == 0:  # Kick: left to right
            return step_idx
        elif instrument_idx == 1:  # Snare: right to left (reversed)
            return 15 - step_idx
        elif instrument_idx == 2:  # Hihat: left to right
            return 16 + step_idx
        else:
            return 0  # Fallback
    
    def update(self):
        """Update LED strip to reflect current pattern and step"""
        if self.pixels is None:
            return
        
        try:
            # Display the step that was just triggered (current_step has already advanced)
            display_step = (self.sequencer.current_step - 1) % NUM_STEPS
            
            for inst_idx, instrument_name in enumerate(INSTRUMENT_NAMES):
                base_color = COLORS[instrument_name]
                
                for step_idx in range(NUM_STEPS):
                    pixel_idx = self.get_pixel_index(inst_idx, step_idx)
                    
                    # Check if this step is active in the pattern
                    is_active = self.sequencer.pattern[inst_idx, step_idx]
                    
                    # Check if this is the current playing step
                    is_current = (step_idx == display_step) and self.sequencer.is_playing
                    
                    if is_current and is_active:
                        # Current step that's active: full brightness white
                        self.pixels[pixel_idx] = COLORS['current']
                    elif is_current:
                        # Current step but inactive: dim white
                        self.pixels[pixel_idx] = tuple(min(255, c + 100) for c in COLORS['current'])
                    elif is_active:
                        # Active step: instrument color at medium brightness
                        self.pixels[pixel_idx] = base_color
                    else:
                        # Inactive step: completely off
                        self.pixels[pixel_idx] = (0, 0, 0)
            
            self.pixels.show()
            
        except Exception as e:
            print(f"Error updating LEDs: {e}")


class OLEDHandler:
    """Handles OLED display with optimized drawing and mode support"""
    def __init__(self, sequencer):
        self.sequencer = sequencer
        self.device = None
        self.font_large = None
        self.font_small = None
        self.update_counter = 0
        
        # Track state to avoid redraws if nothing changed
        self.last_bpm = -1
        self.last_mode = ""
        self.last_vol = -1
        self.last_dist = -1

        self.MODES = {
            'BPM': 'TEMPO',
            'VOL': 'VOLUME',
            'DIST': 'DISTORTION',
            'BANK': 'SAMPLES'
        }
        
        # Pre-allocate image buffers (Optimization)
        self.image = Image.new('1', (OLED_WIDTH, OLED_HEIGHT))
        self.draw = ImageDraw.Draw(self.image)
        
        # --- Bitmaps ---
        # Load Metronome icon from file (34x34)
        try:
            icon_path = Path(__file__).parent / "icons" / "metronome.bmp"
            self.icon_metro = Image.open(icon_path).convert('1')
        except Exception as e:
            print(f"Warning: Could not load metronome.bmp: {e}. Creating fallback.")
            self.icon_metro = Image.new('1', (34, 34), 0)

        # Load Speaker icon from file (34x34)
        try:
            icon_path = Path(__file__).parent / "icons" / "speaker.bmp"
            self.icon_vol = Image.open(icon_path).convert('1')
        except Exception as e:
            print(f"Warning: Could not load speaker.bmp: {e}. Creating fallback.")
            self.icon_vol = Image.new('1', (34, 34), 0)

        # Load FX Icon (Gear) from file (34x34)
        try:
            icon_path = Path(__file__).parent / "icons" / "distortion.bmp"
            self.icon_fx = Image.open(icon_path).convert('1')
        except Exception as e:
            print(f"Warning: Could not load distortion.bmp: {e}. Creating fallback.")
            self.icon_fx = Image.new('1', (34, 34), 0)
        
        
        if not OLED_AVAILABLE:
            print("OLED disabled - libraries not available")
            return
        
        try:
            # Initialize I2C and OLED device
            serial = i2c(port=1, address=OLED_I2C_ADDR)
            self.device = ssd1306(serial, width=OLED_WIDTH, height=OLED_HEIGHT)
            
            # Try to load fonts - aim for bold, chunky look like Arduino u8g2
            try:
                # Try FreeSans Bold first (similar to logisoso)
                self.font_large = ImageFont.truetype("./fonts/SpaceMono-Bold.ttf", 32)
            except:
                try:
                    # Fallback to DejaVu Sans Bold
                    self.font_large = ImageFont.truetype("/usr/share/fonts/truetype/freefont/FreeSansBold.ttf", 32)
                except:
                    self.font_large = ImageFont.load_default()
            
            try:
                # Small font - try to get something condensed and bold
                self.font_small = ImageFont.truetype("./fonts/LeagueSpartan-Bold.ttf", 12)
            except:
                try:
                    self.font_small = ImageFont.truetype("/usr/share/fonts/truetype/freefont/FreeSansBold.ttf", 11)
                except:
                    self.font_small = ImageFont.load_default()
            
            # Clear display
            self.device.clear()
        except Exception as e:
            print(f"Error initializing OLED: {e}")
            self.device = None

    def _create_bitmap(self, data, w, h):
        img = Image.new('1', (w, h))
        pixels = []
        bytes_per_row = (w + 7) // 8
        for row in range(h):
            for col in range(w):
                byte_idx = row * bytes_per_row + col // 8
                bit_idx = col % 8
                if byte_idx < len(data):
                    val = (data[byte_idx] >> bit_idx) & 1
                    pixels.append(255 if val else 0)
                else: pixels.append(0)
        img.putdata(pixels)
        return img
    
    def update(self):
        """Update OLED display with current BPM and status"""
        if self.device is None:
            return
        
        # Update logic: Redraw if mode, bpm, volume, or distortion changes, OR every 20th frame (heartbeat)
        state_changed = (self.sequencer.bpm != self.last_bpm or 
                         self.sequencer.mode != self.last_mode or
                         self.sequencer.volume != self.last_vol or
                         self.sequencer.distortion != self.last_dist)
        
        self.update_counter += 1
        if not state_changed and self.update_counter < 20:
            return
        
        self.update_counter = 0
        self.last_bpm = self.sequencer.bpm
        self.last_mode = self.sequencer.mode
        self.last_vol = self.sequencer.volume
        self.last_dist = self.sequencer.distortion
        
        try:
            # Clear existing image buffer
            self.draw.rectangle((0, 0, OLED_WIDTH, OLED_HEIGHT), fill=0)
            
            # Define constants for layout
            value_xoffset = 50
            
            # Skip drawing if sequencer is paused - just show mode and a pause icon
            if not self.sequencer.is_playing:
                # Draw a large pause icon (two vertical bars) in the center
                self.draw.rectangle((value_xoffset, 10, value_xoffset + 10, 40), fill=255)
                self.draw.rectangle((value_xoffset, 50, value_xoffset + 10, 80), fill=255)
                # Skip the rest of the drawing to save resources when paused
                self.device.display(self.image)
                return

            self.draw.text((5, 50), f"{self.MODES[self.sequencer.mode]}", font=self.font_small, fill=255)

            # Mode Specifics
            if self.sequencer.mode == 'BPM':
                self.image.paste(self.icon_metro, (5, 6))
                self.draw.text((value_xoffset, 0), str(self.sequencer.bpm), font=self.font_large, fill=255)
            
            elif self.sequencer.mode == 'VOL':
                self.image.paste(self.icon_vol, (5, 6))
                vol_percent = int(self.sequencer.volume * 100)
                vol_text = f"{vol_percent}%"
                # Draw text with anchor='ra' (right-aligned)
                self.draw.text((120, 0), vol_text, font=self.font_large, fill=255, anchor='ra')
                # Draw Volume Bar
                self.draw.rectangle((value_xoffset, 40, 120, 44), outline=1)
                fill_width = int(58 * self.sequencer.volume)
                self.draw.rectangle((value_xoffset, 40, value_xoffset + fill_width, 44), fill=1)

            elif self.sequencer.mode == 'DIST':
                self.image.paste(self.icon_fx, (5, 6))
                dist_percent = int(self.sequencer.distortion * 100)
                dist_text = f"{dist_percent}%"
                # Draw text with anchor='ra' (right-aligned)
                self.draw.text((120, 0), dist_text, font=self.font_large, fill=255, anchor='ra')
                # Draw Distortion Bar
                self.draw.rectangle((value_xoffset, 40, 120, 44), outline=1)
                fill_width = int(58 * self.sequencer.distortion)
                self.draw.rectangle((value_xoffset, 40, value_xoffset + fill_width, 44), fill=1)

            self.device.display(self.image)
            
        except Exception as e:
            print(f"Error updating OLED: {e}")


class TouchHandler:
    """Handles MPR121 capacitive touch sensors with gpiozero IRQ monitoring"""
    def __init__(self, sequencer, use_irq=True):
        self.sequencer = sequencer
        self.mpr121_1 = None
        self.mpr121_2 = None
        self.last_touched_1 = 0
        self.last_touched_2 = 0
        self.use_irq = use_irq and GPIOZERO_AVAILABLE
        self.irq_button_1 = None
        self.irq_button_2 = None
        
        # Watchdog for detecting stuck sensors
        self.last_irq_time_1 = time.time()
        self.last_irq_time_2 = time.time()
        self.irq_timeout = 5.0  # Reset sensor if no IRQ for 5 seconds
        
        if not MPR121_AVAILABLE:
            print("Touch sensors disabled - MPR121 library not available")
            return
        
        try:
            time.sleep(0.1)  # Small delay before I2C operations
            # Initialize I2C
            i2c_bus = busio.I2C(board.SCL, board.SDA, frequency=100000)
            time.sleep(0.05)  # Wait for I2C bus
            
            # Initialize MPR121 sensor 1 independently
            try:
                self.mpr121_1 = adafruit_mpr121.MPR121(i2c_bus, address=MPR121_ADDR_1)
                print(f"MPR121 sensor 1 initialized at 0x{MPR121_ADDR_1:02X}")
                
                # Lock baseline to prevent multi-touch stuck states (from https://crimier.github.io/posts/mpr121_funk/)
                time.sleep(0.2)  # Let baseline establish
                i2c_bus.writeto(MPR121_ADDR_1, bytes([0x5E, 0x00]))  # Disable sensor
                time.sleep(0.1)
                i2c_bus.writeto(MPR121_ADDR_1, bytes([0x5E, 0b01001111]))  # Enable with baseline tracking disabled
                time.sleep(0.1)
                print("Sensor 1 baseline locked")
                
                # Set up IRQ for sensor 1 if available
                if self.use_irq:
                    self.irq_button_1 = GPIOZeroButton(
                        MPR121_IRQ_PIN_1,
                        pull_up=True,
                        bounce_time=0.01
                    )
                    self.irq_button_1.when_pressed = self._on_irq_triggered_1
                    print(f"MPR121 IRQ mode enabled for sensor 1: GPIO {MPR121_IRQ_PIN_1}")
            except Exception as e:
                print(f"Warning: MPR121 sensor 1 (0x{MPR121_ADDR_1:02X}) not available: {e}")
                self.mpr121_1 = None
            
            time.sleep(0.02)  # Delay between sensor initializations
            
            # Initialize MPR121 sensor 2 independently
            try:
                self.mpr121_2 = adafruit_mpr121.MPR121(i2c_bus, address=MPR121_ADDR_2)
                print(f"MPR121 sensor 2 initialized at 0x{MPR121_ADDR_2:02X}")
                
                # Lock baseline to prevent multi-touch stuck states (from https://crimier.github.io/posts/mpr121_funk/)
                time.sleep(0.2)  # Let baseline establish
                i2c_bus.writeto(MPR121_ADDR_2, bytes([0x5E, 0x00]))  # Disable sensor
                time.sleep(0.1)
                i2c_bus.writeto(MPR121_ADDR_2, bytes([0x5E, 0b01001111]))  # Enable with baseline tracking disabled
                time.sleep(0.1)
                print("Sensor 2 baseline locked")
                
                # Set up IRQ for sensor 2 if available
                if self.use_irq:
                    self.irq_button_2 = GPIOZeroButton(
                        MPR121_IRQ_PIN_2,
                        pull_up=True,
                        bounce_time=0.01
                    )
                    self.irq_button_2.when_pressed = self._on_irq_triggered_2
                    print(f"MPR121 IRQ mode enabled for sensor 2: GPIO {MPR121_IRQ_PIN_2}")
            except Exception as e:
                print(f"Warning: MPR121 sensor 2 (0x{MPR121_ADDR_2:02X}) not available: {e}")
                self.mpr121_2 = None
            
            # Check if at least one sensor was initialized
            if self.mpr121_1 is None and self.mpr121_2 is None:
                print("Error: No MPR121 sensors available")
            
        except Exception as e:
            print(f"Error initializing MPR121 I2C bus: {e}")
            self.mpr121_1 = None
            self.mpr121_2 = None
    
    def _on_irq_triggered_1(self):
        """Called when sensor 1 IRQ pin goes LOW"""
        self.last_irq_time_1 = time.time()
        print("IRQ triggered for touch sensor 1")
        self._process_sensor(1)
    
    def _on_irq_triggered_2(self):
        """Called when sensor 2 IRQ pin goes LOW"""
        self.last_irq_time_2 = time.time()
        print("IRQ triggered for touch sensor 2")
        self._process_sensor(2)
    
    def _process_sensor(self, sensor_num):
        """Process touch events for a specific sensor"""
        try:
            if sensor_num == 1 and self.mpr121_1:
                touched = self.mpr121_1.touched()
                new_touches = touched & ~self.last_touched_1
                
                for i in range(12):
                    if new_touches & (1 << i):
                        instrument, step = self.map_touch_to_pattern(1, i)
                        self.sequencer.toggle_step(instrument, step)
                
                self.last_touched_1 = touched
                
            elif sensor_num == 2 and self.mpr121_2:
                touched = self.mpr121_2.touched()
                new_touches = touched & ~self.last_touched_2
                
                for i in range(12):
                    if new_touches & (1 << i):
                        instrument, step = self.map_touch_to_pattern(2, i)
                        self.sequencer.toggle_step(instrument, step)
                
                self.last_touched_2 = touched
                
        except Exception as e:
            print(f"Error in touch callback: {e}")
    
    def map_touch_to_pattern(self, sensor_num, pad_num):
        """
        Map touch sensor and pad number to instrument and step.
        
        Layout (Left/Right Split):
        Sensor 2 (0x5B) - Left Half (steps 0-3):
          Pads 0-3: Kick steps 0-3
          Pads 4-7: Snare steps 0-3
          Pads 8-11: Hihat steps 0-3
        Sensor 1 (0x5A) - Right Half (steps 4-7):
          Pads 0-3: Kick steps 4-7
          Pads 4-7: Snare steps 4-7
          Pads 8-11: Hihat steps 4-7
        """
        # Determine instrument from pad number (same for both sensors)
        if pad_num < 4:
            instrument = 0  # Kick
            pad_step = pad_num
        elif pad_num < 8:
            instrument = 1  # Snare
            pad_step = pad_num - 4
        else:  # pad_num < 12
            instrument = 2  # Hihat
            pad_step = pad_num - 8
        
        # Add sensor offset (4 for sensor 1 [right], 0 for sensor 2 [left])
        step_offset = 4 if sensor_num == 1 else 0
        step = pad_step + step_offset
        
        return instrument, step
    
    def poll(self):
        """Poll touch sensors in non-IRQ mode, no-op if using interrupts"""
        if self.use_irq:
            return  # Interrupts handle everything
            
        if self.mpr121_1 is not None:
            self._process_sensor(1)
        if self.mpr121_2 is not None:
            self._process_sensor(2)
    
    def watchdog_check(self):
        """Check if sensors are stuck and attempt recovery"""
        current_time = time.time()
        
        # Check sensor 1
        if self.mpr121_1 is not None:
            if current_time - self.last_irq_time_1 > self.irq_timeout:
                print(f"Warning: Sensor 1 stuck for {self.irq_timeout}s, attempting recovery...")
                try:
                    # Reset sensor 1 state
                    self.last_touched_1 = 0
                    # Read and clear any stuck state
                    _ = self.mpr121_1.touched()
                    self.last_irq_time_1 = current_time
                    print("Sensor 1 recovery attempted")
                except Exception as e:
                    print(f"Error recovering sensor 1: {e}")
        
        # Check sensor 2
        if self.mpr121_2 is not None:
            if current_time - self.last_irq_time_2 > self.irq_timeout:
                print(f"Warning: Sensor 2 stuck for {self.irq_timeout}s, attempting recovery...")
                try:
                    # Reset sensor 2 state
                    self.last_touched_2 = 0
                    # Read and clear any stuck state
                    _ = self.mpr121_2.touched()
                    self.last_irq_time_2 = current_time
                    print("Sensor 2 recovery attempted")
                except Exception as e:
                    print(f"Error recovering sensor 2: {e}")
    
    def cleanup(self):
        """Clean up GPIO resources"""
        if self.use_irq:
            try:
                if self.irq_button_1 is not None:
                    self.irq_button_1.close()
                if self.irq_button_2 is not None:
                    self.irq_button_2.close()
            except:
                pass


class Sequencer:
    """Main sequencer engine"""
    def __init__(self, bpm=120):
        self.bpm = bpm
        self.volume = 0.1  # Default volume 10%
        self.distortion = 0.0  # Distortion amount 0.0-1.0
        self.modes = ['BPM', 'VOL', 'DIST']
        self.mode_idx = 0
        self.mode = self.modes[self.mode_idx]
        
        self.current_step = 0
        self.pattern = np.zeros((NUM_INSTRUMENTS, NUM_STEPS), dtype=bool)
        self.is_playing = False
        self.sample_banks = {}  # Dictionary of lists of samples per instrument
        self.active_voices = []  # List of currently playing samples
        self.lock = threading.Lock()
        
        # Load samples
        self.load_samples()
        
        # Audio stream
        self.stream = None
        
    def load_samples(self):
        """Load drum samples from audio folder structure"""
        if not AUDIO_BASE_PATH.exists():
            print(f"Warning: Audio path {AUDIO_BASE_PATH} does not exist!")
            print("Creating placeholder samples...")
            # Create placeholder samples if no audio folder
            for name in INSTRUMENT_NAMES:
                self.sample_banks[name] = [self._create_placeholder_sample(name)]
            return
        
        for instrument_name in INSTRUMENT_NAMES:
            instrument_path = AUDIO_BASE_PATH / instrument_name
            
            if not instrument_path.exists():
                print(f"Warning: {instrument_path} not found, using placeholder")
                self.sample_banks[instrument_name] = [self._create_placeholder_sample(instrument_name)]
                continue
            
            # Find all .wav files in this instrument folder
            wav_files = list(instrument_path.glob("*.wav"))
            
            if not wav_files:
                print(f"Warning: No .wav files found in {instrument_path}")
                self.sample_banks[instrument_name] = [self._create_placeholder_sample(instrument_name)]
                continue
            
            # Load all samples for this instrument
            samples = []
            for wav_file in wav_files:
                sample = DrumSample(wav_file)
                if sample.data is not None:
                    samples.append(sample)
            
            self.sample_banks[instrument_name] = samples
            print(f"{instrument_name}: loaded {len(samples)} samples")
    
    def _create_placeholder_sample(self, instrument_name):
        """Create a simple beep as placeholder"""
        duration = 0.2
        t = np.linspace(0, duration, int(SAMPLE_RATE * duration))
        freq = {'kick': 60, 'snare': 200, 'hihat': 8000}.get(instrument_name, 440)
        data = (np.sin(2 * np.pi * freq * t) * 0.3 * np.exp(-t * 10)).astype(np.float32)
        class PS: 
            def __init__(self, d): self.data = d
        return PS(data)
    
    def toggle_step(self, instrument_idx, step_idx):
        """Toggle a step on/off for a given instrument"""
        with self.lock:
            self.pattern[instrument_idx, step_idx] = not self.pattern[instrument_idx, step_idx]
            state = "ON" if self.pattern[instrument_idx, step_idx] else "OFF"
            print(f"{INSTRUMENT_NAMES[instrument_idx]} step {step_idx}: {state}")
    
    def set_bpm(self, bpm):
        """Update tempo"""
        self.bpm = max(40, min(300, bpm))  # Clamp between 40-300 BPM
        print(f"BPM: {self.bpm}")
    
    def set_volume(self, vol):
        self.volume = max(0.0, min(1.0, vol))
    
    def set_distortion(self, amount):
        """Set distortion amount (0.0 = none, 1.0 = heavy)"""
        self.distortion = max(0.0, min(1.0, amount))
    
    def cycle_mode(self):
        self.mode_idx = (self.mode_idx + 1) % len(self.modes)
        self.mode = self.modes[self.mode_idx]

    def trigger_samples(self, step):
        """Trigger samples for the current step"""
        with self.lock:
            for inst_idx, instrument_name in enumerate(INSTRUMENT_NAMES):
                if self.pattern[inst_idx, step]:
                    # Randomly select a sample from this instrument's bank
                    selected_sample = random.choice(self.sample_banks[instrument_name])
                    
                    # Add sample to active voices with position counter
                    self.active_voices.append({
                        'data': selected_sample.data.copy(),
                        'position': 0,
                        'length': len(selected_sample.data)
                    })
    
    def audio_callback(self, outdata, frames, time_info, status):
        """Audio callback - mixes all active voices (mono optimized)"""
        if status:
            print(f"Audio status: {status}")
        
        # Start with silence
        outdata.fill(0)
        
        with self.lock:
            voices_to_remove = []
            
            # Mix all active voices directly into mono output buffer
            for i, voice in enumerate(self.active_voices):
                pos = voice['position']
                remaining = voice['length'] - pos
                
                if remaining <= 0:
                    voices_to_remove.append(i)
                    continue
                
                # How many samples to copy this block
                to_copy = min(frames, remaining)
                
                # Mix into mono output (vectorized)
                outdata[:to_copy, 0] += voice['data'][pos:pos + to_copy]
                
                voice['position'] += to_copy
            
            # Remove finished voices (in reverse to maintain indices)
            for i in reversed(voices_to_remove):
                self.active_voices.pop(i)
            
            # Apply Distortion if enabled
            if self.distortion > 0.0:
                # Drive signal by distortion amount
                drive = 1.0 + self.distortion * 9.0  # 1x to 10x drive
                driven = outdata[:, 0] * drive
                # Soft clipping via tanh
                outdata[:, 0] = np.tanh(driven / (self.volume + 1e-6)) * self.volume
            
            # Apply Master Volume
            outdata[:] *= self.volume
    
    def sequencer_thread(self):
        """Main sequencer loop running in separate thread"""
        next_step_time = time.perf_counter()
        step_duration = 60.0 / self.bpm / 2  # 8th notes
        
        while self.is_playing:
            # Trigger samples for current step
            self.trigger_samples(self.current_step)
            
            # Advance step
            self.current_step = (self.current_step + 1) % NUM_STEPS
            
            # Calculate next step time
            next_step_time += step_duration
            
            # Sleep until next step (minimal latency compensation)
            sleep_time = next_step_time - time.perf_counter()
            if sleep_time > 0:
                time.sleep(sleep_time)
            else:
                # We're running late, resync
                next_step_time = time.perf_counter()
            
            # Update step duration if BPM changed
            step_duration = 60.0 / self.bpm / 2
    
    def start(self):
        """Start playback"""
        if self.is_playing:
            return
        
        self.is_playing = True
        self.current_step = 0
        
        # Start audio stream (mono-only for single speaker, more efficient)
        self.stream = sd.OutputStream(
            samplerate=SAMPLE_RATE,
            blocksize=BLOCK_SIZE,
            channels=1,
            dtype=np.float32,
            callback=self.audio_callback
        )
        self.stream.start()
        
        # Start sequencer thread
        self.seq_thread = threading.Thread(target=self.sequencer_thread, daemon=True)
        self.seq_thread.start()
        
        print("Sequencer started")
    
    def stop(self):
        """Stop playback"""
        self.is_playing = False
        
        # Wait for sequencer thread to finish
        if hasattr(self, 'seq_thread') and self.seq_thread.is_alive():
            self.seq_thread.join(timeout=1.0)
        
        # Stop and close audio stream
        if self.stream:
            try:
                if self.stream.active:
                    self.stream.stop()
                self.stream.close()
                self.stream = None
            except Exception as e:
                print(f"Error stopping audio stream: {e}")
        
        print("Sequencer stopped")


def main():
    """Main program entry point"""
    print("Raspberry Pi Drum Machine")
    print("=" * 40)
    
    # Create sequencer
    seq = Sequencer(bpm=120)
    
    # Initialize touch handler (gpiozero handles GPIO setup)
    touch = TouchHandler(seq, use_irq=False)
    
    # Initialize OLED display
    oled = OLEDHandler(seq)

    # Initialize LED handler
    leds = LEDHandler(seq)
    step_indicator = StepIndicatorHandler(seq)
    
    # Initialize rotary encoder (gpiozero handles its own GPIO setup)
    encoder = RotaryEncoder(seq)
    
    # Set up a simple test pattern (for testing without touch sensors)
    # Kick on steps 0, 4
    seq.toggle_step(0, 0)
    seq.toggle_step(0, 4)
    # Snare on steps 2, 6
    seq.toggle_step(1, 2)
    seq.toggle_step(1, 6)
    # Hi-hat on all steps
    # for i in range(8):
    #     seq.toggle_step(2, i)
    
    # Start playback
    seq.start()
    
    print("\nSequencer running.")
    print("- LEDs: Red=Kick, Green=Snare, Blue=Hihat, White=Current step")
    print("Press Ctrl+C to stop.\n")
    
    # LED update thread (separate from main loop to avoid blocking touch/audio)
    led_stop_event = threading.Event()
    
    def led_update_thread():
        """Update LEDs in separate thread (non-blocking)"""
        while not led_stop_event.is_set():
            leds.update()
            step_indicator.update()
            time.sleep(0.01)  # 100 FPS for LEDs
    
    led_thread = threading.Thread(target=led_update_thread, daemon=True)
    led_thread.start()
    
    # Main loop - touch polling and OLED updates only (audio thread handles sequencing)
    try:
        while True:
            oled.update()  # OLED less critical, can take a few ms
            touch.poll()  # Touch input is responsive
            time.sleep(0.01)  # Keep main loop responsive
    except KeyboardInterrupt:
        print("\nStopping...")
    except Exception as e:
        print(f"\nError in main loop: {e}")
    finally:
        led_stop_event.set()
        led_thread.join(timeout=1.0)
        seq.stop()
        encoder.cleanup()
        touch.cleanup()
        if leds.pixels: leds.pixels.fill((0,0,0)); leds.pixels.show()
        if step_indicator.pixels: step_indicator.pixels.fill((0,0,0)); step_indicator.pixels.show()

if __name__ == "__main__":
    main()
