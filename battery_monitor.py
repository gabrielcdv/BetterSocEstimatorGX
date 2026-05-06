#!/usr/bin/env python3
import sys
import os
import dbus
import json
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

sys.path.insert(1, '/opt/victronenergy/dbus-systemcalc-py/ext/velib_python')
from vedbus import VeDbusService, VeDbusItemImport

# ─── CONFIG ───────────────────────────────────────────────────────────────────
JK_SERVICE        = 'com.victronenergy.battery.socketcan_can0'
REST_POWER_W      = 250   # seuil repos en watts
REST_DURATION_S   = 300      # secondes au repos avant correction OCV
OCV_MIN_DELTA_PCT = 0.5      # correction ignorée si écart < 0.5%
OFFSET_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'offset.json')


# OCV curve -> SOC  (linear interpolation between points)
# (voltage_V, soc_%)  sorted by ascending voltage. Points can be added.
OCV_CURVE = [
    (44.0,   0.0),
    (46.0,   5.0),
    (48.0,  15.0),
    (50.0,  30.0),
    (51.0,  50.0),
    (52.0,  70.0),
    (53.0,  85.0),
    (54.0,  95.0),
    (54.6, 100.0),
]
# ──────────────────────────────────────────────────────────────────────────────


def ocv_to_soc(voltage):
    if voltage <= OCV_CURVE[0][0]:
        return OCV_CURVE[0][1]
    if voltage >= OCV_CURVE[-1][0]:
        return OCV_CURVE[-1][1]
    for i in range(len(OCV_CURVE) - 1):
        v0, s0 = OCV_CURVE[i]
        v1, s1 = OCV_CURVE[i + 1]
        if v0 <= voltage <= v1:
            return s0 + (s1 - s0) * (voltage - v0) / (v1 - v0)


def load_offset():
    try:
        with open(OFFSET_FILE) as f:
            offset = json.load(f)['offset']
            print(f"Restored offset from disk: {offset:+.1f}%")
            return offset
    except:
        print("No saved offset, starting with 0.0%")
        return 0.0


def save_offset(offset):
    try:
        with open(OFFSET_FILE, 'w') as f:
            json.dump({'offset': offset}, f)
    except Exception as e:
        print(f"Error saving offset: {e}")


def main():
    DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()

    # ── Lecture JK BMS ────────────────────────────────────────────────────────
    imp_voltage = VeDbusItemImport(bus, JK_SERVICE, '/Dc/0/Voltage')
    imp_current = VeDbusItemImport(bus, JK_SERVICE, '/Dc/0/Current')
    imp_soc_jk   = VeDbusItemImport(bus, JK_SERVICE, '/Soc')
    imp_capacity = VeDbusItemImport(bus, JK_SERVICE, '/Capacity')


    # ── Service virtuel ───────────────────────────────────────────────────────
    svc = VeDbusService('com.victronenergy.battery.virtual', bus=bus, register=False)

    svc.add_path('/Mgmt/ProcessName',    __file__)
    svc.add_path('/Mgmt/ProcessVersion', '1.0')
    svc.add_path('/Mgmt/Connection',     'Virtual')
    svc.add_path('/DeviceInstance',      99)
    svc.add_path('/ProductId',           0)
    svc.add_path('/ProductName',         'BetterEstimatorGX')
    svc.add_path('/FirmwareVersion',     1)
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

    svc.add_path('/Info/Offset',         0.0)
    svc.add_path('/Info/RestTimer',      0)
    svc.add_path('/Info/LastCorrection', 'none')

    svc.register()
    print("Service 'Virtual Battery Manager' registered on bus")

    # ── State ──────────────────────────────────────────────────────────────────
    offset     = load_offset()
    rest_timer = 0

    # ── Udate loop ─────────────────────────────────────────────────────────────
    def update():
        nonlocal offset, rest_timer

        voltage = float(imp_voltage.get_value() or 0)
        current = float(imp_current.get_value() or 0)
        soc_jk  = float(imp_soc_jk.get_value()  or svc['/Soc'])
        power   = voltage * current

        # Rest timer
        if abs(power) < REST_POWER_W:
            rest_timer += 1
        else:
            rest_timer = 0

        # Correct with OCV if the resting period was long enough
        if rest_timer >= REST_DURATION_S:
            corrected = ocv_to_soc(voltage)
            if abs(corrected - (soc_jk + offset)) > OCV_MIN_DELTA_PCT:
                old_offset = offset
                offset = corrected - soc_jk
                save_offset(offset)
                print(f"Correction OCV: V={voltage:.2f}V  soc_jk={soc_jk:.1f}%  "
                      f"offset {old_offset:+.1f}% -> {offset:+.1f}%  "
                      f"soc_virtuel={corrected:.1f}%")
                svc['/Info/LastCorrection'] = f"OCV@{voltage:.2f}V => {corrected:.1f}%"
            rest_timer = 0

        virtual_soc = soc_jk + offset

        svc['/Soc']              = round(max(0.0, min(100.0, virtual_soc)), 1)
        svc['/Dc/0/Voltage']     = round(voltage, 2)
        svc['/Dc/0/Current']     = round(current, 2)
        svc['/Dc/0/Power']       = round(power, 1)
        svc['/Info/Offset']      = round(offset, 2)
        svc['/Info/RestTimer']   = rest_timer
        
        capacity = imp_capacity.get_value()

        svc['/Capacity']          = capacity
        svc['/InstalledCapacity'] = capacity

        return True  # keep the timer

    GLib.timeout_add(1000, update)
    GLib.MainLoop().run()


if __name__ == '__main__':
    main()