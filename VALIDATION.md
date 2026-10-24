# Validation - 0.2.0

## Signal and regression checks

- 36 audio/UI tests passed on Windows with Python 3.12. No live audio streams opened by the tests.
- Compared the optimized DSP with the retained 0.1.4 implementation across 600 frames / 288,000 samples, five settings transitions, bypass, silence, noise, hot input and nonfinite input sanitization. Output was bit-identical for every tested sample. This is coverage of those inputs, not a proof for every possible waveform.
- RNNoise stationary-noise attenuation: 37.6 dB in the existing synthetic test.
- UI checks cover all six sliders, linked tone pad and keyboard controls, recording/preview states, guarded startup, tray restore and error handling while hidden.
- The 800 x 780 default window fits the controls without scrolling at the tested 100% display scale. Scrolling remains available at smaller sizes or larger scaling.

## Controlled process benchmark

Same Windows PC (12 logical processors), same interpreter, same settings and paced synthetic voice/noise input: 48 kHz, 480 samples every 10 ms. Two seconds warmup, then eight seconds measured. Includes the application's audio worker, queues and UI timer. Both windows minimized to the tray. No physical capture/playback streams, no gameplay and no microphone recording. Separate processes and temporary preferences prevent interference with saved user settings.

| Measurement | 0.1.4 | 0.2.0 |
| --- | ---: | ---: |
| CPU, one logical processor equivalent | 18.95% | 11.91% |
| CPU, normalized across 12 logical processors | 1.58% | 0.99% |
| Resident working set | 185.59 MiB | 177.19 MiB |
| Private committed memory | 829.25 MiB | 118.24 MiB |
| DSP median | 1.62 ms | 1.05 ms |
| DSP 95th percentile | 2.45 ms | 1.50 ms |
| Processed frames | 800 | 800 |
| Capture queue drops | 0 | 0 |

This single before/after run measured about 37% less process CPU, 4.5% less resident RAM and 86% less private commit. Private commit is memory allocation, not physical RAM consumption. Timings vary with CPU load and hardware; these figures do not promise a particular FPS improvement. Live WASAPI device/driver overhead is outside this benchmark.

The savings come from cached controls, reusable RNNoise buffers, vectorized compressor target calculations with the original per-sample envelope recurrence, one BLAS worker instead of idle thread pools, and skipping visual-only updates while hidden. Audio frame size, processing stages, metering rate while visible, preview quality and recording limits remain unchanged.

Raw reports: [before](docs/benchmarks/before.json), [after](docs/benchmarks/after.json), [signal comparison](docs/benchmarks/signal-comparison.json).

```powershell
.\.venv\Scripts\python.exe -m unittest test_audio test_ui -q
.\.venv\Scripts\python.exe benchmarks/compare_dsp.py --reference benchmarks/reference_dsp_v0_1_4.py --output signal-comparison.json
.\.venv\Scripts\python.exe benchmarks/benchmark_app.py --source . --output benchmark.json --label 0.2.0 --mode tray
```

The benchmark is Windows-only. Run each version sequentially under comparable load. The retained reference DSP supports reproducing the signal comparison; the old application snapshot was kept locally for the before measurement.
