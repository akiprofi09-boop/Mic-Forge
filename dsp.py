"""Mono 48 kHz voice processing. No device access, disk IO or networking."""
from __future__ import annotations

import ctypes
import importlib.util
import sys
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from scipy.signal import butter, sosfilt

RATE = 48_000
FRAME = 480
# The native library in the pinned pyrnnoise 0.4.5 wheel has two-frame delay.
RNNOISE_DELAY_FRAMES = 2


@dataclass(frozen=True)
class Settings:
    enhance: float = 40
    noise: float = 70
    warmth: float = 0
    clarity: float = 0
    compression: float = 35
    gain_db: float = 0
    bypass: bool = False


class RNNoise:
    """Use the upstream RNNoise C ABI, avoiding resamplers/CLI dependencies."""
    def __init__(self):
        if getattr(sys, 'frozen', False):
            root = Path(sys._MEIPASS) / 'pyrnnoise'
        else:
            spec = importlib.util.find_spec('pyrnnoise')
            if spec is None or not spec.submodule_search_locations:
                raise RuntimeError('RNNoise is missing. Reinstall Mic Forge dependencies.')
            root = Path(next(iter(spec.submodule_search_locations)))
        candidates = list(root.rglob('*rnnoise*.dll'))
        if not candidates:
            candidates = list(root.rglob('*.dll'))
        if not candidates:
            raise RuntimeError('The RNNoise Windows library was not found.')
        self.lib = ctypes.CDLL(str(candidates[0]))
        self.lib.rnnoise_get_frame_size.restype = ctypes.c_int
        if self.lib.rnnoise_get_frame_size() != FRAME:
            raise RuntimeError('This RNNoise library uses an unsupported frame size.')
        self.lib.rnnoise_create.argtypes = [ctypes.c_void_p]
        self.lib.rnnoise_create.restype = ctypes.c_void_p
        self.lib.rnnoise_destroy.argtypes = [ctypes.c_void_p]
        self.lib.rnnoise_destroy.restype = None
        ptr = ctypes.POINTER(ctypes.c_float)
        self.lib.rnnoise_process_frame.argtypes = [ctypes.c_void_p, ptr, ptr]
        self.lib.rnnoise_process_frame.restype = ctypes.c_float
        self.state = self.lib.rnnoise_create(None)
        if not self.state:
            raise RuntimeError('Unable to initialize RNNoise.')
        self._source = np.empty(FRAME, dtype=np.float32)
        self._result = np.empty(FRAME, dtype=np.float32)
        self._source_pointer = self._source.ctypes.data_as(ptr)
        self._result_pointer = self._result.ctypes.data_as(ptr)

    def process(self, samples):
        # RNNoise's float API uses signed 16-bit amplitude, not normalized floats.
        np.multiply(samples, 32768, out=self._source, casting='unsafe')
        probability = self.lib.rnnoise_process_frame(
            self.state, self._result_pointer, self._source_pointer)
        return self._result / 32768, float(probability)

    def close(self):
        if self.state:
            self.lib.rnnoise_destroy(self.state)
            self.state = None


def peak_sos(frequency, gain_db, q=0.8):
    a = 10 ** (gain_db / 40)
    omega = 2 * np.pi * frequency / RATE
    alpha = np.sin(omega) / (2 * q)
    c = np.cos(omega)
    b = np.array([1 + alpha*a, -2*c, 1 - alpha*a])
    a_coeff = np.array([1 + alpha/a, -2*c, 1 - alpha/a])
    return np.r_[b / a_coeff[0], a_coeff / a_coeff[0]]


def shelf_sos(frequency, gain_db, high=False):
    """Monotonic S=1 shelving biquad; RBJ/W3C Audio EQ Cookbook formulae.

    https://www.w3.org/TR/audio-eq-cookbook/
    """
    a = 10 ** (gain_db / 40)
    omega = 2*np.pi*frequency/RATE
    c = np.cos(omega)
    beta = np.sin(omega)*np.sqrt(2*a)
    if high:
        b = a*np.array([(a+1)+(a-1)*c+beta,
                        -2*((a-1)+(a+1)*c), (a+1)+(a-1)*c-beta])
        denominator = np.array([(a+1)-(a-1)*c+beta,
                                2*((a-1)-(a+1)*c), (a+1)-(a-1)*c-beta])
    else:
        b = a*np.array([(a+1)-(a-1)*c+beta,
                        2*((a-1)-(a+1)*c), (a+1)-(a-1)*c-beta])
        denominator = np.array([(a+1)+(a-1)*c+beta,
                                -2*((a-1)+(a+1)*c), (a+1)+(a-1)*c-beta])
    return np.r_[b/denominator[0], denominator/denominator[0]]


class VoiceProcessor:
    def __init__(self):
        self.denoiser = RNNoise()
        self.settings = Settings()
        self._eq_key = None
        self.sos = None
        self.zi = np.zeros((6, 2))
        self.compressor_gain = 1.0
        self.probability = 0.0
        self.limiting = False
        self._control_key = None
        self._target_gains = np.empty(FRAME, dtype=np.float64)
        self._compressor_gains = np.empty(FRAME, dtype=np.float64)
        # Delay measured against the pinned library, not assumed from older builds.
        self.dry_delay = deque(np.zeros(FRAME, dtype=np.float32)
                               for _ in range(RNNOISE_DELAY_FRAMES))

    def process(self, samples):
        x = np.asarray(samples, dtype=np.float32)
        if x.shape != (FRAME,):
            raise ValueError(f'Expected exactly {FRAME} mono samples.')
        x = np.nan_to_num(x, nan=0.0, posinf=1.0, neginf=-1.0)
        settings = self.settings
        denoised, self.probability = self.denoiser.process(x)
        dry = self.dry_delay.popleft()
        self.dry_delay.append(x)
        if settings.bypass:
            self.limiting = False
            return dry.copy()

        if settings != self._control_key:
            self._control_key = settings
            self._noise_strength = np.clip(settings.noise / 100, 0, 1)
            self._enhance = np.clip(settings.enhance / 100, 0, 1)
            self._headroom = 10 ** (-2.0*self._enhance / 20)
            amount = np.clip(settings.compression / 100 + 0.45*self._enhance, 0, 1)
            self._reduction_factor = 1 - 1/(1 + 3*amount)
            self._makeup = 10 ** (4*amount / 20)
            self._output_gain = 10 ** (settings.gain_db / 20)
            self._attack = float(np.exp(-1 / (0.004 * RATE)))
            self._release = float(np.exp(-1 / (0.120 * RATE)))
        strength = self._noise_strength
        y = dry * (1-strength) + denoised * strength
        enhance = self._enhance
        eq_key = (settings.enhance, settings.warmth, settings.clarity)
        if eq_key != self._eq_key:
            self._eq_key = eq_key
            self.sos = np.vstack([
                butter(1, 75-20*enhance, btype='highpass', fs=RATE, output='sos'),
                shelf_sos(160, 7.0*enhance),
                peak_sos(180, settings.warmth),
                peak_sos(400, -4.5*enhance, q=0.9),
                peak_sos(3200, settings.clarity + 5.5*enhance),
                shelf_sos(6500, 1.5*enhance, high=True),
            ])
        y, self.zi = sosfilt(self.sos, y, zi=self.zi)
        # Reserve headroom for the stronger contour, rather than equating more
        # enhancement with a broadband volume increase.
        y *= self._headroom

        # A continuous envelope avoids applying a single gain step to each frame.
        # Vectorize independent level calculations; keep the original sequential
        # attack/release envelope, sample order, and persistent gain state.
        targets = self._target_gains
        np.abs(y, out=targets)
        np.maximum(targets, 1e-8, out=targets)
        np.log10(targets, out=targets)
        targets *= 20
        targets += 22
        np.maximum(targets, 0, out=targets)
        targets *= self._reduction_factor
        targets /= -20
        np.power(10, targets, out=targets)
        attack, release = self._attack, self._release
        attack_rest, release_rest = 1-attack, 1-release
        makeup = float(self._makeup)
        gain = float(self.compressor_gain)
        gains = self._compressor_gains
        for i, target in enumerate(targets.tolist()):
            if target < gain:
                gain = attack*gain + attack_rest*target
            else:
                gain = release*gain + release_rest*target
            gains[i] = gain * makeup
        self.compressor_gain = gain
        y *= gains
        y *= self._output_gain

        # Continuous soft knee with a strict -1 dBFS sample ceiling.
        ceiling = 10 ** (-1 / 20)
        knee = 0.65
        absolute = np.abs(y, out=self._target_gains)
        self.limiting = bool(np.any(absolute > knee))
        mask = absolute > knee
        y[mask] = np.sign(y[mask]) * (
            knee + (ceiling-knee) * np.tanh((absolute[mask]-knee)/(ceiling-knee)))
        return np.clip(y, -ceiling, ceiling).astype(np.float32)

    def close(self):
        self.denoiser.close()
