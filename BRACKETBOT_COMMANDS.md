# BracketBot command reference

Updated September 12, 2026. Companion: [Bouncer robot plan](BOUNCER_ROBOT_PLAN.md).

## Scope and source of truth

This reference documents the commands and Python interfaces in the inspected public repositories. It is not a record of successful execution on our robot.

- Quickstart revision: `a9f8d817ca94b1c04749271f8147dccfbe8a9462` (June 16, 2025).
- MCP revision: `939d56980998f14dc4782c4069ed4e9a652f9bd7`.
- Temporary inspection copies: `/tmp/bracketbot-quickstart` and `/tmp/bracketbot-mcp`. These may disappear; use the pinned source links below to recover the code.
- Actual robot IP, username, attached arm hardware, arm controller protocol, and contact sensors are not yet recorded.
- The user confirms the intended robot uses **two arms for a physical pat-down**. The reviewed quickstart does not supply a two-arm control API, inverse kinematics, or force-controlled pat-down implementation.

Commands below run on the Raspberry Pi unless explicitly described otherwise. They are reference entries, not a batch script to execute in order. Hardware tests can move actuators or play audio. The upstream setup changes the OS; it is not a macOS setup script.

## 1. Connect and set up

From the development computer, replacing the placeholders with the robot's actual details:

```bash
ssh ROBOT_USER@ROBOT_IP
```

Upstream initial setup on the Pi:

```bash
git clone https://github.com/BracketBotCapstone/quickstart.git ~/quickstart
cd ~/quickstart/setup
bash setup_os.sh
```

The setup script creates `~/quickstart-venv`, configures shell activation, installs dependencies, and changes SSH, serial/I2C/SPI boot configuration, audio, LEDs, MQTT, and hotspot settings. It automatically proceeds through steps after short countdowns. Inspect it before rerunning on an already configured robot.

For an existing installation:

```bash
source ~/quickstart-venv/bin/activate
cd ~/quickstart
```

Motor calibration:

```bash
cd ~/quickstart/setup
python3 calibrate_drive.py
```

This physically moves wheels. Upstream specifies a flat, open floor area with at least one metre of clearance. It configures/calibrates the ODrive, tests each wheel's direction using IMU yaw, and writes `~/quickstart/lib/motor_dir.json`. It is wheel calibration, not arm calibration.

Stereo calibration:

```bash
cd ~/quickstart/setup
python3 calibrate_stereo_camera.py
```

Produces `lib/stereo_calibration_fisheye.yaml` through the script's capture/calibration workflow. Follow the script's calibration-target requirements; simply launching it does not establish valid calibration.

Optional setup scripts, only for matching hardware:

```bash
bash ~/quickstart/setup/extras/setup_realsense.sh
bash ~/quickstart/setup/extras/setup_hardware_pwm.sh
```

`setup/extras/create_custom_boot_service.sh` is a template to copy and edit for a custom boot service. Do not auto-start contact motion on boot.

## 2. Manual base driving

```bash
cd ~/quickstart/examples
python3 example_wasd.py
```

| Key | Wheel commands | Result after correct direction calibration |
|---|---|---|
| W | left positive, right positive | Forward |
| S | left negative, right negative | Reverse |
| A | left negative, right positive | Turn left in place |
| D | left positive, right negative | Turn right in place |
| Release any key | both zero | Stop commanded wheel velocity |
| Q | exit listener, then cleanup | Quit |

The example uses `SPEED = 0.375` metres/second. This is its configured value, not a certified operating limit. Its setup disables both motor watchdogs. Keyboard cleanup is not a physical emergency stop and cannot cover power loss, a hung process, or every connection failure.

## 3. ODrive wheel controller

Import: `from lib.odrive_uart import ODriveUART` when the quickstart root is on the Python path.

Constructor defaults:

```python
motor = ODriveUART(
    port='/dev/ttyAMA1',
    left_axis=0,
    right_axis=1,
    dir_left=None,
    dir_right=None,
)
```

The connection is 115200 baud, 8 data bits, no parity, one stop bit, with a one-second read timeout. Missing directions are loaded from `lib/motor_dir.json`; the class falls back to positive directions if unavailable. Several examples explicitly open the configuration file first and therefore fail if it is missing.

**Implementation issue:** the module opens a class-level serial connection at import time and another in the constructor. Importing it on a laptop can fail immediately. Supplying a different constructor port does not avoid the import-time default-port access. Use a hardware adapter and mock implementation for laptop development.

### Public method families and meaning

For the following names, `SIDE` means an actual `_left` or `_right` suffix, e.g. `start_left()`.

| Method family | Meaning / units |
|---|---|
| `start_SIDE()` | Request closed-loop control, state 8. Does not itself select the desired control mode. |
| `enable_velocity_mode_SIDE()` | Velocity control with passthrough input. |
| `enable_velocity_ramp_mode_SIDE()` | Velocity control with ramped input. |
| `set_velocity_ramp_rate_SIDE(rate)` | Ramp rate in turns/second squared. |
| `enable_torque_mode_SIDE()` | Torque control with passthrough input. |
| `set_speed_mps_SIDE(mps)` | Signed wheel surface speed in metres/second; assumes 165 mm wheel diameter. |
| `set_speed_rpm_SIDE(rpm)` | Signed wheel speed in revolutions/minute. |
| `set_torque_nm_SIDE(nm)` | Torque request; implementation adds a signed 0.05 Nm bias, including for input zero. Not an arm force command. |
| `get_speed_rpm_SIDE()` | Encoder velocity converted to signed RPM. |
| `get_position_turns_SIDE()` | Signed cumulative encoder position in turns. |
| `get_pos_vel_SIDE()` | Pair: signed position in turns and velocity in RPM. |
| `stop_SIDE()` | Writes zero velocity and zero torque; does not request idle or cut power. |
| `check_errors_SIDE()` | Returns an error flag; treats an unparseable response as an error. |
| `get_errors_SIDE()` | Broken in this revision: calls a missing `get_errors(axis)` method. |
| `clear_errors_SIDE()` | Clears axis error and requests closed-loop control again; can re-enable the axis. |
| `enable_watchdog_SIDE()` | Enables watchdog; timeout and feeding behavior need integration. |
| `disable_watchdog_SIDE()` | Disables watchdog. Many upstream demos do this. |
| `set_watchdog_timeout(seconds)` | Sets timeout on both axes. |
| `has_errors()` | Checks axes 0 and 1, regardless of constructor axis mapping. |
| `dump_errors()` | Prints axis, encoder, controller, and motor error information. |
| `get_bus_voltage()` | Returns formatted voltage as a string with one decimal place. |
| `send_command(command)` | Low-level UART ASCII command; reads a reply for commands beginning with `r` or `f`. |

The driver does not lock UART transactions. Concurrent access from drive, odometry, and status polling can interleave responses. Give one process exclusive ownership and serialize operations.

The wheel conversion is `turns_per_second = speed_mps / (pi * 0.165)`, followed by the configured direction sign. Positive values should mean physical forward only after calibration.

For a future base adapter, differential-drive kinematics are `left = v - omega*b/2`, `right = v + omega*b/2`, where `b` is measured wheel separation and positive `omega` means turning left. This is a proposed adapter convention, not an existing `ODriveUART.drive(v, omega)` method.

### Voltage CLI

From the Pi home directory with its environment active:

```bash
python -m quickstart.lib.odrive_uart
python -m quickstart.lib.odrive_uart --raw
```

The module parses `--port` but does not pass it into its constructor in this revision. `reset_odrive()` only prints a message; its GPIO reset code is commented out. Neither is a reliable recovery mechanism without fixes.

### Complete wheel-driver signature inventory

The following inventory is extracted from the pinned source so exact spellings and generic axis methods are preserved.

```python
ODriveUART.__init__(self, port='/dev/ttyAMA1', left_axis=0, right_axis=1, dir_left=None, dir_right=None)
ODriveUART.send_command(self, command: str)
ODriveUART.get_errors_left(self)
ODriveUART.get_errors_right(self)
ODriveUART.has_errors(self)
ODriveUART.dump_errors(self)
ODriveUART.enable_torque_mode_left(self)
ODriveUART.enable_torque_mode_right(self)
ODriveUART.enable_torque_mode(self, axis)
ODriveUART.enable_velocity_mode_left(self)
ODriveUART.enable_velocity_mode_right(self)
ODriveUART.enable_velocity_mode(self, axis)
ODriveUART.enable_velocity_ramp_mode_left(self)
ODriveUART.enable_velocity_ramp_mode_right(self)
ODriveUART.enable_velocity_ramp_mode(self, axis)
ODriveUART.set_velocity_ramp_rate_left(self, ramp_rate)
ODriveUART.set_velocity_ramp_rate_right(self, ramp_rate)
ODriveUART.set_velocity_ramp_rate(self, axis, ramp_rate)
ODriveUART.start_left(self)
ODriveUART.start_right(self)
ODriveUART.start(self, axis)
ODriveUART.set_speed_rpm_left(self, rpm)
ODriveUART.set_speed_rpm_right(self, rpm)
ODriveUART.set_speed_rpm(self, axis, rpm, direction)
ODriveUART.set_speed_mps_left(self, mps)
ODriveUART.set_speed_mps_right(self, mps)
ODriveUART.set_torque_nm_left(self, nm)
ODriveUART.set_torque_nm_right(self, nm)
ODriveUART.set_torque_nm(self, axis, nm, direction)
ODriveUART.get_speed_rpm_left(self)
ODriveUART.get_speed_rpm_right(self)
ODriveUART.get_speed_rpm(self, axis, direction)
ODriveUART.get_position_turns_left(self)
ODriveUART.get_position_turns_right(self)
ODriveUART.get_position_turns(self, axis, direction)
ODriveUART.get_pos_vel_left(self)
ODriveUART.get_pos_vel_right(self)
ODriveUART.get_pos_vel(self, axis, direction)
ODriveUART.stop_left(self)
ODriveUART.stop_right(self)
ODriveUART.stop(self, axis)
ODriveUART.check_errors_left(self)
ODriveUART.check_errors_right(self)
ODriveUART.check_errors(self, axis)
ODriveUART.clear_errors_left(self)
ODriveUART.clear_errors_right(self)
ODriveUART.clear_errors(self, axis)
ODriveUART.enable_watchdog_left(self)
ODriveUART.enable_watchdog_right(self)
ODriveUART.enable_watchdog(self, axis)
ODriveUART.disable_watchdog_left(self)
ODriveUART.disable_watchdog_right(self)
ODriveUART.disable_watchdog(self, axis)
ODriveUART.set_watchdog_timeout(self, timeout)
ODriveUART.get_bus_voltage(self)
reset_odrive()
```

## 4. Camera, IMU, and path helpers

### `lib/camera.py`

```python
RealsenseCamera.__init__(self)
RealsenseCamera.get_frames(self)
RealsenseCamera.release(self)
USBCamera.__init__(self, index=0)
USBCamera.get_frame(self)
USBCamera.release(self)
StereoCamera.__init__(self, device_id=0, scale=1.0)
StereoCamera.set_scale(self, scale)
StereoCamera.get_scale(self)
StereoCamera.get_stereo(self, scale=None)
StereoCamera.release(self)
```

### `lib/imu.py`

```python
FilteredMPU6050.__init__(self)
FilteredMPU6050.calibrate(self)
FilteredMPU6050.get_orientation(self)
FilteredMPU6050.read_sensor(self)
FilteredMPU6050.update(self)
FilteredMPU6050.quat_rotate(self, q, v)
```

### `examples/example_drivepath.py`

```python
RobotDriver.__init__(self, motor)
RobotDriver.update_start_position(self)
RobotDriver.drive_distance(self, distance, speed=0.2)
RobotDriver.turn_degrees(self, degrees, turn_speed=0.2)
RobotDriver.drive_circle(self, radius=0.5, speed=0.2, duration=10)
RobotDriver.drive_square(self, side_length=1.0, speed=0.2)
```

### `examples/example_localization.py`

```python
DifferentialDriveOdometry.reset(self)
DifferentialDriveOdometry.update(self, left_turns: float, right_turns: float)
OdometryThread.__init__(self, odrv: ODriveUART, log_to_rerun: bool=False)
OdometryThread.pose(self)
OdometryThread.path(self)
OdometryThread.run(self)
OdometryThread.stop(self)
```

Camera notes:

- `StereoCamera` requests a 2560x720 MJPEG frame at 30 FPS over Linux V4L2, then splits it into two 1280x720 views. Actual capture rates depend on hardware.
- `get_stereo()` returns `(left, right)` or `(None, None)` on failed capture. Frames are OpenCV arrays, with optional scaling.
- `USBCamera` wraps a single OpenCV capture and returns one frame.
- `RealsenseCamera` currently refers to `rs` while its import is commented out. The standalone `tests/test_realsense.py` imports the RealSense package separately.
- Release camera resources with `release()`.

IMU notes:

- `FilteredMPU6050` uses I2C, remaps sensor axes, loads or measures gyro bias, and uses the bundled Madgwick filter.
- Call `calibrate()` before relying on orientation updates. Bias is stored as `gyro_bias.txt` relative to the working directory.
- `get_orientation()` returns pitch, roll, yaw in degrees. It is not an arm joint-angle sensor or a contact sensor.

Path and localisation notes:

- `RobotDriver.drive_distance()` monitors average absolute encoder travel. Its negative-distance handling is not a reliable reverse-driving interface; validate/fix before use.
- `turn_degrees()` uses positive angles for right/clockwise turns.
- Path helpers assume 0.4 m wheel separation; localisation assumes 0.425 m. Measure our hardware and consolidate configuration.
- `DifferentialDriveOdometry.update()` integrates encoder deltas into `(x, z, yaw)`, where x is forward, z is left, and yaw is radians. This is wheel odometry, not SLAM.
- `OdometryThread` exposes `pose` and `path` properties, polls at a configured 5 Hz, and can log to Rerun. It has a hardcoded viewer address and references an absent `lib/Bracketbot.stl` asset.

## 5. All example launch commands

Run from `~/quickstart/examples` with the Pi environment active. Additional dependencies, calibration, models, and addresses may be needed; base OS setup does not install everything used by every example.

| Command | What it does / integration note |
|---|---|
| `python3 example_wasd.py` | Keyboard wheel control; movement. |
| `python3 example_drivepath.py` | Executes distance, turn, circle, and square demos; movement. |
| `python3 example_follow.py` | YOLO person following; movement. Chooses the largest person box, uses its width as a distance proxy and horizontal offset for steering. It does not recognise concealed objects. |
| `python3 example_facetime.py` | Flask/Socket.IO browser teleoperation, video, and audio; browser served on port 8080. Keyboard events can move the base. |
| `python3 example_yolo.py` | YOLO11n object detection with NCNN export/inference. |
| `python3 example_segmentation.py` | YOLO11s segmentation; writes annotated frames. |
| `python3 example_depth.py` | Calibrated stereo depth/point-cloud visualisation in Rerun. |
| `python3 example_depth_fast.py` | Alternative stereo depth viewer; inspect options and performance on the Pi. |
| `python3 example_localization.py` | Wheel odometry and Rerun display. |
| `python3 example_rerun.py` | Stereo image streaming with viewer IP selection. |
| `python3 example_whisper.py` | Local whisper.cpp transcription; first run may install/build dependencies and download a model. |
| `python3 example_kokoro.py` | Local text-to-speech example using Kokoro. |
| `python3 example_realtime.py` | Realtime voice conversation example using an API key and network access. Uses historical API/model settings that need checking before integration. |

**Person-follower fault:** if camera capture fails, its loop returns without zeroing the previous wheel command. Do not reuse it as the bouncer's motion controller without correcting stale-command behavior. Its watchdogs are disabled.

## 6. Hardware test commands

Run from `~/quickstart` using `python3 tests/FILENAME`:

| Filename | Purpose |
|---|---|
| `test_camera.py` | USB camera inspection. |
| `test_realsense.py` | RealSense colour/depth capture. |
| `test_imu_orientation.py` | IMU orientation check. |
| `test_led_strip.py` | LED colour cycle over SPI. |
| `test_microphone.py` | Microphone recording check. |
| `test_microphone_with_led.py` | Microphone level displayed on LEDs. |
| `test_speaker.py` | Audio playback. |
| `test_speaker_with_led.py` | Audio playback with LED visualisation. |
| `test_transcription.py` | Transformers speech-recognition demo. |
| `test_tts.py` | Text-to-speech test. |
| `test_tts_with_led.py` | ElevenLabs speech with LED activity. |
| `test_rerun.py` | Rerun connection/logging check. |
| `test_servos.py` | Single hardware PWM channel servo movement; not a two-arm controller. |
| `test_system.py` | Empty file in this revision; not a system verification suite. |

These are hardware/demo scripts, not an isolated automated unit-test suite. Do not indiscriminately run `pytest` against physical hardware.

### Servo and LED primitives present upstream

The servo example constructs `HardwarePWM(pwm_channel=0, hz=50, chip=2)` for GPIO12, starts it, and calls `change_duty_cycle()` with 5.0 and 10.5 percent. At 50 Hz those are 1.0 and 2.1 ms pulses. The example's final `pwm1.stop()` is commented out.

Those are raw PWM values, **not** degrees, Cartesian positions, or force limits. They must not be copied as pat-down poses. Our actual two-arm hardware may use an entirely different controller.

The LED example constructs `Pi5Neo('/dev/spidev0.0', 15, 800)` and calls `fill_strip(r, g, b)` followed by `update_strip()`. Device, strip length, and wiring need to match the actual robot.

## 7. MCP commands and their HTTP dependencies

The MCP repository is a wrapper around a separate robot HTTP service. It builds URLs as `http://localhost:<port>`. Remote robots therefore require a configured connection mechanism such as a tunnel or an adapted host setting.

**Missing dependency:** the referenced robot-side `example_server.py` is absent from the inspected quickstart tree. `example_facetime.py` uses different routes and is not a drop-in replacement.

| MCP tool | Arguments besides `port=8000` | HTTP call expected by wrapper |
|---|---|---|
| `drive_forward` | `speed=0.2`, `duration=None` | GET `/forward` |
| `drive_backward` | `speed=0.2`, `duration=None` | GET `/backward` |
| `turn_left` | `speed=1.2`, `duration=None` | GET `/left` |
| `turn_right` | `speed=1.2`, `duration=None` | GET `/right` |
| `stop` | none | GET `/stop` |
| `beep` | `frequency=440.0`, `duration=1.0`, `volume=0.5` | GET `/beep` |
| `drive` | `linear_velocity=0.0`, `angular_velocity=0.0`, `duration=None` | POST `/drive` with JSON |
| `robot_status` | none | GET `/` |
| `get_camera_image` | `format="png"`, `quality=90` | GET `/image` |
| `list_available_robots` | no arguments | Checks hardcoded ports 8000 and 8001. |

Forward/backward speeds are described as metres/second; turn speeds as radians/second. Omitted durations mean indefinite motion according to the wrapper descriptions. None of these tools controls an arm.

The resource `robot://info/{port}` returns capability metadata. Its advertised `/image/base64` endpoint is not a separately implemented MCP tool.

The upstream README suggests `uv pip install -e .` in a suitable Python environment. Its direct `server.py` entry point calls `uvicorn.run(mcp.app, ...)` on port 3000; compatibility must be checked against the installed MCP version and the missing robot HTTP server must be supplied. Do not assume this is a working launch recipe for our robot.

## 8. Other library contents

- `lib/madgwickahrs.py`: quaternion maths and Madgwick orientation filtering used by the IMU wrapper.
- `lib/lqr.py`: `LQR_gains(Q_diag, R_diag)` constructs a dynamics model and returns LQR gains using hardcoded historical physical parameters. It is not a complete balancing application or an arm contact controller.
- `lib/vl53l5cx_lib/vl53l5cx.py`: low-level VL53L5CX ranging driver, with initialisation, power, resolution, frequency, start/stop, data-ready and ranging-data methods.
- `lib/vl53l5cx_lib/api.py` and `buffers.py`: supporting constants/buffers for that driver. Presence of code does not confirm the sensor is fitted to our robot.

## 9. Commands we still need for this project

No verified commands are currently available for left/right arm homing, joint positions, Cartesian targets, contact-force readings, force limiting, protective stop, or a dual-arm pat-down sequence. These must come from the actual arm/controller documentation.

Names such as `home_arms`, `execute_contact_step`, and `protective_stop` in the plan are **proposed application interfaces**, not upstream BracketBot commands. Do not wire them to arbitrary PWM values.

## 10. Pinned source links

- [README.md](https://github.com/BracketBotCapstone/quickstart/blob/a9f8d817ca94b1c04749271f8147dccfbe8a9462/README.md)
- [setup/README.md](https://github.com/BracketBotCapstone/quickstart/blob/a9f8d817ca94b1c04749271f8147dccfbe8a9462/setup/README.md)
- [setup/setup_os.sh](https://github.com/BracketBotCapstone/quickstart/blob/a9f8d817ca94b1c04749271f8147dccfbe8a9462/setup/setup_os.sh)
- [setup/calibrate_drive.py](https://github.com/BracketBotCapstone/quickstart/blob/a9f8d817ca94b1c04749271f8147dccfbe8a9462/setup/calibrate_drive.py)
- [setup/calibrate_stereo_camera.py](https://github.com/BracketBotCapstone/quickstart/blob/a9f8d817ca94b1c04749271f8147dccfbe8a9462/setup/calibrate_stereo_camera.py)
- [lib/odrive_uart.py](https://github.com/BracketBotCapstone/quickstart/blob/a9f8d817ca94b1c04749271f8147dccfbe8a9462/lib/odrive_uart.py)
- [lib/camera.py](https://github.com/BracketBotCapstone/quickstart/blob/a9f8d817ca94b1c04749271f8147dccfbe8a9462/lib/camera.py)
- [lib/imu.py](https://github.com/BracketBotCapstone/quickstart/blob/a9f8d817ca94b1c04749271f8147dccfbe8a9462/lib/imu.py)
- [examples/example_drivepath.py](https://github.com/BracketBotCapstone/quickstart/blob/a9f8d817ca94b1c04749271f8147dccfbe8a9462/examples/example_drivepath.py)
- [examples/example_localization.py](https://github.com/BracketBotCapstone/quickstart/blob/a9f8d817ca94b1c04749271f8147dccfbe8a9462/examples/example_localization.py)
- [examples/example_follow.py](https://github.com/BracketBotCapstone/quickstart/blob/a9f8d817ca94b1c04749271f8147dccfbe8a9462/examples/example_follow.py)
- [examples/example_facetime.py](https://github.com/BracketBotCapstone/quickstart/blob/a9f8d817ca94b1c04749271f8147dccfbe8a9462/examples/example_facetime.py)
- [tests/test_servos.py](https://github.com/BracketBotCapstone/quickstart/blob/a9f8d817ca94b1c04749271f8147dccfbe8a9462/tests/test_servos.py)
- [tests/test_led_strip.py](https://github.com/BracketBotCapstone/quickstart/blob/a9f8d817ca94b1c04749271f8147dccfbe8a9462/tests/test_led_strip.py)
- [MCP server.py](https://github.com/BracketBotCapstone/bracketbot-mcp/blob/939d56980998f14dc4782c4069ed4e9a652f9bd7/server.py)
- [MCP README](https://github.com/BracketBotCapstone/bracketbot-mcp/blob/939d56980998f14dc4782c4069ed4e9a652f9bd7/README.md)
