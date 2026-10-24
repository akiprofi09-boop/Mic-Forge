# Third-party components

Mic Forge uses these independent projects. Their package license files and notices are retained in the packaged `licenses` directory. The installed distributions are the authoritative source for their full terms.

| Component | Purpose | Source |
| --- | --- | --- |
| Qt / PySide6 Essentials / Shiboken6 | Native Windows UI, dynamically linked | https://www.qt.io/qt-for-python |
| NumPy | Sample buffers and arithmetic | https://numpy.org/ |
| SciPy | Audio filters | https://scipy.org/ |
| python-sounddevice / PortAudio / CFFI | Windows audio IO | https://python-sounddevice.readthedocs.io/ |
| pyrnnoise / RNNoise | Bundled native noise suppression library; wrapper CLI unused | https://github.com/pengzhendong/pyrnnoise / https://gitlab.xiph.org/xiph/rnnoise |
| PyInstaller | Windows packaging | https://pyinstaller.org/ |

Source code and build instructions for Mic Forge accompany the executable. Qt libraries remain separate files in the folder package. Do not remove library notices when redistributing. Check the included component licenses before publishing a derivative or installer.

VB-CABLE is not included. Obtain it from https://vb-audio.com/Cable/ and follow the vendor's terms.
