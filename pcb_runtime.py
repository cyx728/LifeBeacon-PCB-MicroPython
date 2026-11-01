"""PCB orchestration: two sensor workers, main-thread I/O and display.

Lock order is MAX state -> I2C. No logging/display while either is held.
Workers publish immutable tuples; only main thread prints or draws.
"""
import time
import struct
try:
    import _thread
except ImportError:
    _thread = None
import os
from machine import Pin, I2C, SPI
import pcb_config as cfg


class NoThreadLock:
    """No worker can run if _thread is absent; main may still test other I/O."""
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class UnusedPin:
    def value(self, value=None):
        return 1

    def irq(self, **kwargs):
        pass


class Runtime:
    def __init__(self, api):
        self.api = api
        self.i2c_lock = _thread.allocate_lock() if _thread else NoThreadLock()
        self.max_lock = _thread.allocate_lock() if _thread else NoThreadLock()
        self.stop = False
        self.max_done = self.mpu_done = True
        self.max_status = (False, 'pending')
        self.mpu_status = (False, 'pending')
        self.max_last_ms = self.max_alive_ms = self.mpu_alive_ms = None
        self.mpu_data = None
        self.screen = None
        self.last_render = time.ticks_ms()
        self.last_status = None
        self.rgb = None
        self.rgb_off_at = None
        standalone = getattr(cfg, 'STANDALONE', False)
        self.tf_cs = UnusedPin() if standalone else Pin(cfg.TF_CS, Pin.OUT, value=1)
        self.lora_cs = Pin(cfg.LORA_CS, Pin.OUT, value=1)
        self.buttons = [] if standalone else [Pin(cfg.BTN1, Pin.IN, Pin.PULL_UP), Pin(cfg.BTN2, Pin.IN, Pin.PULL_UP)]
        self.raw_buttons = [pin.value() for pin in self.buttons]
        self.stable_buttons = [None, None]
        self.button_changed = [time.ticks_ms(), time.ticks_ms()]
        self.touch_irq = Pin(cfg.TP_INT, Pin.IN, Pin.PULL_UP)
        self.hr_irq = Pin(cfg.HR_INT, Pin.IN, Pin.PULL_UP)
        self.mpu_irq = UnusedPin() if standalone else Pin(cfg.MPU_INT, Pin.IN, Pin.PULL_UP)
        self.touch_pending = bytearray(1)
        self.touch_ok = False
        self.touch_down = False
        self.touch_last_poll = time.ticks_ms()
        self.touch_last_point = None
        self.touch_error = None
        self.i2c = None
        self.spi = None
        self.radio = None
        self.response_seq = 0
        self.response_active = False
        self.response_due_ms = None
        self.response_led_next_ms = None
        self.response_led_on = False
        self.periodic_tx_active = False
        self.adc = None
        self.motion_ref = None

    def touch_edge(self, pin):
        # IRQ callback: latch only, never I2C/print/allocate.
        self.touch_pending[0] = 1

    def log(self, text, level='INFO'):
        colors = {'INFO': '37', 'OK': '32', 'WARN': '33', 'ERROR': '31'}
        if cfg.CONSOLE_COLOR:
            print('\x1b[%sm%s\x1b[0m' % (colors.get(level, '37'), text))
        else:
            print(text)
        if self.screen:
            self.screen.append(text, level)

    def attempt(self, label, operation):
        try:
            return operation()
        except Exception as exc:
            self.log('[%s] self-test failed: %r' % (label, exc), 'ERROR')
            return None

    def screen_init(self):
        Pin(cfg.SCREEN_BL, Pin.OUT, value=cfg.SCREEN_BL_ON)
        from pcb_screen import ScreenLog
        self.screen = ScreenLog()
        self.log('[scn] ST7789V 240x280 init sent; backlight ON', 'OK')
        self.log('[scn] color bars/logs need visual confirmation')

    def i2c_init(self):
        self.i2c = I2C(0, scl=Pin(cfg.I2C_SCL), sda=Pin(cfg.I2C_SDA), freq=cfg.I2C_HZ)
        self.log('[i2c] SDA5 SCL6 100kHz: %s' % [hex(x) for x in self.i2c.scan()])

    def touch_init(self):
        if getattr(cfg, 'STANDALONE', False):
            ok, chip = self.api['init_touch'](self.i2c)
            self.touch_ok = ok
            self.log('[touch] present=%s id=%s' % (ok, chip))
            return
        reset = Pin(cfg.TOUCH_RST, Pin.OUT, value=0)
        time.sleep_ms(10)
        reset.value(1)
        time.sleep_ms(100)
        with self.i2c_lock:
            # Direct reads wake/probe CST816T even if a sleeping chip skips scan.
            chip = self.i2c.readfrom_mem(0x15, 0xA7, 1)[0]
            self.i2c.readfrom_mem(0x15, 0x01, 6)
        try:
            self.touch_irq.irq(trigger=Pin.IRQ_FALLING, handler=self.touch_edge)
        except Exception as exc:
            self.log('[touch] IRQ registration unavailable: %r' % exc, 'WARN')
        self.touch_ok = True
        self.log('[touch] CST816T ID=0x%02x INT=%d RST33' % (chip, self.touch_irq.value()), 'OK')

    def spi_init(self):
        self.spi = SPI(cfg.LORA_SPI_ID, baudrate=cfg.LORA_SPI_HZ,
                       polarity=0, phase=0, sck=Pin(cfg.LORA_SCK),
                       mosi=Pin(cfg.LORA_MOSI), miso=Pin(cfg.LORA_MISO))

    def tf_test(self):
        from pcb_sdcard import SDCard
        self.lora_cs.value(1)
        mounted = False
        try:
            card = SDCard(self.spi, self.tf_cs)
            vfs = os.VfsFat(card)
            os.mount(vfs, '/sd')
            mounted = True
            message = 'Hello from ESP32-S3 PCB test!\n'
            with open('/sd/Hello.txt', 'w') as stream:
                stream.write(message)
                stream.flush()
            if hasattr(os, 'sync'):
                os.sync()
            with open('/sd/Hello.txt', 'r') as stream:
                if stream.read() != message:
                    raise OSError('Hello.txt readback mismatch')
            self.log('[tf] CS35 Hello.txt write/read PASS (%d sectors)' % card.sectors, 'OK')
        finally:
            try:
                if mounted:
                    os.umount('/sd')
            finally:
                self.tf_cs.value(1)
                self.spi.init(baudrate=cfg.LORA_SPI_HZ, polarity=0, phase=0)
        self.log('[tf] unmounted; file retained, SPI returned to LoRa')

    def max_worker(self):
        self.max_done = False
        try:
            with self.i2c_lock:
                ok, part = self.api['init_max30102'](self.i2c)
            if not ok:
                raise OSError('MAX30102 0x57 missing')
            self.max_status = (True, 'PART_ID=0x%02x config verified' % part)
            while not self.stop:
                # Single FIFO consumer, protects both algorithms and I2C sequence.
                with self.max_lock:
                    with self.i2c_lock:
                        if self.max_alive_ms is not None and time.ticks_diff(time.ticks_ms(), self.max_alive_ms) > 250:
                            for reg in (4, 5, 6):
                                self.i2c.writeto_mem(0x57, reg, b'\x00')
                            self.invalidate_health()
                        overflow = self.i2c.readfrom_mem(0x57, 5, 1)[0]
                        if overflow:
                            for reg in (4, 5, 6):
                                self.i2c.writeto_mem(0x57, reg, b'\x00')
                            self.invalidate_health()
                            count = 0
                        else:
                            motion = self.motion_ref if self.mpu_alive_ms is not None and time.ticks_diff(time.ticks_ms(), self.mpu_alive_ms) < 200 else None
                            count, ir = self.api['sample_max30102'](self.i2c, motion=motion)
                    now = time.ticks_ms()
                    self.max_alive_ms = now
                    if count:
                        self.max_last_ms = now
                        s = self.api['heart_state']
                        if ir < 12000:
                            self.invalidate_health()
                        elif s['last_peak_ms'] is not None and time.ticks_diff(now, s['last_peak_ms']) > 3000:
                            s['bpm'] = None
                    self.max_status = (True, 'FIFO overflow: samples reset' if overflow else 'sampling 100sps')
                time.sleep_ms(20)
        except Exception as exc:
            with self.max_lock:
                self.invalidate_health()
                self.max_status = (False, repr(exc))
        finally:
            self.max_done = True

    def invalidate_health(self):
        self.api['vitals'].reset()
        s = self.api['heart_state']
        s.update({'bpm': None, 'spo2': None, 'last_peak_ms': None,
                  'baseline': 0, 'amplitude': 0, 'above': False,
                  'red_sum': 0, 'ir_sum': 0, 'red_min': 0x3FFFF,
                  'red_max': 0, 'ir_min': 0x3FFFF, 'ir_max': 0, 'spo2_count': 0})

    def mpu_worker(self):
        self.mpu_done = False
        try:
            with self.i2c_lock:
                addresses = self.i2c.scan()
                addr = 0x68 if 0x68 in addresses else 0x69
                who = self.i2c.readfrom_mem(addr, 0x75, 1)[0]
                if who & 0x7E != 0x68:
                    raise OSError('MPU WHO_AM_I=0x%02x' % who)
                self.i2c.writeto_mem(addr, 0x6B, b'\x80')
            time.sleep_ms(100)
            with self.i2c_lock:
                for reg, value in ((0x6B,1),(0x6C,0),(0x1A,3),(0x19,9),
                                   (0x1B,0),(0x1C,0),(0x37,0x20),(0x38,1)):
                    self.i2c.writeto_mem(addr, reg, bytes((value,)))
                    if self.i2c.readfrom_mem(addr, reg, 1)[0] != value:
                        raise OSError('MPU config readback 0x%02x' % reg)
            self.mpu_status = (True, 'addr=0x%02x WHO=0x%02x config verified' % (addr, who))
            while not self.stop:
                with self.i2c_lock:
                    raw = self.i2c.readfrom_mem(addr, 0x3B, 14)
                    self.i2c.readfrom_mem(addr, 0x3A, 1)  # acknowledge interrupt
                ax, ay, az, temp, gx, gy, gz = struct.unpack('>hhhhhhh', raw)
                self.mpu_data = (ax / 16384, ay / 16384, az / 16384,
                                 gx / 131, gy / 131, gz / 131, temp / 340 + 36.53)
                self.mpu_alive_ms = time.ticks_ms()
                self.motion_ref = (self.mpu_data[0]**2 + self.mpu_data[1]**2 + self.mpu_data[2]**2)**0.5 - 1.0
                time.sleep_ms(20)
        except Exception as exc:
            self.mpu_status = (False, repr(exc))
            self.mpu_data = None
        finally:
            self.mpu_done = True

    def start_workers(self):
        if _thread is None:
            self.max_status = self.mpu_status = (False, '_thread missing; sensor threads cannot run')
            self.log('[thread] firmware lacks _thread; MAX/MPU disabled', 'ERROR')
            return
        # Both workers remain separate from main. MicroPython builds differ in
        # supported thread count: report creation failures rather than hide them.
        for label, worker in (('max', self.max_worker), ('mpu', self.mpu_worker)):
            if label == 'mpu' and getattr(cfg, 'STANDALONE', False):
                continue
            setattr(self, label + '_done', False)
            try:
                _thread.start_new_thread(worker, ())
                self.log('[%s] dedicated thread started' % label, 'OK')
            except Exception as exc:
                setattr(self, label + '_done', True)
                setattr(self, label + '_status', (False, 'thread start: %r' % exc))
                self.log('[%s] thread unavailable: %r' % (label, exc), 'ERROR')

    def inputs(self):
        now = time.ticks_ms()
        for index, pin in enumerate(self.buttons):
            value = pin.value()
            if value != self.raw_buttons[index]:
                self.raw_buttons[index] = value
                self.button_changed[index] = now
            if value != self.stable_buttons[index] and time.ticks_diff(now, self.button_changed[index]) >= 35:
                self.stable_buttons[index] = value
                self.log('[btn] SW%d %s' % (index + 1, 'PRESSED' if value == cfg.BUTTON_ACTIVE else 'released'), 'OK')
        pending = self.touch_pending[0]
        self.touch_pending[0] = 0
        if pending and not self.touch_ok:
            self.log('[touch] INT falling edge (controller not initialized)', 'WARN')
        due = time.ticks_diff(now, self.touch_last_poll) >= (50 if self.touch_down else 200)
        if self.touch_ok and (pending or due):
            self.touch_last_poll = now
            try:
                with self.i2c_lock:
                    data = self.i2c.readfrom_mem(0x15, 0x01, 6)
                gesture, fingers = data[0], data[1] & 15
                x, y = ((data[2] & 15) << 8) | data[3], ((data[4] & 15) << 8) | data[5]
                point = (x, y)
                if fingers and (not self.touch_down or point != self.touch_last_point):
                    self.log('[touch] DOWN/MOVE x=%d y=%d gesture=0x%02x' % (x, y, gesture), 'OK')
                elif not fingers and self.touch_down:
                    self.log('[touch] released', 'OK')
                elif pending:
                    self.log('[touch] IRQ gesture=0x%02x fingers=%d' % (gesture, fingers))
                self.touch_down, self.touch_last_point = bool(fingers), point
                self.touch_error = None
            except OSError as exc:
                # A sleeping CST816T may NACK polling; report on actual touch IRQ.
                if pending and repr(exc) != self.touch_error:
                    self.log('[touch] read failed: %r' % exc, 'WARN')
                    self.touch_error = repr(exc)

    def service(self):
        self.poll_radio()
        self.inputs()
        now = time.ticks_ms()
        if self.response_due_ms is not None or self.response_active:
            if self.response_led_next_ms is None or time.ticks_diff(now, self.response_led_next_ms) >= 0:
                self.response_led_on = not self.response_led_on
                self.api['rgb_set'](self.rgb, (255, 0, 0) if self.response_led_on else (0, 0, 0))
                self.response_led_next_ms = time.ticks_add(now, 250)
        statuses = (self.max_status, self.mpu_status)
        if statuses != self.last_status:
            for label, value in zip(('max', 'mpu'), statuses):
                self.log('[%s] %s: %s' % (label, 'OK' if value[0] else 'WAIT/FAIL', value[1]), 'OK' if value[0] else 'WARN')
            self.last_status = statuses
        if self.rgb_off_at is not None and time.ticks_diff(now, self.rgb_off_at) >= 0:
            self.api['rgb_set'](self.rgb, (0, 0, 0))
            self.rgb_off_at = None
        if self.screen and time.ticks_diff(now, self.last_render) >= 100:
            try:
                self.screen.render()
            except Exception as exc:
                self.screen = None
                self.log('[scn] render failed: %r' % exc, 'ERROR')
            self.last_render = now

    def health_snapshot(self, adc):
        """Take the same telemetry snapshot used by periodic beacon frames."""
        with self.max_lock:
            fresh = (self.max_status[0] and self.max_last_ms is not None and
                     time.ticks_diff(time.ticks_ms(), self.max_last_ms) < 1000)
            if not fresh:
                self.invalidate_health()
            phy, flags, ir, bpm, emergency = self.api['read_health'](None, fresh)
            sample_count = self.api['heart_state']['sample_count']
        mv = self.attempt('battery', lambda: self.api['battery_mv'](adc)) if adc else None
        battery = self.api['battery_percent'](mv) if mv is not None else 0xFF
        return phy, flags, ir, bpm, emergency, sample_count, mv, battery

    def poll_radio(self):
        if self.radio is None:
            return
        if self.response_active:
            self.poll_response()
            return
        if self.periodic_tx_active:
            return
        if self.response_due_ms is not None and time.ticks_diff(time.ticks_ms(), self.response_due_ms) >= 0:
            self.response_due_ms = None
            self.send_response()
            return
        try:
            packet = self.radio.receive_nonblocking()
        except Exception as exc:
            self.log('[lora] RxFailed %r' % (exc,), 'WARN')
            return
        if not packet:
            return
        # Receiver-compatible short CQ: LB + version + device ID + type 0 + CRC.
        if len(packet) == 10 and packet[:3] == b'LB\x01' and packet[7] == 0:
            expected = struct.unpack('<H', packet[-2:])[0]
            actual = self.api['crc16_ccitt'](packet[:-2])
            if expected != actual:
                self.log('[rx] ignored short CQ CRC mismatch expected=0x%04x actual=0x%04x' %
                         (expected, actual), 'WARN')
                return
            target = struct.unpack('<I', packet[3:7])[0]
            self.log('[rx] short CQ type=0 target=0x%08x length=10 payload=%s' %
                     (target, self.api['_hex_bytes'](packet)))
            self.schedule_response()
            return
        # Full LB v1 packet: magic[0:2], version[2], message_type[3], CRC[tail].
        if len(packet) < 7 or packet[0:2] != b'LB' or packet[2] != 1:
            self.log('[rx] ignored malformed frame length=%d' % len(packet), 'WARN')
            return
        expected = struct.unpack('<H', packet[-2:])[0]
        actual = self.api['crc16_ccitt'](packet[:-2])
        if expected != actual:
            self.log('[rx] ignored CRC mismatch expected=0x%04x actual=0x%04x' %
                     (expected, actual), 'WARN')
            return
        message_type = packet[3]
        self.log('[rx] type=%d length=%d payload=%s' %
                 (message_type, len(packet), self.api['_hex_bytes'](packet)))
        if message_type == 0:
            self.schedule_response()

    def schedule_response(self):
        # Merge repeated CQ during the wait without extending the first deadline.
        if self.response_due_ms is None:
            delay = self.api['LORA_RESPONSE_DELAY_MS']
            self.response_due_ms = time.ticks_add(time.ticks_ms(), delay)
            self.response_led_on = True
            self.rgb_off_at = None
            self.response_led_next_ms = time.ticks_add(time.ticks_ms(), 250)
            self.api['rgb_set'](self.rgb, (255, 0, 0))
            self.log('[response] scheduled type=2 delay=%dms frequency=%dHz' %
                     (delay, self.api['LORA_RESPONSE_FREQUENCY_HZ']))

    def send_response(self):
        if self.radio is None or self.response_active or self.periodic_tx_active:
            return
        phy, flags, ir, bpm, emergency, sample_count, mv, battery = self.health_snapshot(self.adc)
        packet = self.api['build_beacon'](self.response_seq, battery, phy, flags,
                                          emergency, message_type=2)
        self.log('[response] type=2 seq=%d HR=%s SpO2=%s emergency=%d' %
                 (self.response_seq, phy[0] if flags & 1 else '--',
                  phy[1] if flags & 2 else '--', emergency), 'OK')
        frequency = self.api['LORA_RESPONSE_FREQUENCY_HZ']
        self.api['print_tx_parameters'](self.response_seq, packet, frequency_hz=frequency)
        # Keep the alert indication red from CQ detection through TxDone.
        self.api['rgb_set'](self.rgb, (255, 0, 0))
        self.rgb_off_at = None
        try:
            self.tf_cs.value(1)
            self.radio.set_frequency(frequency)
            self.radio.start_send(packet)
            self.response_active = True
            self.log('[lora] ResponseTxStarted type=2 seq=%d frequency=%dHz' %
                     (self.response_seq, frequency), 'OK')
        except Exception as exc:
            self.finish_response(exc)

    def poll_response(self):
        """One IRQ check per service cycle; no TX wait loop or radio recursion."""
        try:
            if not self.radio.poll_send():
                return
        except Exception as exc:
            self.finish_response(exc)
            return
        self.finish_response()

    def finish_response(self, error=None):
        self.response_active = False
        self.response_due_ms = None
        self.response_led_next_ms = None
        self.response_led_on = False
        if error is None:
            self.log('[lora] ResponseTxDone type=2 seq=%d' % self.response_seq, 'OK')
            self.api['rgb_set'](self.rgb, (0, 255, 0))
            self.rgb_off_at = time.ticks_add(time.ticks_ms(), 150)
        else:
            self.log('[lora] ResponseTxFailed type=2 %r' % (error,), 'ERROR')
            self.api['rgb_set'](self.rgb, (255, 0, 0))
            self.rgb_off_at = time.ticks_add(time.ticks_ms(), 600)
        try:
            self.radio.set_frequency(self.api['LORA_FREQUENCY_HZ'])
            self.radio.start_receive()
            self.log('[lora] RX resumed frequency=%dHz' % self.api['LORA_FREQUENCY_HZ'])
        except Exception as exc:
            self.log('[lora] RX restart failed after response: %r' % (exc,), 'WARN')
        self.response_seq = (self.response_seq + 1) & 255

    def close(self):
        self.stop = True
        self.touch_irq.irq(handler=None)
        self.tf_cs.value(1)
        self.lora_cs.value(1)
        self.api['rgb_set'](self.rgb, (0, 0, 0))
        deadline = time.ticks_add(time.ticks_ms(), 2000)
        while not (self.max_done and self.mpu_done) and time.ticks_diff(deadline, time.ticks_ms()) > 0:
            time.sleep_ms(20)
        if not (self.max_done and self.mpu_done):
            self.log('[thread] stop pending; soft reset before restarting', 'WARN')

    def run(self):
        self.log('[boot] PCB test; TF_CS35 / RGB48')
        if not getattr(cfg, 'STANDALONE', False):
            self.attempt('screen', self.screen_init)
        self.attempt('i2c', self.i2c_init)
        self.rgb = self.attempt('rgb', self.api['init_rgb'])
        self.log('[rgb] GPIO48 %s' % ('ready' if self.rgb else 'unavailable'))
        adc = self.attempt('battery', self.api['init_battery'])
        if self.i2c:
            self.attempt('touch', self.touch_init)
            self.start_workers()
        self.attempt('spi', self.spi_init)
        radio = None
        if self.spi:
            if not getattr(cfg, 'STANDALONE', False):
                self.attempt('tf', self.tf_test)
            def radio_init():
                r = self.api['make_radio'](self.spi)
                version = r.begin(self.api['LORA_FREQUENCY_HZ'], self.api['LORA_TX_POWER_DBM'])
                self.log('[lora] VERSION=0x%02x PASS' % version, 'OK')
                return r
            radio = self.attempt('lora', radio_init)
            self.radio = radio
            if self.radio:
                self.radio.start_receive()
                self.log('[lora] RX continuous ready', 'OK')
        seq = 0
        self.adc = adc
        next_tx = time.ticks_add(time.ticks_ms(), 500)
        while not self.stop:
            self.service()
            if self.response_active or self.response_due_ms is not None:
                time.sleep_ms(2)
                continue
            if time.ticks_diff(time.ticks_ms(), next_tx) < 0:
                time.sleep_ms(2)
                continue
            # Each frame uses one coherent snapshot from the MAX worker.
            with self.max_lock:
                fresh = self.max_status[0] and self.max_last_ms is not None and time.ticks_diff(time.ticks_ms(), self.max_last_ms) < 1000
                if not fresh:
                    self.invalidate_health()
                phy, flags, ir, bpm, emergency = self.api['read_health'](None, fresh)
                s = self.api['heart_state']
                sample_count = s['sample_count']
            self.log('[max] samples=%d IR=%d HR=%s SpO2=%s emergency=%d INT=%d' %
                     (sample_count, ir, phy[0] if flags & 1 else '--', phy[1] if flags & 2 else '--', emergency, self.hr_irq.value()))
            self.log('[max] quality=%s' % self.api['heart_state'].get('quality', 'warming'))
            if self.mpu_data is not None and self.mpu_alive_ms is not None and time.ticks_diff(time.ticks_ms(), self.mpu_alive_ms) < 1000:
                self.log('[mpu] a(g)=%.2f,%.2f,%.2f gyro(d/s)=%.1f,%.1f,%.1f T=%.1f INT=%d' % (self.mpu_data + (self.mpu_irq.value(),)))
            mv = self.attempt('battery', lambda: self.api['battery_mv'](adc)) if adc else None
            battery = self.api['battery_percent'](mv) if mv is not None else 0xFF
            self.log('[battery] %s mV (%s%%)' % (mv if mv is not None else '--', battery if battery != 0xFF else '--'))
            packet = self.api['build_beacon'](seq, battery, phy, flags, emergency)
            self.api['print_tx_parameters'](seq, packet)
            self.log('[tx] seq=%d frequency=%dHz BW125 SF7 CR4/5 6dBm %dB' %
                     (seq, self.api['LORA_FREQUENCY_HZ'], len(packet)))
            if radio:
                self.api['rgb_set'](self.rgb, self.api['RGB_TX_COLOR'])
                try:
                    self.tf_cs.value(1)
                    radio.set_frequency(self.api['LORA_FREQUENCY_HZ'])
                    self.periodic_tx_active = True
                    radio.send(packet, poll=self.service)
                    self.log('[lora] TxDone seq=%d' % seq, 'OK')
                    self.api['rgb_set'](self.rgb, (0, 255, 0))
                    self.rgb_off_at = time.ticks_add(time.ticks_ms(), 150)
                except Exception as exc:
                    self.log('[lora] TxFailed seq=%d %r' % (seq, exc), 'ERROR')
                    self.api['rgb_set'](self.rgb, (255, 0, 0))
                    self.rgb_off_at = time.ticks_add(time.ticks_ms(), 600)
                finally:
                    self.periodic_tx_active = False
                try:
                    self.radio.start_receive()
                except Exception as exc:
                    self.log('[lora] RX restart failed: %r' % (exc,), 'WARN')
            else:
                self.log('[lora] transmit skipped: self-test failed', 'WARN')
            seq = (seq + 1) & 255
            if self.api['ONE_SHOT']:
                return
            # No burst/catch-up. 2000ms target between starts; overruns skip ahead.
            next_tx = time.ticks_add(next_tx, self.api['PERIOD_MS'])
            if time.ticks_diff(time.ticks_ms(), next_tx) >= 0:
                next_tx = time.ticks_add(time.ticks_ms(), self.api['PERIOD_MS'])


def run(api):
    runtime = Runtime(api)
    try:
        runtime.run()
    finally:
        runtime.close()
