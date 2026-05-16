#!/usr/bin/env python3
"""
Virtual battery monitor for Victron GX devices. Meant to be used instead of JK BMS' poor SOC estimator.

Algorithm:
  - Maintain our own SOC by integrating the BMS current (coulomb counting),
    using an SOH-corrected effective capacity.
  - Periodically save SOC and SOH to disk so they survive restarts.
  - When the battery is at rest (low |power|) for long enough AND its voltage
    is OUTSIDE the configured "dead zone" (the flat voltage plateau), realign
    the SOC against the OCV curve.
  - Inside the dead zone, voltage carries almost no SOC information, so only
    coulomb counting is used.
  - Between two OCV "anchor" points we accumulate raw Ah and the SOC change
    measured by the OCV curve. Their ratio gives the *actual* usable capacity,
    from which SOH = measured / installed is derived and EMA-smoothed.
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
OCV_MAX_JUMP_PCT    = 100.0  # safety: refuse OCV jumps larger than this in one shot.

# Coulomb counting
CHARGE_EFFICIENCY   = 1.00   # 1.0 = ideal (LiFePO4); use 0.98–0.99 for Li-ion
                             # applied only when current > 0 (charging)

# SOH estimation
INITIAL_SOH         = 1.00   # starting guess if no saved value
SOH_MIN             = 0.50   # clamp: anything below is rejected as a bad measurement
SOH_MAX             = 1.30   # clamp: anything above is rejected as a bad measurement
SOH_MIN_DELTA_SOC   = 25.0   # need at least this much OCV-measured SOC change
                             # between two anchors before we trust an SOH sample
SOH_EMA_ALPHA       = 0.25   # smoothing factor for SOH updates (0..1).
                             # Higher = faster adaptation, noisier.
                             # ~0.25 ⇒ converges in ~4 good samples.

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
    """Return a dict with persisted state. Missing fields default to None / INITIAL_SOH."""
    result = {
        'soc': None,
        'soc_age': None,
        'soh': INITIAL_SOH,
        'anchor_soc': None,
        'anchor_ah': 0.0,
    }
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
        soc = float(data['soc'])
        ts  = float(data.get('timestamp', 0))
        age = max(0.0, time.time() - ts) if ts > 0 else None
        if 0.0 <= soc <= 100.0:
            result['soc'] = soc
            result['soc_age'] = age
            print(f"Restored SOC from disk: {soc:.2f}%"
                  + (f" (saved {age/60:.1f} min ago)" if age is not None else ""))

        # SOH (optional)
        if 'soh' in data:
            soh = float(data['soh'])
            if SOH_MIN <= soh <= SOH_MAX:
                result['soh'] = soh
                print(f"Restored SOH from disk: {soh*100:.1f}%")
            else:
                print(f"Saved SOH {soh:.3f} out of bounds, using {INITIAL_SOH}.")

        # Anchor (optional). We DO restore it so SOH can keep accumulating
        # across restarts; if the system was off for a while it'll just yield
        # a noisy sample that gets damped by the EMA, or we'll overwrite the
        # anchor at the next OCV recalibration anyway.
        if 'anchor_soc' in data and data['anchor_soc'] is not None:
            a_soc = float(data['anchor_soc'])
            a_ah  = float(data.get('anchor_ah', 0.0))
            if 0.0 <= a_soc <= 100.0:
                result['anchor_soc'] = a_soc
                result['anchor_ah']  = a_ah
                print(f"Restored SOH anchor: soc={a_soc:.2f}% accumulated_Ah={a_ah:+.2f}")
    except FileNotFoundError:
        print("No saved state, will initialize from BMS.")
    except Exception as e:
        print(f"Could not read state file: {e}")
    return result


def save_state(soc, soh, anchor_soc, anchor_ah):
    try:
        tmp = STATE_FILE + '.tmp'
        payload = {
            'soc':        soc,
            'soh':        soh,
            'anchor_soc': anchor_soc,
            'anchor_ah':  anchor_ah,
            'timestamp':  time.time(),
        }
        with open(tmp, 'w') as f:
            json.dump(payload, f)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        print(f"Error saving state: {e}")


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
    svc.add_path('/Mgmt/ProcessVersion', '2.1')
    svc.add_path('/Mgmt/Connection',     'Virtual')
    svc.add_path('/DeviceInstance',      99)
    svc.add_path('/ProductId',           0)
    svc.add_path('/ProductName',         'BetterEstimatorGX')
    svc.add_path('/FirmwareVersion',     2)
    svc.add_path('/HardwareVersion',     1)
    svc.add_path('/Connected',           1)

    svc.add_path('/Soc',                 50.0)
    svc.add_path('/Soh',                 100.0)
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
    svc.add_path('/Info/AnchorSoc',      None)
    svc.add_path('/Info/AnchorAh',       0.0)
    svc.add_path('/Info/LastSohSample',  None)  # last raw SOH measurement (pre-EMA)

    _lock = acquire_single_instance_lock()
    register_with_retry(svc)
    print("Service 'com.victronenergy.battery.virtual' registered on bus")

    # ── State ─────────────────────────────────────────────────────────────────
    persisted = load_state()
    if persisted['soc'] is None:
        bms_soc = safe_float(imp_soc_jk.get_value(), 50.0)
        soc = bms_soc
        print(f"Initialized SOC from BMS: {soc:.2f}%")
    else:
        soc = persisted['soc']

    state = {
        'soc':            soc,
        'soh':            persisted['soh'],
        'anchor_soc':     persisted['anchor_soc'],   # SOC at last OCV anchor, or None
        'anchor_ah':      persisted['anchor_ah'],    # raw Ah accumulated since last anchor
        'rest_timer':     0.0,
        'last_update':    time.monotonic(),
        'last_save':      time.monotonic(),
        'last_save_soc':  soc,
    }

    def persist_now():
        save_state(state['soc'], state['soh'],
                   state['anchor_soc'], state['anchor_ah'])

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

        # Effective capacity = installed * SOH (used by coulomb counting)
        eff_capacity = capacity * state['soh'] if capacity > 0 else 0.0

        # ── Coulomb counting ──────────────────────────────────────────────────
        # Sign convention (Victron): current > 0 = charging, < 0 = discharging.
        delta_ah_raw = 0.0
        if capacity > 0:
            eff = CHARGE_EFFICIENCY if current > 0 else 1.0
            delta_ah_raw = current * eff * dt / 3600.0
            delta_soc = delta_ah_raw / eff_capacity * 100.0
            state['soc'] = max(0.0, min(100.0, state['soc'] + delta_soc))

            # Accumulate Ah into the SOH anchor window (only if an anchor exists)
            if state['anchor_soc'] is not None:
                state['anchor_ah'] += delta_ah_raw

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
            did_correction = False

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
                    did_correction = True
                    print(f"OCV recalibration: V={voltage:.2f}V "
                          f"soc {old_soc:.2f}% -> {ocv_soc:.2f}% "
                          f"(Δ={delta:+.2f}%)")
                    svc['/Info/LastCorrection'] = (
                        f"OCV@{voltage:.2f}V => {ocv_soc:.1f}%"
                    )

            # ── SOH update ────────────────────────────────────────────────────
            # We use BOTH corrected and uncorrected recalibrations as a chance
            # to evaluate SOH, as long as we have a previous anchor and enough
            # SOC change has accumulated.
            if capacity > 0 and state['anchor_soc'] is not None:
                delta_soc_real = ocv_soc - state['anchor_soc']
                if abs(delta_soc_real) >= SOH_MIN_DELTA_SOC:
                    # measured_capacity such that:
                    #   anchor_ah  ==  measured_capacity * (delta_soc_real / 100)
                    measured_cap = state['anchor_ah'] / (delta_soc_real / 100.0)
                    # signs: charging (Δsoc>0, Ah>0) and discharging (both <0)
                    # both yield positive measured_cap. Reject negatives.
                    if measured_cap > 0:
                        new_soh = measured_cap / capacity
                        svc['/Info/LastSohSample'] = round(new_soh * 100.0, 1)
                        if SOH_MIN <= new_soh <= SOH_MAX:
                            old_soh = state['soh']
                            state['soh'] = (1.0 - SOH_EMA_ALPHA) * old_soh \
                                           + SOH_EMA_ALPHA * new_soh
                            print(f"SOH sample: ΔAh={state['anchor_ah']:+.2f} "
                                  f"ΔSOC={delta_soc_real:+.2f}% "
                                  f"=> measured_cap={measured_cap:.1f}Ah "
                                  f"raw_SOH={new_soh*100:.1f}% "
                                  f"smoothed={state['soh']*100:.2f}% "
                                  f"(was {old_soh*100:.2f}%)")
                        else:
                            print(f"SOH sample rejected (out of bounds): "
                                  f"raw_SOH={new_soh*100:.1f}% "
                                  f"(ΔAh={state['anchor_ah']:+.2f} "
                                  f"ΔSOC={delta_soc_real:+.2f}%)")
                    else:
                        print(f"SOH sample rejected (sign mismatch): "
                              f"ΔAh={state['anchor_ah']:+.2f} "
                              f"ΔSOC={delta_soc_real:+.2f}%")
                else:
                    print(f"OCV anchor: ΔSOC={delta_soc_real:+.2f}% too small "
                          f"for SOH update (<{SOH_MIN_DELTA_SOC:.0f}%).")

            # Always set a fresh anchor at the current OCV-derived SOC and
            # reset the Ah accumulator.
            state['anchor_soc'] = ocv_soc
            state['anchor_ah']  = 0.0

            # Persist whenever we anchor/recalibrate
            persist_now()
            state['last_save']     = now
            state['last_save_soc'] = state['soc']

            # Reset rest timer whether we corrected or not, to avoid spamming
            state['rest_timer'] = 0.0

        # ── Periodic state save ───────────────────────────────────────────────
        if (now - state['last_save']) >= SAVE_INTERVAL_S and \
           abs(state['soc'] - state['last_save_soc']) > 0.05:
            persist_now()
            state['last_save']     = now
            state['last_save_soc'] = state['soc']

        # ── Publish to D-Bus ──────────────────────────────────────────────────
        soc_out = round(state['soc'], 1)
        svc['/Soc']                = soc_out
        svc['/Soh']                = round(state['soh'] * 100.0, 1)
        svc['/Dc/0/Voltage']       = round(voltage, 2)
        svc['/Dc/0/Current']       = round(current, 2)
        svc['/Dc/0/Power']         = round(power, 1)
        svc['/Dc/0/Temperature']   = temp
        # /Capacity and /ConsumedAmphours reflect the SOH-corrected usable
        # capacity, which is what actually behaves like the user's "battery".
        svc['/Capacity']           = (round(eff_capacity * state['soc'] / 100.0, 2)
                                      if eff_capacity > 0 else None)
        svc['/InstalledCapacity']  = capacity if capacity > 0 else None
        svc['/ConsumedAmphours']   = (round(-eff_capacity * (100.0 - state['soc']) / 100.0, 2)
                                      if eff_capacity > 0 else None)

        svc['/Info/RestTimer']     = int(state['rest_timer'])
        svc['/Info/InDeadZone']    = 1 if dead else 0
        svc['/Info/SocBmsDelta']   = round(state['soc'] - bms_soc, 2)
        svc['/Info/AnchorSoc']     = (round(state['anchor_soc'], 2)
                                      if state['anchor_soc'] is not None else None)
        svc['/Info/AnchorAh']      = round(state['anchor_ah'], 3)

        return True  # keep timer running

    # Save on graceful shutdown
    def on_shutdown():
        try:
            persist_now()
            print(f"Final state saved: SOC={state['soc']:.2f}% "
                  f"SOH={state['soh']*100:.2f}%")
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