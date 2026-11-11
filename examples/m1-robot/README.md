# Zeroth M1 Robot Integration

This directory is the self-contained M1 integration shipped on the
`feat/m1-robot` branch. It includes the qwen-audio-agent connector, robot audio
gateway, monitoring and motion-control dashboard, vision gateway, deployment
assets, tests, and interface notes.

## Quick start

```bash
git clone --branch feat/m1-robot --single-branch \
  git@github.com:zh-qian2004/qwen-audio-agent.git
cd qwen-audio-agent
npm ci
npm run test:m1-robot
npm run start:m1-robot
```

Copy `examples/m1-robot/m1-robot.config.json` to
`m1-robot.config.local.json` before changing robot addresses, SSH settings, or
ports. The local file is ignored by Git. Never commit model API keys, robot
passwords, SSH private keys, logs, recordings, or runtime data.

Components:

- [`audio_service/`](audio_service/): microphone PCM, TTS, realtime output, and
  playback arbitration.
- [`dashboard/`](dashboard/): ROS state, motion control, sensors, media, and
  Agent status.
- [`vision_service/`](vision_service/): visual processors and normalized
  detection results.
- [`deploy/`](deploy/): robot startup assets and vendor patches.
- [`docs/`](docs/): control-interface and hardware-validation notes.

For architecture, deployment paths, tool semantics, audio behavior, and test
commands, see the [Chinese guide](README_ZH.md).
