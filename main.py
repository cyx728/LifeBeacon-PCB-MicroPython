"""PCB beacon test: protocol/physiology helpers and pcb_runtime entry point."""

from machine import ADC, I2C, Pin
import machine
import struct
import time
try:
    from neopixel import NeoPixel
except ImportError:
    NeoPixel = None
from sx1278 import make_radio
import pcb_config as cfg
from vitals import Vitals
vitals = Vitals()


# Keep all physical I/O assignments in pcb_config.py.
BAT_TEST, I2C_SCL, I2C_SDA = cfg.BAT_TEST, cfg.I2C_SCL, cfg.I2C_SDA

DEVICE_ID = 0       # 0 uses the ESP32 hardware ID
PERSON_NAME = "DEMO2_PCB"

BATTERY_DIVIDER = 1.67 - 0.1
BATTERY_FULL_MV = 4200
BATTERY_EMPTY_MV = 3200

ONE_SHOT = False
PERIOD_MS = 2000
LORA_FREQUENCY_HZ = 433_775_000
LORA_RESPONSE_FREQUENCY_HZ = 433_375_000
LORA_RESPONSE_DELAY_MS = 2500
LORA_TX_POWER_DBM = 6
LORA_BANDWIDTH_KHZ = 125
LORA_SPREADING_FACTOR = 7
LORA_CODING_RATE = '4/5'
LORA_CRC_ENABLED = True
LORA_HEADER_MODE = 'explicit'

RGB_ENABLED = True
RGB_PIN = cfg.RGB_PIN
RGB_BRIGHTNESS = 16
RGB_TX_COLOR = (255, 255, 0)

MAX30102_ADDR = 0x57
MAX30102_SAMPLE_PERIOD_MS = 10  # 100 FIFO samples/s, matching SPO2_CONFIG below
SPO2_WINDOW_SAMPLES = 100
SPO2_MIN_DC_IR = 10000

# Prototype-only alert bands; validate before operational use.
EMERGENCY_UNKNOWN = 0
EMERGENCY_NORMAL = 1
EMERGENCY_WARNING = 2
EMERGENCY_CRITICAL = 3
HR_WARNING_LOW = 50
HR_WARNING_HIGH = 120
HR_CRITICAL_LOW = 40
HR_CRITICAL_HIGH = 150
SPO2_WARNING_LOW = 94
SPO2_CRITICAL_LOW = 90

heart_state = {
    "baseline": 0,
    "amplitude": 0,
    "above": False,
    "last_peak_ms": None,
    "bpm": None,
    "last_ir": 0,
    "sample_count": 0,
    "red_sum": 0,
    "ir_sum": 0,
    "red_min": 0x03FFFF,
    "red_max": 0,
    "ir_min": 0x03FFFF,
    "ir_max": 0,
    "spo2_count": 0,
    "spo2": None,
}


def crc16_ccitt(data):
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def device_id():
    if DEVICE_ID:
        return DEVICE_ID & 0xFFFFFFFF
    raw = machine.unique_id()
    value = 0x811C9DC5
    for byte in raw:
        value = ((value ^ byte) * 0x01000193) & 0xFFFFFFFF
    return value


def init_battery():
    adc = ADC(Pin(BAT_TEST))
    adc.atten(ADC.ATTN_11DB)
    return adc


def battery_mv(adc):
    try:
        raw = adc.read_u16()
        return int(raw * 3300 / 65535 * BATTERY_DIVIDER)
    except AttributeError:
        return int(adc.read() * 3300 / 4095 * BATTERY_DIVIDER)


def battery_percent(mv):
    return max(0, min(100, int((mv - BATTERY_EMPTY_MV) * 100 / (BATTERY_FULL_MV - BATTERY_EMPTY_MV))))


def i2c_read(i2c, address, register, length=1):
    return i2c.readfrom_mem(address, register, length)


def init_max30102(i2c):
    address = MAX30102_ADDR
    if address not in i2c.scan():
        return False, None
    part_id = i2c_read(i2c, address, 0xFF)[0]
    if part_id != 0x15:
        raise RuntimeError('MAX30102 PART_ID=0x%02x expected=0x15' % part_id)
    # Reset, SpO2 mode, 100 sps / 411 us pulse width, and LED current.
    i2c.writeto_mem(address, 0x09, b"@")  # MODE_CONFIG: reset
    deadline = time.ticks_add(time.ticks_ms(), 500)
    while i2c_read(i2c, address, 0x09)[0] & 0x40:
        if time.ticks_diff(deadline, time.ticks_ms()) <= 0:
            raise RuntimeError('MAX30102 reset timeout')
        time.sleep_ms(5)
    i2c.writeto_mem(address, 0x04, b"\x00")  # FIFO_WR_PTR
    i2c.writeto_mem(address, 0x05, b"\x00")  # OVF_COUNTER
    i2c.writeto_mem(address, 0x06, b"\x00")  # FIFO_RD_PTR
    i2c.writeto_mem(address, 0x08, b"\x1F")  # avg=1, rollover enabled
    i2c.writeto_mem(address, 0x09, b"\x03")  # SpO2 mode
    i2c.writeto_mem(address, 0x0A, b"\x27")  # 100 sps, 18-bit ADC, 411 us
    i2c.writeto_mem(address, 0x0C, b"\x24")  # RED LED current
    i2c.writeto_mem(address, 0x0D, b"\x24")  # IR LED current
    for reg, expected in ((0x08, 0x1F), (0x09, 3), (0x0A, 0x27), (0x0C, 0x24), (0x0D, 0x24)):
        if i2c_read(i2c, address, reg)[0] != expected:
            raise RuntimeError('MAX30102 config readback reg=0x%02x' % reg)
    part_id = i2c_read(i2c, address, 0xFF)[0]
    return True, part_id


def init_touch(i2c):
    address = 0x15
    if address not in i2c.scan():
        return False, None
    # CST816S/CST816T chip ID register. Some revisions do not expose it;
    # presence on the bus is still a successful electrical initialization.
    try:
        chip_id = i2c_read(i2c, address, 0xA7)[0]
    except OSError:
        chip_id = None
    return True, chip_id


def _max30102_fifo_count(i2c):
    write_ptr = i2c_read(i2c, MAX30102_ADDR, 0x04)[0]
    read_ptr = i2c_read(i2c, MAX30102_ADDR, 0x06)[0]
    return (write_ptr - read_ptr) & 0x1F


# Signal processing is implemented by vitals.Vitals.



def classify_vital_emergency(heart_rate_bpm, spo2_percent):
    """Return 0=unknown, 1=normal, 2=warning, 3=critical.

    Prototype telemetry logic only; not a medical diagnosis.
    """
    hr_valid = heart_rate_bpm is not None and 0 < heart_rate_bpm < 250
    spo2_valid = spo2_percent is not None and 0 < spo2_percent <= 100
    if not hr_valid and not spo2_valid:
        return EMERGENCY_UNKNOWN

    critical = (
        (hr_valid and (heart_rate_bpm < HR_CRITICAL_LOW or heart_rate_bpm > HR_CRITICAL_HIGH)) or
        (spo2_valid and spo2_percent < SPO2_CRITICAL_LOW)
    )
    warning = (
        (hr_valid and (heart_rate_bpm < HR_WARNING_LOW or heart_rate_bpm > HR_WARNING_HIGH)) or
        (spo2_valid and spo2_percent < SPO2_WARNING_LOW)
    )
    if critical:
        return EMERGENCY_CRITICAL
    if warning:
        return EMERGENCY_WARNING
    return EMERGENCY_NORMAL if hr_valid and spo2_valid else EMERGENCY_UNKNOWN


def sample_max30102(i2c, max_samples=32, motion=None):
    count = min(_max30102_fifo_count(i2c), max_samples)
    if count <= 0:
        return 0, heart_state["last_ir"]
    fifo = i2c.readfrom_mem(MAX30102_ADDR, 0x07, count * 6)
    for index in range(count):
        raw = fifo[index*6:index*6+6]
        # In SpO2 mode each channel is a settings-bit 18-bit sample.
        red_value = ((raw[0] << 16) | (raw[1] << 8) | raw[2]) & 0x03FFFF
        ir_value = ((raw[3] << 16) | (raw[4] << 8) | raw[5]) & 0x03FFFF
        vitals.update(red_value, ir_value, motion)
        heart_state['last_ir'] = ir_value
        heart_state['sample_count'] += 1
    heart_state['bpm'], heart_state['spo2'] = vitals.bpm, vitals.spo2
    heart_state['quality'] = vitals.quality
    heart_state['sqi'] = dict(vitals.sqi.metrics)
    return count, heart_state["last_ir"]


def read_health(i2c, max30102_ok):
    if not max30102_ok:
        return (0xFF, 0xFF, 0), 0, 0, None, EMERGENCY_UNKNOWN
    # Only the MAX worker consumes the FIFO; caller holds its state lock.
    ir_value = heart_state['last_ir']
    bpm = heart_state["bpm"]
    bpm_valid = bpm is not None and ir_value > 12000
    spo2 = heart_state["spo2"]
    spo2_valid = spo2 is not None and ir_value > 12000
    phy_flag = (1 if bpm_valid else 0) | (2 if spo2_valid else 0)
    emergency_level = classify_vital_emergency(
        bpm if bpm_valid else None,
        spo2 if spo2_valid else None,
    )
    # Protocol physiology fields: heart rate BPM, SpO2, reserved.
    return (bpm if bpm_valid else 0xFF,
            spo2 if spo2_valid else 0xFF,
            0), phy_flag, ir_value, bpm, emergency_level


def build_beacon(seq, batt_percent_value, phy, phy_flag, emergency_level, message_type=1):
    name = PERSON_NAME.encode("ascii", "replace")[:31]
    # LB v1 | type=1 beacon / type=2 response | emergency | id | person |
    # physiology | no GPS | power | seq | RSSI unknown
    packet = bytearray(b"LB")
    packet.extend(struct.pack("<BBBIB", 1, message_type & 0xFF, emergency_level, device_id(), len(name)))
    packet.extend(name)
    packet.extend(struct.pack("<BBBB", phy_flag, phy[0], phy[1], phy[2]))
    packet.extend(b"\x00")  # location flag: no GPS in the provided GPIO map
    packet.extend(struct.pack("<BBBB", batt_percent_value, seq & 0xFF, 0x80, 0))
    packet.extend(struct.pack("<H", crc16_ccitt(packet)))
    return bytes(packet)


def build_call_packet(target_device_id=None):
    """Build the receiver-compatible short type-0 call frame.

    Layout is LB + version + 32-bit device ID + message type + CRC16-CCITT.
    The receiver recognizes this exact 10-byte form as ``short_cq``.
    """
    target = device_id() if target_device_id is None else int(target_device_id)
    packet = bytearray(b"LB\x01")
    packet.extend(struct.pack("<I", target & 0xFFFFFFFF))
    packet.append(0)
    packet.extend(struct.pack("<H", crc16_ccitt(packet)))
    return bytes(packet)


def init_rgb():
    if not RGB_ENABLED or NeoPixel is None:
        return None
    try:
        rgb = NeoPixel(Pin(RGB_PIN, Pin.OUT), 1)
        rgb[0] = (0, 0, 0)
        rgb.write()
        return rgb
    except Exception as exc:
        print('[rgb] disabled: %r' % exc)
        return None


def rgb_set(rgb, color):
    if rgb is None:
        return
    rgb[0] = tuple((value * RGB_BRIGHTNESS) // 255 for value in color)
    rgb.write()


def _hex_bytes(data):
    if not data:
        return "--"
    return " ".join("%02x" % value for value in data)


def print_payload_segments(packet):
    name_length = packet[9]
    name_start = 10
    physiology_start = name_start + name_length
    location_start = physiology_start + 4
    status_start = location_start + 1
    crc_start = len(packet) - 2
    fields = [
        ("magic", 0, 2),
        ("version", 2, 3),
        ("message_type", 3, 4),
        ("emergency_level", 4, 5),
        ("device_id_le", 5, 9),
        ("name_length", 9, 10),
        ("name_ascii", name_start, physiology_start),
        ("physiology_flags", physiology_start, physiology_start + 1),
        ("heart_rate_bpm", physiology_start + 1, physiology_start + 2),
        ("spo2_percent", physiology_start + 2, physiology_start + 3),
        ("physiology_reserved", physiology_start + 3, location_start),
        ("location_flag", location_start, status_start),
        ("battery_percent", status_start, status_start + 1),
        ("sequence", status_start + 1, status_start + 2),
        ("rssi_placeholder", status_start + 2, status_start + 3),
        ("reserved_1", status_start + 3, crc_start),
        ("crc16_ccitt_le", crc_start, len(packet)),
    ]
    print("[tx] payload fields (byte offsets, hex):")
    for label, start, end in fields:
        if end <= start:
            continue
        offset = "%02d" % start if end - start == 1 else "%02d-%02d" % (start, end - 1)
        print("[tx]   %-22s [%s] %s" %
              (label, offset, _hex_bytes(packet[start:end])))


def print_tx_parameters(seq, packet, frequency_hz=None):
    print('[tx] seq=%d frequency=%dHz bandwidth=%dkHz sf=%d cr=%s header=%s crc=%s power=%ddBm length=%dB' %
          (seq, LORA_FREQUENCY_HZ if frequency_hz is None else frequency_hz,
           LORA_BANDWIDTH_KHZ, LORA_SPREADING_FACTOR, LORA_CODING_RATE,
           LORA_HEADER_MODE, LORA_CRC_ENABLED, LORA_TX_POWER_DBM, len(packet)))
    print_payload_segments(packet)


def main():
    from pcb_runtime import run
    time.sleep(5)
    run(globals())


try:
    main()
except Exception as exc:
    print("[fatal]", repr(exc))
    raise

'''
py -3.11 -m mpremote connect COM7 fs cp sx1278.py :sx1278.py
py -3.11 -m mpremote connect COM7 fs cp pcb_config.py :pcb_config.py
py -3.11 -m mpremote connect COM7 fs cp pcb_screen.py :pcb_screen.py
py -3.11 -m mpremote connect COM7 fs cp pcb_sdcard.py :pcb_sdcard.py
py -3.11 -m mpremote connect COM7 fs cp pcb_runtime.py :pcb_runtime.py
py -3.11 -m mpremote connect COM7 fs cp main.py :main.py
py -3.11 -m mpremote connect COM7 reset
mpremote connect COM7 repl
'''
