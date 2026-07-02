<div align="center">

<img width="1000" alt="banner_bordered_trimmed" src="https://github.com/user-attachments/assets/64f13b39-da06-4f58-add0-cfc44f04db4e" />

<h2>The Agentive Operating System for Physical Space</h2>

[![Discord](https://img.shields.io/discord/1341146487186391173?style=flat-square&logo=discord&logoColor=white&label=Discord&color=5865F2)](https://discord.gg/dimos)
[![Stars](https://img.shields.io/github/stars/dimensionalOS/dimos?style=flat-square)](https://github.com/dimensionalOS/dimos/stargazers)
[![Forks](https://img.shields.io/github/forks/dimensionalOS/dimos?style=flat-square)](https://github.com/dimensionalOS/dimos/fork)
[![Contributors](https://img.shields.io/github/contributors/dimensionalOS/dimos?style=flat-square)](https://github.com/dimensionalOS/dimos/graphs/contributors)
![Nix](https://img.shields.io/badge/Nix-flakes-5277C3?style=flat-square&logo=NixOS&logoColor=white)
![NixOS](https://img.shields.io/badge/NixOS-supported-5277C3?style=flat-square&logo=NixOS&logoColor=white)
![CUDA](https://img.shields.io/badge/CUDA-supported-76B900?style=flat-square&logo=nvidia&logoColor=white)
[![Docker](https://img.shields.io/badge/Docker-ready-2496ED?style=flat-square&logo=docker&logoColor=white)](https://www.docker.com/)

<a href="https://trendshift.io/repositories/23169" target="_blank"><img src="https://trendshift.io/api/badge/repositories/23169" alt="dimensionalOS%2Fdimos | Trendshift" style="width: 250px; height: 55px;" width="250" height="55"/></a>

<big><big>

[Hardware](#hardware) •
[Installation](#installation) •
[Agent CLI & MCP](#agent-cli-and-mcp) •
[Blueprints](#blueprints) •
[Development](#development)

⚠️ **Pre-Release Beta** ⚠️

</big></big>

</div>

# Intro

Dimensional is the modern operating system for generalist robotics. We are setting the next-generation SDK standard, integrating with the majority of robot manufacturers.

With a simple install and no ROS required, build physical applications entirely in python that run on any humanoid, quadruped, or drone.

Dimensional is agent native -- "vibecode" your robots in natural language and build (local & hosted) multi-agent systems that work seamlessly with your hardware. Agents run as native modules — subscribing to any embedded stream, from perception (lidar, camera) and spatial memory down to control loops and motor drivers.
<table>
  <tr>
    <td align="center" width="50%">
      <a href="docs/capabilities/navigation/index.md"><img src="assets/readme/navigation.gif" alt="Navigation" width="100%"></a>
    </td>
    <td align="center" width="50%">
      <img src="assets/readme/perception.png" alt="Perception" width="100%">
    </td>
  </tr>
  <tr>
    <td align="center" width="50%">
      <h3><a href="docs/capabilities/navigation/index.md">Navigation and Mapping</a></h3>
      SLAM, dynamic obstacle avoidance, route planning, and autonomous exploration — via both DimOS native and ROS<br><a href="https://x.com/stash_pomichter/status/2010471593806545367">Watch video</a>
    </td>
    <td align="center" width="50%">
      <h3>Perception</h3>
      Detectors, 3d projections, VLMs, Audio processing
    </td>
  </tr>
  <tr>
    <td align="center" width="50%">
      <a href="docs/capabilities/agents/readme.md"><img src="assets/readme/agentic_control.gif" alt="Agents" width="100%"></a>
    </td>
    <td align="center" width="50%">
      <img src="assets/readme/spatial_memory.gif" alt="Spatial Memory" width="100%">
    </td>
  </tr>
  <tr>
    <td align="center" width="50%">
      <h3><a href="docs/capabilities/agents/readme.md">Agentive Control, MCP</a></h3>
      "hey Robot, go find the kitchen"<br><a href="https://x.com/stash_pomichter/status/2015912688854200322">Watch video</a>
    </td>
    <td align="center" width="50%">
      <h3>Spatial Memory</a></h3>
      Spatio-temporal RAG, Dynamic memory, Object localization and permanence<br><a href="https://x.com/stash_pomichter/status/1980741077205414328">Watch video</a>
    </td>
  </tr>
</table>


# Hardware

<table>
  <tr>
    <td align="center" width="20%">
      <h3>Quadruped</h3>
      <img width="245" height="1" src="assets/readme/spacer.png">
    </td>
    <td align="center" width="20%">
      <h3>Humanoid</h3>
      <img width="245" height="1" src="assets/readme/spacer.png">
    </td>
    <td align="center" width="20%">
      <h3>Arm</h3>
      <img width="245" height="1" src="assets/readme/spacer.png">
    </td>
    <td align="center" width="20%">
      <h3>Drone</h3>
      <img width="245" height="1" src="assets/readme/spacer.png">
    </td>
    <td align="center" width="20%">
      <h3>Misc</h3>
      <img width="245" height="1" src="assets/readme/spacer.png">
    </td>
  </tr>

  <tr>
    <td align="center" width="20%">
      🟩 <a href="docs/platforms/quadruped/go2/index.md">Unitree Go2 pro/air</a><br>
      🟥 <a href="dimos/robot/unitree/b1">Unitree B1</a><br>
    </td>
    <td align="center" width="20%">
      🟨 <a href="docs/platforms/humanoid/g1/index.md">Unitree G1</a><br>
    </td>
    <td align="center" width="20%">
      🟨 <a href="docs/capabilities/manipulation/readme.md">Xarm</a><br>
      🟨 <a href="docs/capabilities/manipulation/readme.md">AgileX Piper</a><br>
    </td>
    <td align="center" width="20%">
      🟧 <a href="dimos/robot/drone/README.md">MAVLink</a><br>
      🟧 <a href="dimos/robot/drone/README.md">DJI Mavic</a><br>
    </td>
    <td align="center" width="20%">
      🟥 <a href="https://github.com/dimensionalOS/openFT-sensor">Force Torque Sensor</a><br>
    </td>
  </tr>
</table>
<br>
<div align="right">
🟩 stable 🟨 beta 🟧 alpha 🟥 experimental

</div>

> [!IMPORTANT]
> 🤖 Direct your favorite Agent (OpenClaw, Claude Code, etc.) to [AGENTS.md](AGENTS.md) and our [CLI and MCP](#agent-cli-and-mcp) interfaces to start building powerful Dimensional applications.

# Installation

## Interactive Install

```sh skip
curl -fsSL https://raw.githubusercontent.com/dimensionalOS/dimos/main/scripts/install.sh | bash
```

> See [`scripts/install.sh --help`](scripts/install.sh) for non-interactive and advanced options.

## Manual System Install

To set up your system dependencies, follow one of these guides:

- 🟩 [Ubuntu 22.04 / 24.04](docs/installation/ubuntu.md)
- 🟩 [NixOS / General Linux](docs/installation/nix.md)
- 🟧 [macOS](docs/installation/osx.md)

> Full system requirements, tested configs, and dependency tiers: [docs/requirements.md](docs/requirements.md)

## Python Install

### Quickstart

```bash
uv venv --python "3.12"
source .venv/bin/activate
uv pip install 'dimos[base,unitree]'

# Replay a recorded quadruped session (no hardware needed)
# NOTE: First run will show a black rerun window while ~75 MB downloads from LFS
dimos --replay run unitree-go2
```

```bash
# Install with simulation support
uv pip install 'dimos[base,unitree,sim]'

# Run quadruped in MuJoCo simulation
dimos --simulation run unitree-go2

# Run humanoid in simulation
dimos --simulation run unitree-g1-sim
```

```bash
# Control a real robot (Unitree quadruped over WebRTC)
export ROBOT_IP=<YOUR_ROBOT_IP>
dimos run unitree-go2
```

# Featured Runfiles

| Run command | What it does |
|-------------|-------------|
| `dimos --replay run unitree-go2` | Quadruped navigation replay — SLAM, costmap, A* planning |
| `dimos --replay --replay-db go2_bigoffice run unitree-go2-memory` | Quadruped temporal memory replay |
| `dimos --simulation run unitree-go2-agentic` | Quadruped agentic + MCP server in simulation |
| `dimos --simulation run unitree-g1-sim` | Humanoid in MuJoCo simulation |
| `dimos --replay run drone-basic` | Drone video + telemetry replay |
| `dimos --replay run drone-agentic` | Drone + LLM agent with flight skills (replay) |
| `dimos run demo-camera` | Webcam demo — no hardware needed |
| `dimos run keyboard-teleop-xarm7` | Keyboard teleop with mock xArm7 (requires `dimos[manipulation]` extra) |
| `dimos --simulation run unitree-go2-agentic-ollama` | Quadruped agentic with local LLM (requires [Ollama](https://ollama.com) + `ollama serve`) |

> Full blueprint docs: [docs/usage/blueprints.md](docs/usage/blueprints.md)

# Agent CLI and MCP

The `dimos` CLI manages the full lifecycle — run blueprints, inspect state, interact with agents, and call skills via MCP.

```bash
dimos run unitree-go2-agentic --daemon   # Start in background
dimos status                              # Check what's running
dimos log -f                              # Follow logs
dimos agent-send "explore the room"       # Send agent a command
dimos mcp list-tools                      # List available MCP skills
dimos mcp call relative_move --arg forward=0.5  # Call a skill directly
dimos stop                                # Shut down
```

> Full CLI reference: [docs/usage/cli.md](docs/usage/cli.md)


# Robot Voice I/O (Go2 speaker + microphone)

On a Unitree Go2 the agent talks and listens through the **robot's own speaker
and microphone**, not the host machine's audio devices. Both directions ride the
**single, already-negotiated WebRTC connection** — no second connection is opened
and no SDP renegotiation happens (the Go2 tolerates one connection per boot).

| Direction | Path |
|-----------|------|
| **Speak (TTS out)** | `speak` skill → OpenAI TTS → `RobotSpeakerTrack` swapped onto the pre-negotiated audio sender with `replaceTrack` → Go2 speaker |
| **Listen (STT in)** | Go2 mic → `GO2Connection.audio_stream` (48 kHz stereo `AudioEvent`) → `WebInput` resamples to 16 kHz mono → voice-activity gate → Whisper → `/human_input` |

## How it decides: robot vs. local

One shared rule governs both directions, so the speaker and mic always stay in
lock-step (`use_robot_audio` in `dimos/robot/unitree/go2/connection_spec.py`):

```
use robot audio  ==  a robot connection is present  AND  force_local_audio is False
```

- **Default (connection present):** speak through the Go2 speaker, listen through
  the Go2 mic.
- **No connection (sim, replay, non-robot blueprint):** fall back to local audio
  out and browser push-to-talk in.
- **`force_local_audio: true`** (`GlobalConfig`, `dimos/core/global_config.py`):
  debug override that forces both directions back to the host's local devices
  even when a robot is connected.

## The speaker track (why one persistent track)

aiortc permanently tears an RTP sender down the first time a track signals
end-of-file, and the Go2 cannot renegotiate to rebuild it. So a single
`RobotSpeakerTrack` (`dimos/robot/unitree/robot_speaker_track.py`) is attached
once and lives for the whole session: it emits digital silence when idle and the
current clip's frames while speaking, and **never** raises end-of-file. Each new
utterance just swaps the track's internal source — the sender keeps running.

## The microphone gate (why voice-activity detection)

The Go2 mic is a **continuous** ~50 fps stream that never stops — unlike the
browser's push-to-talk source it replaced. Feeding it frame-by-frame into Whisper
would transcribe every ~20 ms fragment, saturate the CPU, and flood the agent
with blank/hallucinated turns. `VoiceActivityRecorder`
(`dimos/stream/audio/node_vad_recorder.py`) sits in front of Whisper on the robot
branch only: it buffers audio once speech begins (with a short pre-roll) and emits
**one clip per utterance** after trailing silence. Speech is detected against an
**adaptive noise floor** (a frame is speech only when it rises a set ratio above
the tracked ambient level), so it works across rooms and mic gains without a fixed
threshold. Blank transcriptions are also dropped before reaching `/human_input`.

The robot's firmware wake-word ("Hey Benben") runs independently — the WebRTC
audio channel streams the raw mic to DimOS in parallel, so no wake word is needed
on the DimOS side; just speak and pause.

## Config knobs

| Setting | Where | Effect |
|---------|-------|--------|
| `force_local_audio` | `GlobalConfig` | Force both directions to host-local audio (debug override) |
| `microphone` | `ConnectionConfig` (`dimos/robot/unitree/go2/connection.py`) | Disable the Go2 mic stream while keeping the speaker (`microphone: false`) |
| `speech_rms_threshold`, `noise_floor_ratio`, `silence_duration`, `min_speech_duration` | `VoiceActivityRecorder` | Tune when speech starts/stops and which blips are ignored |

## Key files

- `dimos/agents/skills/speak_skill.py` — the `speak` skill and robot-speaker routing
- `dimos/robot/unitree/robot_speaker_track.py` — persistent outbound speaker track
- `dimos/robot/unitree/connection.py` — `play_audio_track` (speaker) and `audio_stream` (mic) on the WebRTC connection
- `dimos/agents/web_human_input.py` — STT source selection and utterance gating
- `dimos/stream/audio/node_vad_recorder.py` — voice-activity utterance recorder
- `dimos/stream/audio/resample.py` — 48 kHz stereo → 16 kHz mono for Whisper


# Usage

## Use DimOS as a Library

See below a simple robot connection module that sends streams of continuous `cmd_vel` to the robot and receives `color_image` to a simple `Listener` module. DimOS Modules are subsystems on a robot that communicate with other modules using standardized messages.

```py skip
import threading, time, numpy as np
from dimos.core.coordination.blueprints import autoconnect
from dimos.core.core import rpc
from dimos.core.module import Module
from dimos.core.stream import In, Out
from dimos.msgs.geometry_msgs import Twist
from dimos.msgs.sensor_msgs import Image, ImageFormat

class RobotConnection(Module):
    cmd_vel: In[Twist]
    color_image: Out[Image]

    @rpc
    def start(self):
        threading.Thread(target=self._image_loop, daemon=True).start()

    def _image_loop(self):
        while True:
            img = Image.from_numpy(
                np.zeros((120, 160, 3), np.uint8),
                format=ImageFormat.RGB,
                frame_id="camera_optical",
            )
            self.color_image.publish(img)
            time.sleep(0.2)

class Listener(Module):
    color_image: In[Image]

    @rpc
    def start(self):
        self.color_image.subscribe(lambda img: print(f"image {img.width}x{img.height}"))

if __name__ == "__main__":
    autoconnect(
        RobotConnection.blueprint(),
        Listener.blueprint(),
    ).build().loop()
```

## Blueprints

Blueprints are instructions for how to construct and wire modules. We compose them with
`autoconnect(...)`, which connects streams by `(name, type)` and returns a `Blueprint`.

Blueprints can be composed, remapped, and have transports overridden if `autoconnect()` fails due to conflicting variable names or `In[]` and `Out[]` message types.

A blueprint example that connects the image stream from a robot to an MCP-backed LLM agent for reasoning and action execution.
```py skip
from dimos.core.coordination.blueprints import autoconnect
from dimos.core.transport import LCMTransport
from dimos.msgs.sensor_msgs import Image
from dimos.robot.unitree.go2.connection import go2_connection
from dimos.agents.mcp.mcp_client import McpClient
from dimos.agents.mcp.mcp_server import McpServer

blueprint = autoconnect(
    go2_connection(),
    McpServer.blueprint(),
    McpClient.blueprint(),
).transports({("color_image", Image): LCMTransport("/color_image", Image)})

# Run the blueprint
if __name__ == "__main__":
    blueprint.build().loop()
```

## Library API

- [Modules](docs/usage/modules.md)
- [LCM](docs/usage/lcm.md)
- [Blueprints](docs/usage/blueprints.md)
- [Transports](docs/usage/transports/index.md) — LCM, SHM, DDS, ROS 2
- [Data Streams](docs/usage/data_streams/README.md)
- [Configuration](docs/usage/configuration.md)
- [Visualization](docs/usage/visualization.md)

## Demos

<img src="assets/readme/dimos_demo.gif" alt="DimOS Demo" width="100%">

# Development

## Develop on DimOS

```sh skip
export GIT_LFS_SKIP_SMUDGE=1
git clone https://github.com/dimensionalOS/dimos.git
cd dimos

# Run the default test suite (uv run syncs deps on demand; --all-groups
# only needed for self-hosted tests / mypy — see docs/development/testing.md)
uv run pytest --numprocesses=auto dimos
```


## Multi Language Support

Python is our glue and prototyping language, but we support many languages via LCM interop.

Check our language interop examples:
- [C++](examples/language-interop/cpp/)
- [Lua](examples/language-interop/lua/)
- [TypeScript](examples/language-interop/ts/)
