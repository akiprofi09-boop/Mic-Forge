## Mic Forge 0.2.0

- Compact 800 x 780 desktop layout, restrained colors and all controls together.
- All existing audio, tone pad, recording, preview/export and tray features retained.
- Cached DSP controls, reusable native buffers and vectorized compressor targets. The tested 288,000-sample comparison against 0.1.4 was bit-identical.
- Single-worker BLAS configuration avoids unused worker stacks. Hidden windows skip visual-only updates while audio and error handling continue.
- In the controlled synthetic tray test: about 37% less process CPU, 4.5% less resident RAM and 86% less private memory allocation. See VALIDATION.md for method and limits.
- 36 audio/UI tests and a packaged synthetic runtime check. No driver installation or Windows audio-default changes.

Download the Windows x64 ZIP, extract the complete folder, and launch Mic Forge.exe. VB-CABLE is a separate installation from VB-Audio. Keep your playback on your headset and choose CABLE Output as the microphone in Discord or games.

Existing settings remain compatible. Monitoring starts off. The executable is unsigned.
