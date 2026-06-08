# BetterSocEstimatorGX

A better State-of-Charge (SOC) estimator for Victron GX devices paired with a JK BMS.  
Tested on a **Multiplus II GX** with a 16S LiFePO4 pack, but should work on any Cerbo GX or GX-integrated device.

---

## Table of Contents

1. [Why the JK BMS SOC estimator is bad](#why-the-jk-bms-soc-estimator-is-bad)
2. [How this works](#how-this-works)
3. [Requirements](#requirements)
4. [Installation](#installation)
5. [Usage](#usage)
6. [Configuration](#configuration)
7. [D-Bus paths published](#d-bus-paths-published)
8. [Monitoring & troubleshooting](#monitoring--troubleshooting)
9. [Persistent state](#persistent-state)

---

## Why the JK BMS SOC estimator is bad

The JK BMS ships with a built-in coulomb counter for SOC tracking. In theory this is fine, but it has two critical flaws that compound over time:

### 1. Recalibration only at hard voltage limits (0 % or 100 %)

Coulomb counting always drifts — tiny measurement errors in current sensing accumulate over hundreds of cycles. A good estimator regularly **re-anchors** its SOC against a reliable reference (the open-circuit voltage, or OCV) whenever the battery is at rest and the voltage gives meaningful SOC information.

The JK BMS **only resets its SOC counter when the battery hits the exact configured cut-off voltages** — i.e., at 0 % (under-voltage protection trigger) or 100 % (charge completion at absorption voltage). If your system never fully charges to 100 % (common in solar self-consumption setups) or never fully discharges, the counter can drift for months without ever being corrected. Even a 1 % per-cycle drift becomes 50 % of error after 50 cycles.

### 2. SOH (State of Health) is static or silently stale

The JK BMS advertises an SOH figure, but it does **not appear to update it dynamically** as the battery ages. The installed capacity value used for coulomb counting stays fixed at the factory-rated value. As the battery ages and its real capacity shrinks, the percentage reported becomes progressively more optimistic — the denominator never changes. This means drift gets *worse* over time, not better, because the BMS is still computing `SOC = remaining_Ah / rated_Ah` with an increasingly wrong denominator.

### The combined effect

- Coulomb counting errors accumulate between full charge/discharge cycles.
- Since the battery rarely (or never) hits 0 % or 100 %, there is no recalibration.
- The SOH figure used in the denominator is stale, so even if the BMS tried to improve, its capacity model is wrong.
- Result: **reported SOC drifts further and further from reality** the longer the system runs in a partial state-of-charge (PSOC) cycling regime — exactly what solar/ESS systems do.

---

## How this works

`battery_monitor.py` registers a **virtual battery service** on the Victron D-Bus (`com.victronenergy.battery.virtual`). Venus OS sees it as a second battery monitor you can select in the GX settings. It reads raw data from the JK BMS D-Bus service (voltage, current, temperature, installed capacity) and re-implements the SOC/SOH estimation from scratch.

### Algorithm overview

#### 1. Coulomb counting (always active)

Every second, the script reads the BMS current and integrates it:

```
ΔAh = current_A × efficiency × dt_s / 3600
ΔSOC = ΔAh / effective_capacity × 100
```

`effective_capacity = installed_capacity × SOH`  
This means as SOH degrades, the same Ah change translates to a larger SOC swing — which is physically correct.

A `CHARGE_EFFICIENCY` factor (default `1.00` for LiFePO4) can optionally be applied during charging to model round-trip losses.

#### 2. OCV recalibration (at rest, outside the flat plateau)

LiFePO4 cells have a very flat voltage plateau between roughly 20 % and 90 % SOC — in that range, voltage tells you almost nothing about SOC. However, **outside** that plateau (below ~20 % or above ~90 %), the OCV curve is steep enough to use as a reliable reference.

When:
- `|power| < REST_POWER_W` (battery is at rest), **and**
- this rest condition has been sustained for `REST_DURATION_S` seconds, **and**
- the voltage is **outside** the configured dead zone (`OCV_DEAD_ZONE_V`),

…the script looks up the current voltage on the OCV curve, computes the expected SOC, and **snaps the internal SOC to that value** (subject to a sanity check: jumps larger than `OCV_MAX_JUMP_PCT` are rejected as sensor glitches).

Inside the dead zone (the flat plateau), only coulomb counting is used — voltage-based correction would be meaningless and potentially harmful there.

#### 3. SOH estimation (between OCV anchors)

Every time an OCV recalibration happens, the script records an **anchor**: the OCV-derived SOC and a reset of the Ah accumulator. Between two anchors, it tracks exactly how many Ah flowed. When the next recalibration fires, it computes:

```
measured_capacity = ΔAh_accumulated / (ΔSOC_OCV / 100)
raw_SOH = measured_capacity / installed_capacity
```

If the raw SOH is within plausible bounds (`SOH_MIN` to `SOH_MAX`) and enough SOC change accumulated (`SOH_MIN_DELTA_SOC`), this raw sample is blended into a running **Exponential Moving Average**:

```
SOH = (1 - α) × SOH_previous + α × raw_SOH_sample
```

With `α = 0.25`, the estimate converges in roughly 4 good samples. This means the capacity model used for coulomb counting **self-calibrates** over time as the battery ages, preventing the drift-gets-worse failure mode of the JK BMS.

#### 4. Persistence across reboots

SOC, SOH, and the current anchor are saved to `soc_state.json` every minute (when SOC has changed by more than 0.05 %) and on every OCV recalibration. On startup, the saved SOC is restored — no need to wait for a full charge/discharge cycle to get a sensible reading after a GX reboot.

---

## Requirements

- A Victron GX device (Cerbo GX, Multiplus II GX, EasySolar GX, etc.) running **Venus OS**
- A JK BMS connected via CAN (service `com.victronenergy.battery.socketcan_can0`)
- SSH access to the GX device
- Internet access on the GX device (for `download_or_update.sh`) **or** manual file transfer

---

## Installation

### Step 1 — Enable SSH on the GX device

In the GX local console or VRM: **Settings → General → Access Level → Superuser**, then enable SSH.

### Step 2 — SSH into the device

```bash
ssh root@<your-gx-ip>
```

There is no password by default on most Venus OS installations (or set one in Settings → General).

### Step 3 — Create a working directory

```bash
mkdir -p /data/bettersoc
cd /data/bettersoc
```

> **Why `/data`?** The `/data` partition survives firmware updates on Venus OS. Everything else under `/` is wiped on update.

### Step 4 — Download the scripts

Download `battery_monitor.py` and both helper shell scripts using the `download_or_update.sh` script — or do it manually with `curl`:

```bash
# Download the main estimator
curl -sSL https://raw.githubusercontent.com/gabrielcdv/BetterSocEstimatorGX/main/battery_monitor.py \
     -o battery_monitor.py

# Download the helper scripts
curl -sSL https://raw.githubusercontent.com/gabrielcdv/BetterSocEstimatorGX/main/download_or_update.sh \
     -o download_or_update.sh

curl -sSL https://raw.githubusercontent.com/gabrielcdv/BetterSocEstimatorGX/main/restart_soc_estimator.sh \
     -o restart_soc_estimator.sh

chmod +x download_or_update.sh restart_soc_estimator.sh battery_monitor.py
```

### Step 5 — Edit the configuration (important!)

Open `battery_monitor.py` in a text editor (e.g. `vi`) and review the `CONFIG` block near the top:

```bash
vi battery_monitor.py
```

Key things to verify:

| Variable | Default | What to check |
|---|---|---|
| `JK_SERVICE` | `com.victronenergy.battery.socketcan_can0` | Run `dbus -y` or `dbus-spy` to confirm your BMS service name |
| `OCV_CURVE` | 16S LiFePO4 values | **Must match your actual battery chemistry and cell count** |
| `OCV_DEAD_ZONE_V` | `(51.1, 53.9)` | The flat plateau voltage range of your pack |
| `REST_POWER_W` | `250 W` | Adjust to your system's idle power (inverter standby, etc.) |
| `REST_DURATION_S` | `300 s` | How long the battery must be at rest before OCV correction fires |

To find your actual BMS service name:

```bash
dbus -y com.victronenergy.battery.<TAB>   # tab-complete to see available services
# or list all battery services:
dbus-send --system --dest=org.freedesktop.DBus --type=method_call \
  --print-reply /org/freedesktop/DBus org.freedesktop.DBus.ListNames \
  | grep battery
```

### Step 6 — Start the estimator

```bash
bash restart_soc_estimator.sh
```

### Step 7 — Select the virtual battery in Venus OS

Go to: **Settings → System setup → Battery monitor** and select `BetterEstimatorGX (Virtual)`.

The virtual service will now be the SOC source used by the GX device, MPPT, and VRM.

### Step 8 — Survive reboots (rc.local)

To auto-start after a GX reboot, add the launch command to `/data/rc.local` (create it if it doesn't exist):

```bash
cat >> /data/rc.local << 'EOF'
# Start BetterSocEstimatorGX
cd /data/bettersoc && nohup python3 -u battery_monitor.py > /data/bettersoc/log.txt 2>&1 &
EOF
chmod +x /data/rc.local
```

> `/data/rc.local` is the standard Venus OS hook for persistent startup scripts. It is sourced at boot after all services are up.

---

## Usage

### Starting / restarting the estimator

Use `restart_soc_estimator.sh` whenever you want to (re)start the estimator — after a config change, after an update, or if it has crashed:

```bash
bash /data/bettersoc/restart_soc_estimator.sh
```

What this script does, step by step:
1. Kills any running instance of `battery_monitor.py` via `pkill -f battery_monitor.py`.
2. Waits 1 second for resources (D-Bus name, lock file) to be released.
3. Starts `battery_monitor.py` in the background with `nohup`, redirecting all output to `log.txt` in the same directory.
4. Prints the PID of the new process.

The process runs detached from the terminal — closing your SSH session will not stop it.

### Updating to the latest version

Use `download_or_update.sh` to pull the latest `battery_monitor.py` from GitHub:

```bash
bash /data/bettersoc/download_or_update.sh
```

What this script does:
1. Downloads the latest `battery_monitor.py` from the `main` branch of this repository using `curl -sSL` (silent, show errors, follow redirects).
2. Saves it as `virtual_battery.py` in the current directory (overwriting the existing file).
3. Makes it executable with `chmod +x`.

> **Note:** After updating, run `restart_soc_estimator.sh` to apply the new version. The existing `soc_state.json` is preserved — your SOC and SOH history carry over.

### Viewing live logs

```bash
tail -f /data/bettersoc/log.txt
```

Example log output:
```
Restored SOC from disk: 67.45% (saved 2.3 min ago)
Restored SOH from disk: 97.2%
Restored SOH anchor: soc=45.20% accumulated_Ah=+0.00
Service 'com.victronenergy.battery.virtual' registered on bus
OCV recalibration: V=53.48V soc 66.91% -> 68.30% (Δ=+1.39%)
SOH sample: ΔAh=-28.34 ΔSOC=-23.10% => measured_cap=122.7Ah raw_SOH=97.4% smoothed=97.2% (was 97.1%)
```

### Checking the process is running

```bash
pgrep -a -f battery_monitor.py
```

---

## Configuration

All tunable parameters live in the `# ─── CONFIG ───` block at the top of `battery_monitor.py`.

### BMS connection

```python
JK_SERVICE = 'com.victronenergy.battery.socketcan_can0'
```
The D-Bus service name of your JK BMS. Change this if your CAN interface is named differently (e.g. `can1`).

### Rest detection

```python
REST_POWER_W   = 250    # |P| below this → "at rest"
REST_DURATION_S = 300   # seconds at rest before OCV recalibration fires
```

Set `REST_POWER_W` to comfortably above your system's idle draw (inverter standby, fridge, router, etc.) but well below your typical load. `REST_DURATION_S = 300` (5 minutes) gives the voltage time to relax to OCV after a load change.

### OCV recalibration safety

```python
OCV_MIN_DELTA_PCT = 0.1    # ignore corrections smaller than this
OCV_MAX_JUMP_PCT  = 100.0  # reject corrections larger than this (sensor glitch guard)
```

### Coulomb counting

```python
CHARGE_EFFICIENCY = 1.00   # 1.0 = ideal (LiFePO4). Use 0.98–0.99 for Li-ion.
```

### SOH estimation

```python
INITIAL_SOH       = 1.00   # starting guess (100%)
SOH_MIN           = 0.50   # reject raw samples below 50% (measurement error)
SOH_MAX           = 1.30   # reject raw samples above 130% (measurement error)
SOH_MIN_DELTA_SOC = 25.0   # minimum OCV-measured SOC swing between two anchors
SOH_EMA_ALPHA     = 0.25   # EMA smoothing: higher = faster but noisier
```

### OCV curve

```python
OCV_CURVE = [
    (44.80,   0.0),
    ...
    (55.20, 100.0),
]
OCV_DEAD_ZONE_V = (51.1, 53.9)
```

The default curve is for a **16S LiFePO4** pack (nominal 51.2 V). If your battery is different (different chemistry, different cell count), you **must** replace this with the correct OCV data for your cells. Your BMS manufacturer or cell datasheet will have this. Voltage points must be in ascending order.

`OCV_DEAD_ZONE_V` defines the flat plateau region where OCV correction is suppressed. For 16S LiFePO4, this is roughly 51.1–53.9 V (corresponding to ~20–90 % SOC).

---

## D-Bus paths published

The virtual service (`com.victronenergy.battery.virtual`) publishes the following paths:

| Path | Description |
|---|---|
| `/Soc` | SOC in % (this estimator's output) |
| `/Soh` | SOH in % (EMA-smoothed capacity estimate) |
| `/Dc/0/Voltage` | Pack voltage (mirrored from BMS) |
| `/Dc/0/Current` | Pack current (mirrored from BMS) |
| `/Dc/0/Power` | Pack power (V × I) |
| `/Dc/0/Temperature` | Pack temperature (mirrored from BMS) |
| `/Capacity` | SOH-corrected remaining capacity in Ah |
| `/InstalledCapacity` | Rated installed capacity in Ah (from BMS) |
| `/ConsumedAmphours` | Ah consumed from full (negative value) |
| `/Info/RestTimer` | Seconds the battery has been at rest |
| `/Info/InDeadZone` | 1 if voltage is in the OCV dead zone, 0 otherwise |
| `/Info/SocBmsDelta` | `virtual_SOC − BMS_SOC` in % (drift monitor) |
| `/Info/LastCorrection` | Human-readable description of the last OCV correction |
| `/Info/AnchorSoc` | SOC value at the last OCV anchor point |
| `/Info/AnchorAh` | Ah accumulated since the last anchor |
| `/Info/LastSohSample` | Most recent raw (pre-EMA) SOH measurement in % |

You can read any of these from the command line:

```bash
dbus -y com.victronenergy.battery.virtual /Soc
dbus -y com.victronenergy.battery.virtual /Info/SocBmsDelta
dbus -y com.victronenergy.battery.virtual /Soh
```

---

## Monitoring & troubleshooting

### Check live log

```bash
tail -f /data/bettersoc/log.txt
```

### Check D-Bus registration

```bash
dbus -y com.victronenergy.battery.virtual /Mgmt/ProcessName
```
Should return the path to `battery_monitor.py`.

### Check the drift between this estimator and the JK BMS

```bash
dbus -y com.victronenergy.battery.virtual /Info/SocBmsDelta
```
A positive value means this estimator thinks the battery is *more charged* than the JK BMS claims. If this grows over time, the JK BMS is drifting low (underestimating). If it oscillates around zero, both are roughly in agreement.

### Process not starting

- Check for errors in `log.txt`.
- Verify the JK BMS service name with `dbus -y` or check `/Info/BmsService`.
- Ensure you're running as root (`whoami`).
- Check for a stale lock file: `rm -f /data/bettersoc/virtual_battery.lock` then restart.

### D-Bus name conflict

If another instance is already running and holding the D-Bus name, the script will retry 10 times with a 1.5-second delay before failing. Use `restart_soc_estimator.sh` — it kills existing instances first.

### OCV corrections not firing

- Confirm `REST_POWER_W` is high enough to cover your actual standby load.
- Check `/Info/InDeadZone` — if it's 1, your voltage is in the plateau; OCV correction is intentionally suppressed there.
- Watch `/Info/RestTimer` — it must reach `REST_DURATION_S` (default 300) before a correction fires.

---

## Persistent state

State is saved to `soc_state.json` in the same directory as `battery_monitor.py`:

```json
{
  "soc": 67.45,
  "soh": 0.972,
  "anchor_soc": 45.20,
  "anchor_ah": -12.34,
  "timestamp": 1748000000.0
}
```

- **On clean shutdown** (SIGTERM or SIGINT): state is saved immediately.
- **Every 60 seconds**: state is saved if SOC has changed by more than 0.05 %.
- **On every OCV recalibration**: state is saved.

On startup, if a valid saved SOC exists, it is restored directly. If not (first run, or corrupted file), SOC is initialized from the JK BMS value. SOH is always restored from disk if available.

---

## License

MIT — do whatever you want with it, no warranty provided.
