#!/usr/bin/env python3
"""
Simple 8-step, 3-instrument drum machine sequencer for Raspberry Pi Zero 2 W
"""

import numpy as np
import sounddevice as sd
import soundfile as sf
import time
import threading
import random
from pathlib import Path
try:
    import board
    import busio
    import adafruit_mpr121
    MPR121_AVAILABLE = True
except (ImportError, NotImplementedError):
    MPR121_AVAILABLE = False
    print("Warning: MPR121 libraries not available, running without touch sensors")

try:
    import neopixel
    NEOPIXEL_AVAILABLE = True
except (ImportError, NotImplementedError):
    NEOPIXEL_AVAILABLE = False
    print("Warning: NeoPixel library not available, running without LEDs")

try:
    from luma.core.interface.serial import i2c
    from luma.oled.device import ssd1306
    from luma.core.render import canvas
    from PIL import Image, ImageDraw, ImageFont
    OLED_AVAILABLE = True
except (ImportError, NotImplementedError):
    OLED_AVAILABLE = False
    print("Warning: OLED libraries not available, running without display")

try:
    import lgpio
    LGPIO_AVAILABLE = True
except (ImportError, RuntimeError):
    LGPIO_AVAILABLE = False
    print("Warning: lgpio not available, running without rotary encoder")

# Audio configuration
SAMPLE_RATE = 44100
BLOCK_SIZE = 512  # Buffer size for low latency

# Sequencer configuration
NUM_STEPS = 8
AUDIO_BASE_PATH = Path(__file__).parent / "audio"  # Folder with samples
INSTRUMENT_NAMES = ['kick', 'snare', 'hihat']
NUM_INSTRUMENTS = len(INSTRUMENT_NAMES)

# MPR121 configuration
MPR121_ADDR_1 = 0x5A  # First MPR121 - handles kick (8 pads) + snare (4 pads)
MPR121_ADDR_2 = 0x5B  # Second MPR121 - handles snare (4 pads) + hihat (8 pads)
MPR121_IRQ_PIN = 4    # GPIO 4 - shared IRQ for both MPR121s (optional but recommended)

# NeoPixel configuration
NEOPIXEL_PIN = board.D12 if NEOPIXEL_AVAILABLE else None  # GPIO 12 (PWM0 - alternative)
NUM_PIXELS = 24  # 8 LEDs per instrument × 3 instruments
PIXEL_BRIGHTNESS = 0.3  # 0.0 to 1.0

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
            
            self.data = data.astype(np.float32)
            print(f"Loaded: {self.filepath.name} ({len(self.data)} samples)")
            
        except Exception as e:
            print(f"Error loading {self.filepath}: {e}")
            # Create silent fallback
            self.data = np.zeros(1000, dtype=np.float32)


class RotaryEncoder:
    """Handles quadrature rotary encoder for BPM control"""
    def __init__(self, sequencer, gpio_handle, clk_pin=ENCODER_CLK_PIN, dt_pin=ENCODER_DT_PIN, sw_pin=ENCODER_SW_PIN):
        self.sequencer = sequencer
        self.gpio_handle = gpio_handle
        self.clk_pin = clk_pin
        self.dt_pin = dt_pin
        self.sw_pin = sw_pin
        
        self.clk_last_state = None
        self.bpm_step = 1  # BPM change per detent
        self.callback_id_clk = None
        self.callback_id_sw = None
        
        if not LGPIO_AVAILABLE or gpio_handle is None:
            print("Rotary encoder disabled - lgpio not available")
            return
        
        try:
            # Set up GPIO pins
            lgpio.gpio_claim_input(gpio_handle, self.clk_pin, lgpio.SET_PULL_UP)
            lgpio.gpio_claim_input(gpio_handle, self.dt_pin, lgpio.SET_PULL_UP)
            
            # Store initial state
            self.clk_last_state = lgpio.gpio_read(gpio_handle, self.clk_pin)
            
            # Set up alert (interrupt) on CLK pin for both edges
            lgpio.gpio_claim_alert(gpio_handle, self.clk_pin, lgpio.BOTH_EDGES)
            self.callback_id_clk = lgpio.callback(gpio_handle, self.clk_pin, lgpio.BOTH_EDGES, self._rotary_callback)
            
            if self.sw_pin:
                lgpio.gpio_claim_input(gpio_handle, self.sw_pin, lgpio.SET_PULL_UP)
                # Set up alert on button for falling edge
                lgpio.gpio_claim_alert(gpio_handle, self.sw_pin, lgpio.FALLING_EDGE)
                self.callback_id_sw = lgpio.callback(gpio_handle, self.sw_pin, lgpio.FALLING_EDGE, self._button_callback)
            
            print(f"Rotary encoder initialized on GPIO {clk_pin}/{dt_pin} (interrupt mode)")
            
        except Exception as e:
            print(f"Error initializing rotary encoder: {e}")
    
    def _rotary_callback(self, chip, gpio, level, tick):
        """Interrupt callback for rotary encoder rotation"""
        try:
            dt_state = lgpio.gpio_read(self.gpio_handle, self.dt_pin)
            
            # Only process on rising edge of CLK
            if level == 1 and self.clk_last_state == 0:
                if dt_state != level:
                    # Clockwise rotation - increase BPM
                    new_bpm = self.sequencer.bpm + self.bpm_step
                    self.sequencer.set_bpm(new_bpm)
                else:
                    # Counter-clockwise rotation - decrease BPM
                    new_bpm = self.sequencer.bpm - self.bpm_step
                    self.sequencer.set_bpm(new_bpm)
            
            self.clk_last_state = level
            
        except Exception as e:
            print(f"Error in rotary callback: {e}")
    
    def _button_callback(self, chip, gpio, level, tick):
        """Interrupt callback for encoder button press"""
        try:
            # Toggle play/stop on button press
            if self.sequencer.is_playing:
                print("Button: Stop")
                self.sequencer.stop()
            else:
                print("Button: Start")
                self.sequencer.start()
        except Exception as e:
            print(f"Error in button callback: {e}")
    
    def poll(self):
        """No-op in interrupt mode"""
        pass
    
    def cleanup(self):
        """Clean up GPIO resources"""
        if LGPIO_AVAILABLE and self.gpio_handle is not None:
            try:
                # Cancel callbacks
                if self.callback_id_clk is not None:
                    lgpio.callback_cancel(self.callback_id_clk)
                if self.callback_id_sw is not None:
                    lgpio.callback_cancel(self.callback_id_sw)
                
                # Free GPIO pins
                lgpio.gpio_free(self.gpio_handle, self.clk_pin)
                lgpio.gpio_free(self.gpio_handle, self.dt_pin)
                if self.sw_pin:
                    lgpio.gpio_free(self.gpio_handle, self.sw_pin)
            except:
                pass


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
            self.pixels = None
    
    def get_pixel_index(self, instrument_idx, step_idx):
        """
        Map instrument and step to LED index.
        Layout: [Kick 0-7][Snare 0-7][Hihat 0-7]
        """
        return instrument_idx * NUM_STEPS + step_idx
    
    def update(self):
        """Update LED strip to reflect current pattern and step"""
        if self.pixels is None:
            return
        
        try:
            current_step = self.sequencer.current_step
            
            for inst_idx, instrument_name in enumerate(INSTRUMENT_NAMES):
                base_color = COLORS[instrument_name]
                
                for step_idx in range(NUM_STEPS):
                    pixel_idx = self.get_pixel_index(inst_idx, step_idx)
                    
                    # Check if this step is active in the pattern
                    is_active = self.sequencer.pattern[inst_idx, step_idx]
                    
                    # Check if this is the current playing step
                    is_current = (step_idx == current_step) and self.sequencer.is_playing
                    
                    if is_current and is_active:
                        # Current step that's active: full brightness white
                        self.pixels[pixel_idx] = COLORS['current']
                    elif is_current:
                        # Current step but inactive: dim white
                        self.pixels[pixel_idx] = (50, 50, 50)
                    elif is_active:
                        # Active step: instrument color at medium brightness
                        self.pixels[pixel_idx] = base_color
                    else:
                        # Inactive step: very dim instrument color
                        dim_factor = 0.05
                        self.pixels[pixel_idx] = tuple(int(c * dim_factor) for c in base_color)
            
            self.pixels.show()
            
        except Exception as e:
            print(f"Error updating LEDs: {e}")


class OLEDHandler:
    """Handles OLED display for BPM and status"""
    def __init__(self, sequencer):
        self.sequencer = sequencer
        self.device = None
        self.font_large = None
        self.font_small = None
        
        # Metronome icon (34x34px) - converted from your Arduino bitmap
        self.metronome_icon = Image.new('1', (34, 34))
        icon_data = [
            0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x03, 0x00, 0x00, 0x00,
            0x00, 0x03, 0x00, 0x00, 0x00, 0x80, 0x07, 0x00, 0x00, 0x00, 0x80, 0x07, 0x00, 0x00, 0x00, 0xc0,
            0x0f, 0x00, 0x00, 0x00, 0xc0, 0x0c, 0x03, 0x00, 0x00, 0xe0, 0x1c, 0x03, 0x00, 0x00, 0x60, 0x18,
            0x03, 0x00, 0x00, 0x70, 0x98, 0x03, 0x00, 0x00, 0x30, 0x80, 0x01, 0x00, 0x00, 0x38, 0xc0, 0x01,
            0x00, 0x00, 0x18, 0xc0, 0x00, 0x00, 0x00, 0x1c, 0xc0, 0x00, 0x00, 0x00, 0x0c, 0xe0, 0x00, 0x00,
            0x00, 0x0c, 0x60, 0x00, 0x00, 0x00, 0x06, 0x70, 0x00, 0x00, 0x00, 0x06, 0x30, 0x00, 0x00, 0x00,
            0x07, 0x30, 0x02, 0x00, 0x00, 0x03, 0x18, 0x02, 0x00, 0x80, 0x03, 0x18, 0x06, 0x00, 0x80, 0x01,
            0x1c, 0x06, 0x00, 0xc0, 0x01, 0x0c, 0x0e, 0x00, 0xc0, 0x00, 0x0e, 0x0c, 0x00, 0xe0, 0x00, 0x06,
            0x1c, 0x00, 0x60, 0x00, 0x06, 0x18, 0x00, 0x60, 0x00, 0x03, 0x18, 0x00, 0x70, 0x00, 0x00, 0x38,
            0x00, 0x70, 0x00, 0x00, 0x38, 0x00, 0xe0, 0xff, 0xff, 0x1f, 0x00, 0xc0, 0xff, 0xff, 0x0f, 0x00,
            0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00
        ]
        
        # Convert bitmap data to image
        pixels = []
        for byte in icon_data:
            for bit in range(8):
                pixels.append(255 if byte & (1 << bit) else 0)
        self.metronome_icon.putdata(pixels[:34*34])
        
        if not OLED_AVAILABLE:
            print("OLED disabled - libraries not available")
            return
        
        try:
            # Initialize I2C and OLED device
            serial = i2c(port=1, address=OLED_I2C_ADDR)
            self.device = ssd1306(serial, width=OLED_WIDTH, height=OLED_HEIGHT)
            
            # Try to load fonts (these are standard PIL fonts)
            try:
                # Large font for BPM number
                self.font_large = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 28)
            except:
                self.font_large = ImageFont.load_default()
            
            try:
                # Small font for text
                self.font_small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 10)
            except:
                self.font_small = ImageFont.load_default()
            
            # Clear display
            self.device.clear()
            
            print(f"OLED display initialized at 0x{OLED_I2C_ADDR:02X}")
            
        except Exception as e:
            print(f"Error initializing OLED: {e}")
            self.device = None
    
    def update(self):
        """Update OLED display with current BPM and status"""
        if self.device is None:
            return
        
        try:
            # Create image for drawing
            image = Image.new('1', (OLED_WIDTH, OLED_HEIGHT))
            draw = ImageDraw.Draw(image)
            
            # Draw metronome icon at top left
            image.paste(self.metronome_icon, (10, 6))
            
            # Draw BPM number (large)
            bpm_text = str(self.sequencer.bpm)
            draw.text((62, 10), bpm_text, font=self.font_large, fill=255)
            
            # Draw status text at bottom
            status_text = "1 bar - 1/8 notes"
            draw.text((10, 52), status_text, font=self.font_small, fill=255)
            
            # Display the image
            self.device.display(image)
            
        except Exception as e:
            print(f"Error updating OLED: {e}")


class TouchHandler:
    """Handles MPR121 capacitive touch sensors"""
    def __init__(self, sequencer, gpio_handle, use_irq=True):
        self.sequencer = sequencer
        self.gpio_handle = gpio_handle
        self.mpr121_1 = None
        self.mpr121_2 = None
        self.last_touched_1 = 0
        self.last_touched_2 = 0
        self.use_irq = use_irq and LGPIO_AVAILABLE and gpio_handle is not None
        self.callback_id = None
        
        if not MPR121_AVAILABLE:
            print("Touch sensors disabled - MPR121 library not available")
            return
        
        try:
            # Initialize I2C
            i2c = busio.I2C(board.SCL, board.SDA)
            
            # Initialize both MPR121 sensors
            self.mpr121_1 = adafruit_mpr121.MPR121(i2c, address=MPR121_ADDR_1)
            self.mpr121_2 = adafruit_mpr121.MPR121(i2c, address=MPR121_ADDR_2)
            
            print(f"MPR121 sensors initialized at 0x{MPR121_ADDR_1:02X} and 0x{MPR121_ADDR_2:02X}")
            
            # Set up shared IRQ pin if using interrupt mode
            if self.use_irq:
                lgpio.gpio_claim_input(gpio_handle, MPR121_IRQ_PIN, lgpio.SET_PULL_UP)
                
                # IRQ is active LOW - set up alert on falling edge
                lgpio.gpio_claim_alert(gpio_handle, MPR121_IRQ_PIN, lgpio.FALLING_EDGE)
                self.callback_id = lgpio.callback(gpio_handle, MPR121_IRQ_PIN, lgpio.FALLING_EDGE, self._irq_callback)
                
                print(f"MPR121 IRQ mode enabled on GPIO {MPR121_IRQ_PIN} (shared, interrupt mode)")
            
        except Exception as e:
            print(f"Error initializing MPR121: {e}")
            self.mpr121_1 = None
            self.mpr121_2 = None
    
    def _irq_callback(self, chip, gpio, level, tick):
        """Interrupt callback for both MPR121 sensors (shared IRQ)"""
        # Check both sensors since they share the IRQ line
        self._process_sensor(1)
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
        
        Layout:
        Sensor 1 (0x5A):
          Pads 0-7: Kick steps 0-7
          Pads 8-11: Snare steps 0-3
        Sensor 2 (0x5B):
          Pads 0-3: Snare steps 4-7
          Pads 4-11: Hihat steps 0-7
        """
        if sensor_num == 1:
            if pad_num < 8:
                # Kick
                return 0, pad_num
            else:
                # Snare (first 4 steps)
                return 1, pad_num - 8
        else:  # sensor_num == 2
            if pad_num < 4:
                # Snare (last 4 steps)
                return 1, pad_num + 4
            else:
                # Hihat
                return 2, pad_num - 4
    
    def poll(self):
        """Poll touch sensors in non-IRQ mode, no-op if using interrupts"""
        if self.use_irq:
            return  # Interrupts handle everything
            
        if self.mpr121_1 is None or self.mpr121_2 is None:
            return
        
        # Polling mode - always check both sensors
        self._process_sensor(1)
        self._process_sensor(2)
    
    def cleanup(self):
        """Clean up GPIO resources"""
        if self.use_irq and LGPIO_AVAILABLE and self.gpio_handle is not None:
            try:
                # Cancel callback
                if self.callback_id is not None:
                    lgpio.callback_cancel(self.callback_id)
                
                # Free GPIO pin
                lgpio.gpio_free(self.gpio_handle, MPR121_IRQ_PIN)
            except:
                pass


class Sequencer:
    """Main sequencer engine"""
    def __init__(self, bpm=120):
        self.bpm = bpm
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
        samples = int(SAMPLE_RATE * duration)
        t = np.linspace(0, duration, samples)
        freq = {'kick': 60, 'snare': 200, 'hihat': 8000}.get(instrument_name, 440)
        data = np.sin(2 * np.pi * freq * t) * 0.3
        envelope = np.exp(-t * 10)
        data = (data * envelope).astype(np.float32)
        
        # Create a minimal DrumSample-like object
        class PlaceholderSample:
            def __init__(self, data):
                self.data = data
        
        return PlaceholderSample(data)
    
    def toggle_step(self, instrument_idx, step_idx):
        """Toggle a step on/off for a given instrument"""
        with self.lock:
            self.pattern[instrument_idx, step_idx] = not self.pattern[instrument_idx, step_idx]
            state = "ON" if self.pattern[instrument_idx, step_idx] else "OFF"
            print(f"{INSTRUMENT_NAMES[instrument_idx]} step {step_idx}: {state}")
    
    def set_bpm(self, bpm):
        """Update tempo"""
        self.bpm = max(40, min(240, bpm))  # Clamp between 40-240 BPM
        print(f"BPM: {self.bpm}")
    
    def trigger_samples(self, step):
        """Trigger samples for the current step"""
        with self.lock:
            for inst_idx, instrument_name in enumerate(INSTRUMENT_NAMES):
                if self.pattern[inst_idx, step]:
                    # Randomly select a sample from this instrument's bank
                    sample_bank = self.sample_banks[instrument_name]
                    selected_sample = random.choice(sample_bank)
                    
                    # Add sample to active voices with position counter
                    self.active_voices.append({
                        'data': selected_sample.data.copy(),
                        'position': 0,
                        'length': len(selected_sample.data)
                    })
    
    def audio_callback(self, outdata, frames, time_info, status):
        """Audio callback - mixes all active voices"""
        if status:
            print(f"Audio status: {status}")
        
        # Start with silence
        outdata.fill(0)
        
        with self.lock:
            voices_to_remove = []
            
            # Mix all active voices
            for i, voice in enumerate(self.active_voices):
                pos = voice['position']
                remaining = voice['length'] - pos
                
                if remaining <= 0:
                    voices_to_remove.append(i)
                    continue
                
                # How many samples to copy this block
                to_copy = min(frames, remaining)
                
                # Mix into output (mono to stereo)
                outdata[:to_copy, 0] += voice['data'][pos:pos + to_copy]
                outdata[:to_copy, 1] += voice['data'][pos:pos + to_copy]
                
                voice['position'] += to_copy
            
            # Remove finished voices (in reverse to maintain indices)
            for i in reversed(voices_to_remove):
                self.active_voices.pop(i)
    
    def sequencer_thread(self):
        """Main sequencer loop running in separate thread"""
        while self.is_playing:
            step_duration = 60.0 / self.bpm / 2  # 8th notes
            
            # Trigger samples for current step
            self.trigger_samples(self.current_step)
            print(f"Step: {self.current_step}")
            
            # Advance step
            self.current_step = (self.current_step + 1) % NUM_STEPS
            
            # Sleep until next step
            time.sleep(step_duration)
    
    def start(self):
        """Start playback"""
        if self.is_playing:
            return
        
        self.is_playing = True
        self.current_step = 0
        
        # Start audio stream
        self.stream = sd.OutputStream(
            samplerate=SAMPLE_RATE,
            blocksize=BLOCK_SIZE,
            channels=2,
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
    
    # Open GPIO chip
    gpio_handle = None
    if LGPIO_AVAILABLE:
        try:
            gpio_handle = lgpio.gpiochip_open(0)
        except Exception as e:
            print(f"Warning: Could not open GPIO: {e}")
    
    # Create sequencer
    seq = Sequencer(bpm=120)
    
    # Initialize touch handler (use_irq=True by default)
    touch = TouchHandler(seq, gpio_handle, use_irq=True)
    
    # Initialize LED handler
    leds = LEDHandler(seq)
    
    # Initialize OLED display
    oled = OLEDHandler(seq)
    
    # Initialize rotary encoder
    encoder = RotaryEncoder(seq, gpio_handle)
    
    # Set up a simple test pattern (for testing without touch sensors)
    # Kick on steps 0, 4
    seq.toggle_step(0, 0)
    seq.toggle_step(0, 4)
    # Snare on steps 2, 6
    seq.toggle_step(1, 2)
    seq.toggle_step(1, 6)
    # Hi-hat on all steps
    for i in range(8):
        seq.toggle_step(2, i)
    
    # Start playback
    seq.start()
    
    print("\nSequencer running.")
    print("- Touch pads to toggle steps")
    print("- Rotate encoder to change BPM")
    print("- Press encoder button to start/stop")
    print("- LEDs: Red=Kick, Green=Snare, Blue=Hihat, White=Current step")
    print("Press Ctrl+C to stop.\n")
    
    # Main loop - update LEDs and OLED (touch and encoder handled by interrupts)
    try:
        while True:
            touch.poll()  # No-op if using IRQ mode
            leds.update()
            oled.update()
            encoder.poll()  # No-op in interrupt mode
            time.sleep(0.01)  # Poll at 100Hz
    except KeyboardInterrupt:
        print("\nStopping...")
    except Exception as e:
        print(f"\nError in main loop: {e}")
    finally:
        # Ensure cleanup happens
        try:
            seq.stop()
        except:
            pass
        
        try:
            encoder.cleanup()
        except:
            pass
        
        try:
            touch.cleanup()
        except:
            pass
        
        # Turn off LEDs
        try:
            if leds.pixels:
                leds.pixels.fill((0, 0, 0))
                leds.pixels.show()
        except:
            pass
        
        # Close GPIO handle
        if gpio_handle is not None:
            try:
                lgpio.gpiochip_close(gpio_handle)
            except:
                pass
        
        print("Cleanup complete")


if __name__ == "__main__":
    main()
