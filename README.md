# ADI DataX&trade; — 10BASE-T1L Industrial Reference Design

A desktop control panel for the Analog Devices DataX&trade; 10BASE-T1L
single-pair Ethernet reference design. It discovers the boards on the T1L
network, verifies each one's command port, and plots live temperature from the
CN0575 (ADT75) sensor node.

![The control panel in light theme](docs/screenshot-light.png)

<details>
<summary>Dark theme</summary>

![The control panel in dark theme](docs/screenshot-dark.png)
</details>

This is the trimmed companion app: connectivity plus the CN0575 only. The
servo, colour-sensor, SWIOT1L fan and ADXL355 vibration panels live in the full
reference design app.

## What it does

- **Network links** — ICMP ping and a TCP connect check against each board's
  command port, with per-board state and a fleet roll-up in the header.
- **Sensor & telemetry** — the current ADT75 reading as a hero figure, running
  minimum / average / maximum / count, a trend chart, and a table of recent
  samples so every plotted value is also readable as a number.
- **Activity** — a timestamped log of every request and response, colour-coded
  by outcome.
- **Configuration** — board names and addresses, the sensor address and the
  port are editable from the header's *Configure* button, applied without a
  restart and saved to JSON.

## Requirements

- **Linux.** The ICMP check shells out to `ping` with iputils flags
  (`-c count`, `-W timeout` in seconds). BSD/macOS read `-W` as *milliseconds*
  and Windows spells the flags `-n`/`-w`, so those platforms need a branch in
  `ping_host()` that is deliberately not there. The TCP check and the sensor
  readout are portable; only ICMP is not.
- **Python 3.7 or newer** with `tkinter`. 3.7 is the floor because the board
  order you configure is the insertion order of a dict, which is only
  guaranteed from 3.7 on. Some distributions package Tk separately:
  ```sh
  sudo apt install python3-tk        # Debian / Ubuntu
  ```
- **`matplotlib`** for the temperature trend chart and **`Pillow`** for sharp
  logo scaling. Install both:

  ```sh
  pip3 install -r requirements.txt
  ```

Both are needed only by the control panel in `RPI_T1LPSE/`. The CN0575
command server in `RPI_CN0575/` needs nothing beyond the standard library on
a normal Kuiper setup — the one exception is `smbus2`, and only if the sensor
has to be read over direct I²C. See *Deploying to the hardware* below.

## Getting started

| Machine | Image | Runs |
|---|---|---|
| RPi 5 + AD-RPI-T1L-PSE shield | **ADI Kuiper Linux** | `RPI_T1LPSE/datax-industrial.py` — the GUI |
| RPi 4 + EVAL-CN0575-RPIZ | **ADI Kuiper Linux** | `RPI_CN0575/cn0575_state_machine.py` — the command server |

### 1. Flash the SD cards

Both cards get **ADI Kuiper Linux**. It is the distribution that carries the
ADI driver stack and device-tree overlays, and each node depends on a
different one of them: the T1L PSE shield needs its overlay to bring the
10BASE-T1L PHY up at all, and without it there is no T1L link for anything
else to run over. The CN0575 node needs its overlay to expose the ADT75 as an
IIO device.

Download the image from ADI's wiki, under *Resources → Tools & Software →
Linux Software → ADI Kuiper Linux*, and write it to both cards. Raspberry Pi
Imager ("Use custom" → select the `.img`) is the easiest route; balenaEtcher
or `dd` work equally well. Decompress the archive first if it ships
compressed.

#### Enable the device-tree overlays

Add the line for that node to `/boot/config.txt` and **reboot** — the change
takes effect at boot, not immediately. On newer images the file lives at
`/boot/firmware/config.txt` instead.

| Node | Line to add |
|---|---|
| RPi + AD-RPI-T1L-PSE shield | `dtoverlay=rpi-tl1pse-class12` |
| RPi + EVAL-CN0575-RPIZ | `dtoverlay=rpi-cn0575` |

`class12` selects the PoDL power class the PSE advertises, so this is also the
line to revisit if the shield is meant to source a different class.

#### Verify before going further

On the T1L PSE node, confirm the PHY came up and has a link:

```sh
ip link                        # the T1L interface should be present and UP
```

On the CN0575 node, confirm the sensor is bound to a driver:

```sh
ls /sys/bus/iio/devices/
cat /sys/bus/iio/devices/iio:device0/name     # expect: adt75
```

If a device reports `adt75`, the server's preferred read path will work. If
nothing appears there, check for `/sys/class/hwmon/hwmon*/temp1_input`, which
is the second path it tries; if neither exists, the sensor can still be read
over direct I²C at address `0x48` with `smbus2` installed. All three are
described under *The CN0575 command server*.

### 2. Clone the repository on both machines

The same clone on each — the control panel host and the CN0575 node:

```sh
git clone https://github.com/constmonica/datax-industrial-reference-design.git
cd datax-industrial-reference-design
```

On the **control panel host**, install its two dependencies:

```sh
pip3 install -r requirements.txt
```

The **CN0575 node** needs no `pip3` step at this point; its server is standard
library only unless it has to fall back to direct I²C, which step 4 covers.

### 3. Check the control panel in demo mode

On the control panel host:

```sh
python3 RPI_T1LPSE/datax-industrial.py --demo
```

Demo mode needs no hardware and no network — every panel is driven by
synthetic data. It is the quickest way to confirm Python, `tkinter` and both
dependencies are in place before any addresses are involved. You should get
the window shown at the top of this README, with a live trend chart.

### 4. Start the command server on the CN0575 node

On the EVAL-CN0575-RPIZ:

```sh
cd RPI_CN0575
python3 cn0575_state_machine.py
```

It prints each request and response, so you can watch the control panel's
`READ_TEMP` calls arrive. Only this one file is needed on that Pi — cloning
the whole repository in step 2 is merely the easiest way to get it there.

Install `smbus2` **only** if step 1 showed the sensor is reachable through
neither IIO nor hwmon, and it has to fall back to direct I²C:

```sh
pip3 install smbus2
```

This server has to be running before the control panel can read a
temperature. If it is not, the CN0575 card still shows as reachable — ICMP and
the TCP connect check are answered by the Pi itself, not by this script — but
no reading appears. That is the intended behaviour, not a fault to chase.

For a bench you return to, run it under systemd rather than a terminal, so it
survives a reboot and a closed SSH session.

### 5. Point the app at your boards

Restart the control panel without `--demo` and set your real addresses. Either
press **Configure** in the header and type them in — see *Editing it from the
app* — or pass them on the command line:

```sh
python3 RPI_T1LPSE/datax-industrial.py \
    --board "APARD32690 #1=10.0.0.5" \
    --board "APARD32690 #2=10.0.0.6" \
    --cn0575 10.0.0.9 \
    --port 10000
```

Press **Test all**. Each board should go green on both checks, and the CN0575
card should return a temperature from the server started in step 4.

The APARD32690 and SWIOT1L nodes need nothing from this repository — the app
only opens a TCP connection to each one to prove it is alive. See *What the
boards have to implement* for that contract.

## Configuring your network

Nothing is hard-coded into the UI. The shipped defaults describe the reference
setup, and anyone bringing this up on their own bench will be on a different
subnet, so all three network values are user-supplied.

| Flag | Meaning |
|---|---|
| `--board NAME=IP` | A board to monitor. Repeatable. **Any use replaces the built-in list** rather than adding to it, so you never end up pinging the shipped defaults alongside your own. |
| `--cn0575 IP` | The CN0575 / ADT75 sensor node. |
| `--port N` | TCP command port, shared by every board. |
| `--config FILE` | The same three settings as JSON. |


For a bench you return to, keep a config file:

```json
{
  "boards": {
    "APARD32690 #1": "10.0.0.5",
    "APARD32690 #2": "10.0.0.6",
    "SWIOT1L":       "10.0.0.7"
  },
  "cn0575": "10.0.0.9",
  "port": 10000
}
```

```sh
python3 RPI_T1LPSE/datax-industrial.py --config bench.json
```

Precedence is **command line > config file > defaults**, so a saved config can
be kept for the bench and overridden for a single run:

```sh
python3 RPI_T1LPSE/datax-industrial.py --config bench.json --port 10001
```

### Editing it from the app

The header's **Configure** button opens the same three settings as a form:
board names and addresses, add and remove boards, the sensor address and the
port. Values are held to the same validation as the command line, and nothing
is written until all of them pass — an address that will not resolve or a
port outside 1-65535 marks its own field and says why.

![The configuration dialog](docs/screenshot-config.png)

Saving applies the change to the running window — no restart. Renaming a board
retitles its card and keeps its check results; changing an address drops that
card back to *Not checked*, because a latency measured against the previous
host says nothing about the new one.

Save writes to whichever file the session is using:

| Launched with | Save writes to |
|---|---|
| `--config FILE` | that file |
| no `--config` | `$XDG_CONFIG_HOME/datax-industrial/boards.json`, or `~/.config/datax-industrial/boards.json` |

That default is also read at startup when it exists, which is how edits come
back on the next launch. It is absent on a first run, and that is not an
error — unlike a `--config` file you named that is not there, which is
reported as the typo it probably is.

Command-line flags still win over both, so `--board` overrides a saved file
for that run. The edit you then save is the full board list as the form shows
it, flags included.

## All options

| Option | Default | Notes |
|---|---|---|
| `--demo` | off | Synthetic data for every panel; no hardware needed. |
| `--theme {light,dark}` | `light` | Switchable at runtime too. |
| `--board NAME=IP` | three APARD/SWIOT1L boards | Repeatable; replaces the default list. |
| `--cn0575 IP` | `192.168.10.2` | Sensor node address. |
| `--port N` | `10000` | TCP command port. |
| `--config FILE` | `~/.config/datax-industrial/boards.json` | JSON with `boards` / `cn0575` / `port`. Also where *Configure* saves. |
| `--ping-count N` | `1` | ICMP echo count per check. |
| `--ping-timeout S` | `1.0` | ICMP echo timeout, seconds. |
| `--ui-scale F` | autodetect | Override the UI density factor. |
| `--custom-titlebar` | off | See *Window chrome* below. |

### Keyboard

| Key | Action |
|---|---|
| `Ctrl+R` / `F5` | Test all boards |
| `Ctrl+T` | Toggle light / dark theme |
| `Ctrl+L` | Clear the activity log |
| `Return` | Save — in the Configure dialog |
| `Esc` | Cancel — in the Configure dialog |

## What the boards have to implement

The app is a plain TCP client. Each board listens on the command port
(`--port`, default 10000) and answers one newline-terminated ASCII command per
connection:

| Request | Response | Used by |
|---|---|---|
| *(connect, then close)* | — | The TCP connect check; opening the socket is the whole test. |
| `READ_TEMP\n` | `TEMP:<celsius>\n`, e.g. `TEMP:23.4` | The CN0575 sensor card. |

Anything that is not a `TEMP:` reply with a parsable float is ignored rather
than displayed, so a board that is up but not yet answering correctly shows as
reachable with no reading — not as a crash or a bogus value. That is also what
makes the server's own error replies safe: they are reported, not plotted.

### The CN0575 command server

`RPI_CN0575/cn0575_state_machine.py` is a working implementation of the above,
for the EVAL-CN0575-RPIZ — see *Deploying to the hardware* for getting it onto
the Pi and running it.

It listens on `0.0.0.0:10000`, matching the app's default port, and serves one
command per connection, logging each request and response to stdout.

| Request | Response |
|---|---|
| `READ_TEMP` | `TEMP:23.4` |
| `READ_TEMP`, sensor unreadable | `ERR:SENSOR_FAIL` |
| anything else | `ERR:UNKNOWN_CMD` |

Commands are matched case-insensitively after stripping whitespace.

Reading the ADT75 is attempted three ways, in order, so the same script works
across driver setups:

1. **IIO sysfs** — scans `/sys/bus/iio/devices/iio:device*/name` for `adt75`
   and converts `in_temp_raw × in_temp_scale`. This is the path you get on
   Kuiper Linux with the `rpi-cn0575` overlay.
2. **hwmon** — the first `/sys/class/hwmon/hwmon*/temp1_input`, in
   millidegrees.
3. **Direct I²C** — `smbus2` against address `0x48`, decoding the ADT75's
   12-bit two's-complement register at 0.0625 °C per LSB. This is the only
   step that needs a package installed (`pip3 install smbus2`); the first two
   use the standard library alone.

If all three fail it answers `ERR:SENSOR_FAIL`, which the app logs and shows
as a reachable board with no reading.

Two limits worth knowing before you build on it: it is single-threaded with a
backlog of one, so it serves one client at a time, and it reads once per
connection rather than framing a stream. That suits this app's one-shot
request pattern and is not a general-purpose server.

## Architecture

The repository is organised by deployment target: each `RPI_*` directory is
what you copy onto one Raspberry Pi.

| Path | Role |
|---|---|
| `RPI_T1LPSE/datax-industrial.py` | The control panel: network helpers, panels, window. Runs on the host or the T1L PSE Pi. |
| `RPI_T1LPSE/design_system.py` | Visual language — palette, light/dark themes, spacing scale, `Button`, `Card`, `TextField`, `ThemeMixin`. Imported by the app, and the two files must stay together. |
| `RPI_T1LPSE/assets/` | The Analog Devices logo (1x and 2x), resolved relative to the app file. |
| `RPI_CN0575/cn0575_state_machine.py` | The board-side command server for the EVAL-CN0575-RPIZ. See *The CN0575 command server* below. |
| `docs/` | Screenshots used by this README. |


## Troubleshooting

**Every board shows "Unreachable".** Check you are on the T1L subnet and that
your addresses are right (`--board`, `--cn0575`). 

**No chart.** `matplotlib` is not installed; the install step was missed or
failed. Run `pip3 install -r requirements.txt` and restart. The rest of the
sensor card — readout, statistics and sample table — works meanwhile.

**My saved edits did not come back.** Command-line flags outrank the saved
file, so launching with `--board` or `--port` overrides it for that run — the
window shows the flags, not what you saved. Drop the flags, or pass
`--config FILE` and save into that file instead.

**Configure will not save.** The dialog reports the reason on its own error
line rather than closing. A read-only home or an unwritable `--config` path
is the usual cause; the previous file is left intact either way, because the
new one is written alongside it and moved into place only once complete.

**Configure is greyed out or does nothing.** It refuses to open while checks
are still in flight — wait for *Test all* to finish. Reconfiguring mid-check
would land results on a card that had already changed address.

**The UI is too large or too small.** Pass `--ui-scale`, e.g. `--ui-scale 1.0`
for native sizing or `--ui-scale 2` on a high-density panel the X server
reports as 96 DPI.

**It opens across two monitors.** It shouldn't — the window is sized to the
monitor it opens on via `xrandr`. If `xrandr` is unavailable the app falls back
to the full X screen, which on a multi-head desktop is every monitor side by
side.
