#!/usr/bin/env python3
"""
Simple 8-step, 3-instrument drum machine sequencer for Raspberry Pi Zero 2 W
"""

import numpy as np
import sounddevice as sd
import time
import threading
from pathlib import Path

# Audio configuration
SAMPLE_RATE = 44100
BLOCK_SIZE = 512  # Buffer size for low latency

# Sequencer configuration
NUM_STEPS = 8
NUM_INSTRUMENTS = 3
INSTRUMENT_NAMES = ['kick', 'snare', 'hihat']

class DrumSample:
    """Represents a single drum sample"""
    def __init__(self, filepath):
        self.filepath = filepath
        self.data = None
        self.load()
    
    def load(self):
        """Load WAV file into numpy array"""
        # TODO: Load actual WAV file
        # For now, create a simple sine wave as placeholder
        duration = 0.3  # 300ms sample
        samples = int(SAMPLE_RATE * duration)
        t = np.linspace(0, duration, samples)
        # Different frequencies for different drums
        freq = 60 if 'kick' in str(self.filepath) else (200 if 'snare' in str(self.filepath) else 8000)
        self.data = np.sin(2 * np.pi * freq * t) * 0.3
        # Apply envelope
        envelope = np.exp(-t * 10)
        self.data = self.data * envelope
        self.data = self.data.astype(np.float32)


class Sequencer:
    """Main sequencer engine"""
    def __init__(self, bpm=120):
        self.bpm = bpm
        self.current_step = 0
        self.pattern = np.zeros((NUM_INSTRUMENTS, NUM_STEPS), dtype=bool)
        self.is_playing = False
        self.samples = {}
        self.active_voices = []  # List of currently playing samples
        self.lock = threading.Lock()
        
        # Load samples
        self.load_samples()
        
        # Audio stream
        self.stream = None
        
    def load_samples(self):
        """Load drum samples from files"""
        # TODO: Load from actual files in /home/pi/samples/
        for name in INSTRUMENT_NAMES:
            self.samples[name] = DrumSample(f"{name}.wav")
        print(f"Loaded {len(self.samples)} samples")
    
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
                    # Add sample to active voices with position counter
                    sample_data = self.samples[instrument_name].data.copy()
                    self.active_voices.append({
                        'data': sample_data,
                        'position': 0,
                        'length': len(sample_data)
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
        if self.stream:
            self.stream.stop()
            self.stream.close()
        print("Sequencer stopped")


def main():
    """Main program entry point"""
    print("Raspberry Pi Drum Machine")
    print("=" * 40)
    
    # Create sequencer
    seq = Sequencer(bpm=120)
    
    # Set up a simple test pattern
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
    
    # Run for 10 seconds
    try:
        time.sleep(10)
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        seq.stop()


if __name__ == "__main__":
    main()
