# Auto Editor

Auto Editor is an automated desktop screen recording and video post-production tool. It intelligently detects dead air to accelerate pacing, tracks cursor and click interactions to produce smooth virtual camera zooms, ducks synthetic audio sound effects under voice, and can generate dynamic subtitles using Whisper AI.

---

## Key Features

### 1. Smart Virtual Camera
- **Dynamic Pan & Zoom**: Follows cursor movement using exponential moving average (EMA) smoothing for cinematic focus.
- **Click Punch-Ins**: Automatically punches in on click locations and keyboard events, accentuating important UI interactions.
- **Blurred Canvas Fill**: Formats videos onto clean padded canvases with styled blurred backgrounds.

### 2. Intelligent Dead-Air Speedup
- **Pacing Optimization**: Detects pauses where neither voice activity nor user interaction occurs and accelerates them seamlessly.
- **Audio Preservation**: Avoids abrupt cuts or mid-word clipping by analyzing speech energy contours.

### 3. Integrated Audio & Sound Effects
- **Procedural Sound Effects**: Includes subtle click, keystroke, and transition whoosh sounds synchronized to user actions.
- **Audio Ducking**: Automatically attenuates sound effects during active narration so voice clarity remains primary.
- **Custom Sound Packs**: Easily replace sound files in the `sfx/` folder with your own custom audio cues.

### 4. Built-In Screen Recorder
- **60 FPS Capture**: High frame rate capture via FFmpeg `gdigrab`.
- **Interaction Logging**: Captures mouse coordinates, clicks, scrolls, and key presses simultaneously into an `.events.json` sidecar.
- **Privacy First**: Keyboard events are logged strictly as timestamps of key activity—individual keystrokes or character inputs are never recorded.
- **One-Key Stop**: Stop recording anytime with the `F9` hotkey, which immediately hands off the footage to the auto-editing pipeline.

### 5. Automated Folder Watcher
- **Batch Processing**: Point to a directory and automatically edit all queued video files.
- **Drop-in Watch Mode**: Leave Auto Editor open with "Watch Folder" active to automatically process screen captures or phone videos the moment they arrive.

---

## Installation & Setup

### Prerequisites
1. **Python 3.9 or higher**: [python.org](https://www.python.org/downloads/)
2. **FFmpeg on system PATH**:
   - **Windows**: `winget install Gyan.FFmpeg` or extract essentials to `C:\ffmpeg\bin`.
   - **macOS**: `brew install ffmpeg`
   - **Linux**: `sudo apt install ffmpeg`

### Quick Start (Windows)
1. Double-click `run.bat`.
   - The launcher verifies your Python and FFmpeg environment, installs missing requirements, and launches Auto Editor.

### Manual Setup (Cross-Platform)
1. Clone this repository:
   ```bash
   git clone https://github.com/your-username/auto-editor.git
   cd auto-editor
   ```
2. (Optional) Create and activate a virtual environment:
   ```bash
   python -m venv venv
   # On Windows:
   venv\Scripts\activate
   # On macOS/Linux:
   source venv/bin/activate
   ```
3. Install required libraries:
   ```bash
   pip install -r requirements.txt
   ```
4. Start the application:
   ```bash
   python app.py
   ```

### Optional AI Subtitles
To generate bold synchronized subtitles (.ass):
```bash
pip install faster-whisper
```

---

## Usage Guide

### Record Tab
1. Select your target directory and microphone input (if desired).
2. Click **Start Recording**.
3. Perform your demo or tutorial on the primary monitor.
4. Press **F9** to stop recording. The application will log events and trigger the auto-editor automatically.

### Edit Tab
1. Select any directory containing existing `.mp4`, `.mov`, `.mkv`, or `.webm` files.
2. Click **Edit All New Videos** (or toggle **Watch folder** for automatic processing).
3. Edited outputs are generated directly in the same folder with the `_edited.mp4` suffix.

---

## Project Structure

```text
auto-editor/
├── app.py               # CustomTkinter graphical desktop interface
├── editor_core.py       # Core editing engine (camera motion, audio ducking, dead air, rendering)
├── recorder.py          # Screen recorder & interaction event logger
├── requirements.txt     # Python package requirements
├── run.bat              # One-click Windows startup script
├── sfx/                 # Sound effects folder
│   ├── click.wav        # Mouse click sound effect
│   ├── keystroke.wav    # Typing sound effect
│   └── whoosh.wav       # Camera pan transition sound effect
├── .gitignore           # Git ignore patterns for temporary footage and build artifacts
├── .gitattributes       # Git normalization rules for binary audio and text
└── README.md            # Documentation and usage instructions
```
