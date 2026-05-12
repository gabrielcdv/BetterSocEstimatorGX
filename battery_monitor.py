#!/usr/bin/env python3
"""
Virtual battery monitor for Victron GX devices. Meant to be used instead of JK BMS' poor SOC estimator.

Algorithm:
  - Maintain our own SOC by integrating the BMS current (coulomb counting).
  - Periodically save SOC to disk so it survives restarts.
  - When the battery is at rest (low |power|) for long enough AND its voltage
    is OUTSIDE the configured "dead zone" (the flat voltage plateau), realign
    the SOC against the OCV curve.
  - Inside the dead zone, voltage carries almost no SOC information, so only
    coulomb counting is used.
"""

import sys
import os
import time
import json
import dbus
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib
import fcntl
import errno

sys.path.insert(1, '/opt/victronenergy/dbus-systemcalc-py/ext/velib_python')
from vedbus import VeDbusService, VeDbusItemImport

# ─── CONFIG ───────────────────────────────────────────────────────────────────
JK_SERVICE          = 'com.victronenergy.battery.socketcan_can0'

# Rest detection
REST_POWER_W        = 250    # |P| below this => considered "at rest"
REST_DURATION_S     = 300    # seconds at rest before OCV recalibration is allowed

# OCV recalibration
OCV_MIN_DELTA_PCT   = 0.1    # ignore correction if |OCV_SOC - current_SOC| < this
OCV_MAX_JUMP_PCT    = 100.0   # safety: refuse OCV jumps larger than this in one shot. Warning: 
                              # if the current SOC estimator is way off, set this to 100 to allow 
                              # jumping to a better value.



# Coulomb counting
CHARGE_EFFICIENCY   = 1.00   # 1.0 = ideal (LiFePO4); use 0.98–0.99 for Li-ion
                             # applied only when current > 0 (charging)

# Persistence
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'soc_state.json')
SAVE_INTERVAL_S = 60         # seconds between periodic SOC saves to disk

# Update loop
UPDATE_PERIOD_MS = 1000

# OCV curve  -> SOC  (linear interpolation between points)
# (voltage_V, soc_%)  sorted by ascending voltage. Add points to refine.
OCV_CURVE = [
    (44.80,   0.0),
    (46.40,   2.0),
    (48.00,   5.0),
    (49.60,   8.0),
    (50.40,  10.0),
    (51.20,  13.0),
    (51.52,  15.0),
    (51.84,  20.0),
    (52.00,  25.0),
    (52.08,  30.0),
    (52.16,  40.0),
    (52.24,  50.0),
    (52.32,  60.0),
    (52.48,  70.0),
    (52.64,  75.0),
    (52.80,  80.0),
    (53.12,  85.0),
    (53.44,  88.0),
    (53.76,  90.0),
    (54.24,  93.0),
    (54.56,  95.0),
    (54.88,  98.0),
    (55.20, 100.0),
]
# Dead zone: voltage range where OCV is NOT used to update SOC.
# For a 16S LiFePO4 pack, the plateau is roughly between ~20% and ~90% SOC.
OCV_DEAD_ZONE_V     = (51.1, 53.9)   # (low, high) inclusive
# ──────────────────────────────────────────────────────────────────────────────

LOCK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'virtual_battery.lock')

def acquire_single_instance_lock():
    """Prevent more than one copy of this script from running at once."""
    fp = open(LOCK_FILE, 'w')
    try:
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        if e.errno in (errno.EACCES, errno.EAGAIN):
            print("Another instance is already running. Exiting.")
            sys.exit(0)
        raise
    fp.write(str(os.getpid()))
    fp.flush()
    return fp  # keep the file object alive for the lifetime of the process


def register_with_retry(svc, attempts=10, delay=1.5):
    """Retry svc.register() to ride out transient name-ownership conflicts."""
    for i in range(1, attempts + 1):
        try:
            svc.register()
            return
        except dbus.exceptions.NameExistsException:
            if i == attempts:
                raise
            print(f"D-Bus name busy (attempt {i}/{attempts}), retrying in {delay:.1f}s...")
            time.sleep(delay)

def ocv_to_soc(voltage):
    """Linear interpolation of the OCV curve."""
    if voltage <= OCV_CURVE[0][0]:
        return OCV_CURVE[0][1]
    if voltage >= OCV_CURVE[-1][0]:
        return OCV_CURVE[-1][1]
    for i in range(len(OCV_CURVE) - 1):
        v0, s0 = OCV_CURVE[i]
        v1, s1 = OCV_CURVE[i + 1]
        if v0 <= voltage <= v1:
            return s0 + (s1 - s0) * (voltage - v0) / (v1 - v0)
    return OCV_CURVE[-1][1]  # fallback (unreachable)


def in_dead_zone(voltage):
    lo, hi = OCV_DEAD_ZONE_V
    return lo <= voltage <= hi


def load_state():
    """Return (soc, age_seconds) or (None, None) if no/invalid state file."""
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
        soc = float(data['soc'])
        ts  = float(data.get('timestamp', 0))
        age = max(0.0, time.time() - ts) if ts > 0 else None
        if 0.0 <= soc <= 100.0:
            print(f"Restored SOC from disk: {soc:.2f}%"
                  + (f" (saved {age/60:.1f} min ago)" if age is not None else ""))
            return soc, age
    except FileNotFoundError:
        print("No saved SOC state, will initialize from BMS.")
    except Exception as e:
        print(f"Could not read SOC state: {e}")
    return None, None


def save_state(soc):
    try:
        tmp = STATE_FILE + '.tmp'
        with open(tmp, 'w') as f:
            json.dump({'soc': soc, 'timestamp': time.time()}, f)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        print(f"Error saving SOC state: {e}")


def safe_float(value, default=0.0):
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def main():
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
    DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()

    # ── BMS imports ───────────────────────────────────────────────────────────
    imp_voltage  = VeDbusItemImport(bus, JK_SERVICE, '/Dc/0/Voltage')
    imp_current  = VeDbusItemImport(bus, JK_SERVICE, '/Dc/0/Current')
    imp_soc_jk   = VeDbusItemImport(bus, JK_SERVICE, '/Soc')
    imp_capacity = VeDbusItemImport(bus, JK_SERVICE, '/InstalledCapacity')
    imp_temp     = VeDbusItemImport(bus, JK_SERVICE, '/Dc/0/Temperature')

    # ── Virtual service ───────────────────────────────────────────────────────
    svc = VeDbusService('com.victronenergy.battery.virtual', bus=bus, register=False)

    svc.add_path('/Mgmt/ProcessName',    __file__)
    svc.add_path('/Mgmt/ProcessVersion', '2.0')
    svc.add_path('/Mgmt/Connection',     'Virtual')
    svc.add_path('/DeviceInstance',      99)
    svc.add_path('/ProductId',           0)
    svc.add_path('/ProductName',         'BetterEstimatorGX')
    svc.add_path('/FirmwareVersion',     2)
    svc.add_path('/HardwareVersion',     1)
    svc.add_path('/Connected',           1)

    svc.add_path('/Soc',                 50.0)
    svc.add_path('/Dc/0/Voltage',        0.0)
    svc.add_path('/Dc/0/Current',        0.0)
    svc.add_path('/Dc/0/Power',          0.0)
    svc.add_path('/Dc/0/Temperature',    None)
    svc.add_path('/Capacity',            None)
    svc.add_path('/InstalledCapacity',   None)
    svc.add_path('/ConsumedAmphours',    None)

    svc.add_path('/Alarms/LowVoltage',   0)
    svc.add_path('/Alarms/HighVoltage',  0)
    svc.add_path('/Alarms/LowSoc',       0)

    svc.add_path('/Info/RestTimer',      0)
    svc.add_path('/Info/InDeadZone',     0)
    svc.add_path('/Info/SocBmsDelta',    0.0)   # virtual_SOC - BMS_SOC, for monitoring
    svc.add_path('/Info/LastCorrection', 'none')

    _lock = acquire_single_instance_lock()
    register_with_retry(svc)
    print("Service 'com.victronenergy.battery.virtual' registered on bus")

    # ── State ─────────────────────────────────────────────────────────────────
    saved_soc, _ = load_state()
    if saved_soc is None:
        bms_soc = safe_float(imp_soc_jk.get_value(), 50.0)
        soc = bms_soc
        print(f"Initialized SOC from BMS: {soc:.2f}%")
    else:
        soc = saved_soc

    state = {
        'soc':            soc,
        'rest_timer':     0.0,
        'last_update':    time.monotonic(),
        'last_save':      time.monotonic(),
        'last_save_soc':  soc,
    }

    # ── Update loop ───────────────────────────────────────────────────────────
    def update():
        now = time.monotonic()
        dt  = now - state['last_update']
        state['last_update'] = now

        # Sanity-clip dt (handles clock jumps, suspends, etc.)
        if dt < 0 or dt > 10.0:
            dt = UPDATE_PERIOD_MS / 1000.0

        voltage  = safe_float(imp_voltage.get_value())
        current  = safe_float(imp_current.get_value())
        bms_soc  = safe_float(imp_soc_jk.get_value(), state['soc'])
        capacity = safe_float(imp_capacity.get_value())
        temp     = imp_temp.get_value()
        power    = voltage * current

        # ── Coulomb counting ──────────────────────────────────────────────────
        # Sign convention (Victron): current > 0 = charging, < 0 = discharging.
        if capacity > 0:
            eff = CHARGE_EFFICIENCY if current > 0 else 1.0
            delta_ah  = current * eff * dt / 3600.0
            delta_soc = delta_ah / capacity * 100.0
            state['soc'] = max(0.0, min(100.0, state['soc'] + delta_soc))

        # ── Rest detection ────────────────────────────────────────────────────
        if abs(power) < REST_POWER_W:
            state['rest_timer'] += dt
        else:
            state['rest_timer'] = 0.0

        dead = in_dead_zone(voltage)

        # ── OCV recalibration (only outside the dead zone) ────────────────────
        if state['rest_timer'] >= REST_DURATION_S and not dead and voltage > 0:
            ocv_soc = ocv_to_soc(voltage)
            delta   = ocv_soc - state['soc']
            if abs(delta) > OCV_MIN_DELTA_PCT:
                if abs(delta) > OCV_MAX_JUMP_PCT:
                    # Refuse implausibly large jumps (sensor glitch, bad curve, etc.)
                    msg = (f"OCV jump rejected (>{OCV_MAX_JUMP_PCT:.0f}%): "
                           f"V={voltage:.2f}V soc={state['soc']:.1f}% "
                           f"ocv_soc={ocv_soc:.1f}%")
                    print(msg)
                    svc['/Info/LastCorrection'] = (
                        f"REJECTED OCV@{voltage:.2f}V => {ocv_soc:.1f}%"
                    )
                else:
                    old_soc = state['soc']
                    state['soc'] = ocv_soc
                    save_state(state['soc'])
                    state['last_save']     = now
                    state['last_save_soc'] = state['soc']
                    print(f"OCV recalibration: V={voltage:.2f}V "
                          f"soc {old_soc:.2f}% -> {ocv_soc:.2f}% "
                          f"(Δ={delta:+.2f}%)")
                    svc['/Info/LastCorrection'] = (
                        f"OCV@{voltage:.2f}V => {ocv_soc:.1f}%"
                    )
            # Reset rest timer whether we corrected or not, to avoid spamming
            state['rest_timer'] = 0.0

        # ── Periodic state save ───────────────────────────────────────────────
        if (now - state['last_save']) >= SAVE_INTERVAL_S and \
           abs(state['soc'] - state['last_save_soc']) > 0.05:
            save_state(state['soc'])
            state['last_save']     = now
            state['last_save_soc'] = state['soc']

        # ── Publish to D-Bus ──────────────────────────────────────────────────
        soc_out = round(state['soc'], 1)
        svc['/Soc']                = soc_out
        svc['/Dc/0/Voltage']       = round(voltage, 2)
        svc['/Dc/0/Current']       = round(current, 2)
        svc['/Dc/0/Power']         = round(power, 1)
        svc['/Dc/0/Temperature']   = temp
        svc['/Capacity']           = (round(capacity * state['soc'] / 100.0, 2)
                                      if capacity > 0 else None)
        svc['/InstalledCapacity']  = capacity if capacity > 0 else None
        svc['/ConsumedAmphours']   = (round(-capacity * (100.0 - state['soc']) / 100.0, 2)
                                      if capacity > 0 else None)

        svc['/Info/RestTimer']     = int(state['rest_timer'])
        svc['/Info/InDeadZone']    = 1 if dead else 0
        svc['/Info/SocBmsDelta']   = round(state['soc'] - bms_soc, 2)

        return True  # keep timer running

    # Save on graceful shutdown
    def on_shutdown():
        try:
            save_state(state['soc'])
            print(f"Final SOC saved: {state['soc']:.2f}%")
        except Exception as e:
            print(f"Shutdown save failed: {e}")

    import atexit, signal
    atexit.register(on_shutdown)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: sys.exit(0))

    GLib.timeout_add(UPDATE_PERIOD_MS, update)
    GLib.MainLoop().run()


if __name__ == '__main__':
    main()
