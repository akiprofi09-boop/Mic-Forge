"""Explicit microphone capture -> DSP -> dedicated cable, never system loopback."""
from __future__ import annotations

import queue
import threading
import time
from collections import deque
from dataclasses import dataclass

import numpy as np
import sounddevice as sd

from dsp import FRAME, RATE, RNNOISE_DELAY_FRAMES, Settings, VoiceProcessor


def render_preview(original, settings):
    """Reprocess a saved original with current controls, aligned for comparison."""
    original = np.asarray(original, dtype=np.float32)
    if not len(original):
        return original.copy()
    processor = VoiceProcessor()
    processor.settings = settings
    try:
        padding = (-len(original)) % FRAME + RNNOISE_DELAY_FRAMES*FRAME
        source = np.pad(original, (0, padding))
        result = np.concatenate([processor.process(source[i:i+FRAME])
                                 for i in range(0, len(source), FRAME)])
        delay = RNNOISE_DELAY_FRAMES*FRAME
        return result[delay:delay+len(original)].copy()
    finally:
        processor.close()


def is_cable(name):
    lower = name.lower()
    return 'cable' in lower and ('vb-audio' in lower or 'virtual cable' in lower)


def is_physical_input(name):
    return not any(word in name.lower() for word in (
        'cable', 'voicemeeter', 'stereo mix', 'what u hear', 'loopback',
        'wave out', 'virtual', 'steam streaming', 'nvidia broadcast'))


@dataclass(frozen=True)
class Device:
    index: int
    name: str
    inputs: int
    outputs: int


def devices():
    apis = sd.query_hostapis()
    all_devices = sd.query_devices()
    wasapi = [Device(i, d['name'], d['max_input_channels'], d['max_output_channels'])
              for i, d in enumerate(all_devices)
              if 'WASAPI' in apis[d['hostapi']]['name']]
    microphones = [d for d in wasapi if d.inputs and is_physical_input(d.name)]
    cables = [d for d in wasapi if d.outputs and is_cable(d.name)]
    headphones = [d for d in wasapi if d.outputs and not is_cable(d.name)
                  and is_physical_input(d.name)]
    return microphones, cables, headphones


class SampleBuffer:
    """Bounded output buffer. Different capture/playback clocks cannot grow latency."""
    def __init__(self):
        self.lock = threading.Lock()
        self.chunks = deque()
        self.available = 0
        self.drops = 0
        self.underruns = 0

    def put(self, samples):
        with self.lock:
            self.chunks.append(samples)
            self.available += len(samples)
            while self.available > FRAME * 12:
                self.available -= len(self.chunks.popleft())
                self.drops += 1

    def take(self, count):
        result = np.zeros(count, dtype=np.float32)
        with self.lock:
            position = 0
            while position < count and self.chunks:
                chunk = self.chunks.popleft()
                n = min(count-position, len(chunk))
                result[position:position+n] = chunk[:n]
                self.available -= n
                position += n
                if n < len(chunk):
                    self.chunks.appendleft(chunk[n:])
            if position < count:
                self.underruns += 1
        return result


class AudioEngine:
    def __init__(self):
        self.running = False
        self.settings = Settings()
        self.error = ''
        self.raw_peak = 0.0
        self.wet_peak = 0.0
        self.clipped_until = 0.0
        self.limiting = False
        self.overflows = 0
        self.process_ms = 0.0
        self.recording = False
        self.record_lock = threading.Lock()
        self.recorded_raw = []
        self.recorded_wet = []
        self.record_frames = 0
        self.last_test = None
        self.monitor_enabled = False
        self._streams = []
        self.processor = None
        self.thread = None
        self.input_queue = queue.Queue(maxsize=24)
        self.cable_buffer = SampleBuffer()
        self.monitor_buffer = SampleBuffer()
        self.cable_device = None

    def start(self, microphone, cable=None):
        if self.running:
            return
        # Revalidate indices against live devices; never trust UI labels alone.
        mic_info = sd.query_devices(microphone.index)
        if not mic_info['max_input_channels'] or not is_physical_input(mic_info['name']):
            raise ValueError('Select a physical microphone, not a mixer or virtual input.')
        if cable is not None:
            cable_info = sd.query_devices(cable.index)
            if not cable_info['max_output_channels'] or not is_cable(cable_info['name']):
                raise ValueError('Live voice output must be a VB-Audio cable playback device.')
        self.error = ''
        self.overflows = 0
        self.raw_peak = self.wet_peak = 0
        self.input_queue = queue.Queue(maxsize=24)
        self.cable_buffer = SampleBuffer()
        self.monitor_buffer = SampleBuffer()
        self.cable_device = cable
        self.processor = VoiceProcessor()
        self.running = True
        try:
            output = None
            if cable is not None:
                output = self._output_stream(cable, self.cable_buffer)
                self._streams.append(output)
            self.thread = threading.Thread(target=self._process, name='MicForgeDSP', daemon=True)
            self.thread.start()
            sd.check_input_settings(device=microphone.index, channels=1, dtype='float32', samplerate=RATE)
            capture = sd.InputStream(device=microphone.index, samplerate=RATE,
                channels=1, dtype='float32', blocksize=FRAME, latency=0.03,
                callback=self._capture)
            self._streams.append(capture)
            capture.start()
            if output is not None:
                output.start()
        except Exception:
            self.stop()
            raise

    def _output_stream(self, device, buffer):
        channels = min(2, device.outputs)
        sd.check_output_settings(device=device.index, channels=channels, dtype='float32', samplerate=RATE)
        # Priming avoids a guaranteed underrun before the first capture callback.
        for _ in range(3):
            buffer.put(np.zeros(FRAME, dtype=np.float32))
        def callback(outdata, frames, timing, status):
            outdata[:] = buffer.take(frames)[:, None]
        return sd.OutputStream(device=device.index, samplerate=RATE,
            channels=channels, dtype='float32', blocksize=FRAME,
            latency=0.03, callback=callback)

    def _capture(self, indata, frames, timing, status):
        if status:
            self.overflows += 1
        try:
            self.input_queue.put_nowait(indata[:, 0].copy())
        except queue.Full:
            self.overflows += 1
            # Bound latency during CPU stalls, rather than replaying old speech.
            try:
                self.input_queue.get_nowait()
                self.input_queue.put_nowait(indata[:, 0].copy())
            except (queue.Empty, queue.Full):
                pass

    def _process(self):
        pending = np.empty(0, dtype=np.float32)
        try:
            while self.running:
                try:
                    block = self.input_queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                pending = np.concatenate((pending, block))
                while len(pending) >= FRAME and self.running:
                    raw = pending[:FRAME]
                    pending = pending[FRAME:]
                    start = time.perf_counter()
                    self.processor.settings = self.settings
                    wet = self.processor.process(raw)
                    self.process_ms = 0.9*self.process_ms + 0.1*(time.perf_counter()-start)*1000
                    self.raw_peak = float(np.max(np.abs(raw)))
                    self.wet_peak = float(np.max(np.abs(wet)))
                    self.limiting = self.processor.limiting
                    if self.raw_peak >= 0.999:
                        self.clipped_until = time.monotonic()+2
                    if self.cable_device is not None:
                        self.cable_buffer.put(wet)
                    if self.monitor_enabled:
                        self.monitor_buffer.put(wet)
                    with self.record_lock:
                        if self.recording:
                            self.recorded_raw.append(raw.copy())
                            self.recorded_wet.append(wet.copy())
                            self.record_frames += FRAME
                            if self.record_frames >= RATE*12:
                                self._finish_recording_locked()
        except Exception as exc:
            self.error = f'Audio processing stopped: {exc}'
            self.running = False

    def set_monitor(self, enabled, device=None):
        if hasattr(self, '_monitor_stream') and self._monitor_stream is not None:
            self._monitor_stream.stop()
            self._monitor_stream.close()
            self._monitor_stream = None
        self.monitor_enabled = False
        if enabled:
            if not self.running or device is None:
                raise ValueError('Start your microphone and select headphones first.')
            info = sd.query_devices(device.index)
            if not info['max_output_channels'] or not is_physical_input(info['name']):
                raise ValueError('Monitoring requires a physical headphone output.')
            self.monitor_buffer = SampleBuffer()
            output = self._output_stream(device, self.monitor_buffer)
            try:
                output.start()
            except Exception:
                output.close()
                raise
            self._monitor_stream = output
            self.monitor_enabled = True

    def start_recording(self):
        if not self.running:
            raise ValueError('Start the microphone first.')
        with self.record_lock:
            self.recorded_raw = []
            self.recorded_wet = []
            self.record_frames = 0
            self.last_test = None
            self.recording = True

    def _finish_recording_locked(self):
        self.recording = False
        if self.recorded_raw:
            self.last_test = (np.concatenate(self.recorded_raw), np.concatenate(self.recorded_wet))
        self.recorded_raw = []
        self.recorded_wet = []

    def finish_recording(self):
        with self.record_lock:
            self._finish_recording_locked()

    def stop(self):
        self.running = False
        self.set_monitor(False)
        for stream in reversed(self._streams):
            try:
                stream.stop()
            finally:
                stream.close()
        self._streams = []
        if self.thread:
            self.thread.join(timeout=3)
            if self.thread.is_alive():
                raise RuntimeError('The audio worker did not stop. Close Mic Forge before restarting.')
            self.thread = None
        if self.processor:
            self.processor.close()
            self.processor = None
        self.finish_recording()
        self.raw_peak = self.wet_peak = 0


class TestPlayback:
    """Finite, cancellable test playback restricted to a physical output."""
    def __init__(self):
        self.stream = None
        self.samples = None
        self.position = 0
        self.done = True

    def play(self, samples, device):
        self.stop()
        info = sd.query_devices(device.index)
        if not info['max_output_channels'] or not is_physical_input(info['name']):
            raise ValueError('Test playback requires physical headphones, never the cable.')
        self.samples = samples
        self.position = 0
        self.done = False
        def callback(outdata, frames, timing, status):
            outdata.fill(0)
            n = min(frames, len(self.samples)-self.position)
            outdata[:n] = self.samples[self.position:self.position+n, None] * 0.7
            self.position += n
            if self.position >= len(self.samples):
                self.done = True
                raise sd.CallbackStop
        self.stream = sd.OutputStream(device=device.index, samplerate=RATE,
            channels=min(2, device.outputs), dtype='float32', callback=callback)
        try:
            self.stream.start()
        except Exception:
            self.stop()
            raise

    def stop(self):
        if self.stream:
            self.stream.stop()
            self.stream.close()
            self.stream = None
        self.done = True
