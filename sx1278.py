"""Minimal SX1278 (Ra-02) LoRa driver for the PCB beacon test."""

from machine import Pin, SPI
import time
import pcb_config as cfg


class SX1278TxTimeoutError(RuntimeError):
    """Raised when the radio does not report TxDone before the deadline."""


class SX1278:
    REG_FIFO = 0x00
    REG_OP_MODE = 0x01
    REG_FRF_MSB = 0x06
    REG_FRF_MID = 0x07
    REG_FRF_LSB = 0x08
    REG_PA_CONFIG = 0x09
    REG_LNA = 0x0C
    REG_FIFO_ADDR_PTR = 0x0D
    REG_FIFO_TX_BASE_ADDR = 0x0E
    REG_FIFO_RX_BASE_ADDR = 0x0F
    REG_FIFO_RX_CURRENT_ADDR = 0x10
    REG_RX_NB_BYTES = 0x13
    REG_IRQ_FLAGS = 0x12
    REG_MODEM_CONFIG_1 = 0x1D
    REG_MODEM_CONFIG_2 = 0x1E
    REG_MODEM_CONFIG_3 = 0x26
    REG_PAYLOAD_LENGTH = 0x22
    REG_VERSION = 0x42

    MODE_LONG_RANGE = 0x80
    MODE_SLEEP = 0x00
    MODE_STDBY = 0x01
    MODE_TX = 0x03
    MODE_RX_CONTINUOUS = 0x05

    def __init__(self, spi, cs, reset, dio0, dio1=None):
        self.spi = spi
        self.cs = cs
        self.reset = reset
        self.dio0 = dio0
        self.dio1 = dio1
        self.tx_started_ms = None
        self.tx_timeout_ms = 5000
        self.cs.value(1)
        self.reset.value(1)

    def _read(self, address):
        self.cs.value(0)
        self.spi.write(bytes((address & 0x7F,)))
        value = self.spi.read(1, 0)[0]
        self.cs.value(1)
        return value

    def _write(self, address, value):
        self.cs.value(0)
        self.spi.write(bytes(((address & 0x7F) | 0x80, value & 0xFF)))
        self.cs.value(1)

    def _burst_write(self, address, data):
        self.cs.value(0)
        self.spi.write(bytes(((address & 0x7F) | 0x80,)) + data)
        self.cs.value(1)

    def _reset_chip(self):
        self.reset.value(0)
        time.sleep_ms(2)
        self.reset.value(1)
        time.sleep_ms(10)

    def begin(self, frequency_hz=433775000, tx_power_dbm=2):
        self._reset_chip()
        version = self._read(self.REG_VERSION)
        if version != 0x12:
            raise RuntimeError("SX1278 version mismatch: 0x%02x" % version)

        self._write(self.REG_OP_MODE, self.MODE_LONG_RANGE | self.MODE_SLEEP)
        time.sleep_ms(5)
        frf = int((frequency_hz << 19) // 32000000)
        self._write(self.REG_FRF_MSB, (frf >> 16) & 0xFF)
        self._write(self.REG_FRF_MID, (frf >> 8) & 0xFF)
        self._write(self.REG_FRF_LSB, frf & 0xFF)

        # BW=125 kHz, CR=4/5, explicit header; SF7, CRC enabled.
        self._write(self.REG_MODEM_CONFIG_1, 0x72)
        self._write(self.REG_MODEM_CONFIG_2, 0x74)
        self._write(self.REG_MODEM_CONFIG_3, 0x04)
        self._write(self.REG_LNA, 0x23)
        self.set_tx_power(tx_power_dbm)
        self._write(self.REG_FIFO_TX_BASE_ADDR, 0x00)
        self._write(self.REG_OP_MODE, self.MODE_LONG_RANGE | self.MODE_STDBY)
        self._write(0x40, 0x40)  # DIO0 mapping=01: TxDone (not reset default RxDone)
        self._write(self.REG_IRQ_FLAGS, 0xFF)
        return version

    def set_tx_power(self, tx_power_dbm):
        # PA_BOOST output mapping: 2 dBm through 17 dBm.
        tx_power_dbm = max(2, min(17, tx_power_dbm))
        self._write(self.REG_PA_CONFIG, 0x80 | (tx_power_dbm - 2))
        return tx_power_dbm

    def set_frequency(self, frequency_hz):
        """Retune in standby without resetting the chip or PHY settings."""
        if self.tx_started_ms is not None:
            raise RuntimeError('Cannot retune during TX')
        self._write(self.REG_OP_MODE, self.MODE_LONG_RANGE | self.MODE_STDBY)
        frf = (int(frequency_hz) << 19) // 32000000
        self._write(self.REG_FRF_MSB, (frf >> 16) & 0xFF)
        self._write(self.REG_FRF_MID, (frf >> 8) & 0xFF)
        self._write(self.REG_FRF_LSB, frf & 0xFF)

    def start_send(self, payload, timeout_ms=5000):
        """Start TX and return immediately; call poll_send() for completion."""
        if self.tx_started_ms is not None:
            raise RuntimeError('SX1278 TX already active')
        if len(payload) > 255:
            raise ValueError("LoRa payload exceeds 255 bytes")
        self._write(self.REG_OP_MODE, self.MODE_LONG_RANGE | self.MODE_STDBY)
        self._write(self.REG_FIFO_ADDR_PTR, 0x00)
        self._burst_write(self.REG_FIFO, payload)
        self._write(self.REG_PAYLOAD_LENGTH, len(payload))
        self._write(self.REG_IRQ_FLAGS, 0xFF)
        self._write(0x40, 0x40)  # DIO0 = TxDone
        self._write(self.REG_OP_MODE, self.MODE_LONG_RANGE | self.MODE_TX)
        self.tx_timeout_ms = timeout_ms
        self.tx_started_ms = time.ticks_ms()

    def poll_send(self):
        """Return True on completion, False while transmitting; raise on timeout."""
        if self.tx_started_ms is None:
            return True
        if self._read(self.REG_IRQ_FLAGS) & 0x08:
            self._write(self.REG_IRQ_FLAGS, 0x08)
            self._write(self.REG_OP_MODE, self.MODE_LONG_RANGE | self.MODE_STDBY)
            self.tx_started_ms = None
            return True
        if time.ticks_diff(time.ticks_ms(), self.tx_started_ms) > self.tx_timeout_ms:
            self._write(self.REG_OP_MODE, self.MODE_LONG_RANGE | self.MODE_STDBY)
            self.tx_started_ms = None
            raise SX1278TxTimeoutError('SX1278 TX timeout')
        return False

    def send(self, payload, timeout_ms=5000, poll=None):
        self.start_send(payload, timeout_ms)
        while not self.poll_send():
            if poll:
                poll()
            time.sleep_ms(2)

    def start_receive(self):
        """Enter continuous RX mode; receive_nonblocking() polls RxDone."""
        self._write(self.REG_FIFO_RX_BASE_ADDR, 0x00)
        self._write(self.REG_IRQ_FLAGS, 0xFF)
        self._write(0x40, 0x00)  # DIO0 = RxDone
        self._write(self.REG_OP_MODE, self.MODE_LONG_RANGE | self.MODE_RX_CONTINUOUS)

    def receive_nonblocking(self):
        """Return one valid received payload, or None when no frame is ready."""
        flags = self._read(self.REG_IRQ_FLAGS)
        if not (flags & 0x40):  # RxDone
            return None
        if flags & 0x20:  # Payload CRC error
            self._write(self.REG_IRQ_FLAGS, 0x60)
            return None
        length = self._read(self.REG_RX_NB_BYTES)
        current = self._read(self.REG_FIFO_RX_CURRENT_ADDR)
        self._write(self.REG_FIFO_ADDR_PTR, current)
        self.cs.value(0)
        self.spi.write(bytes((self.REG_FIFO & 0x7F,)))
        payload = self.spi.read(length, 0)
        self.cs.value(1)
        self._write(self.REG_IRQ_FLAGS, 0x40)
        # RX continuous remains active: do not clear all IRQs or re-enter RX
        # after each packet, which can discard a following CQ.
        return bytes(payload)


def make_radio(spi=None):
    # MicroPython bus IDs are 1/2, corresponding to ESP32-S3 SPI2/SPI3.
    if spi is None:
        spi = SPI(cfg.LORA_SPI_ID, baudrate=cfg.LORA_SPI_HZ, polarity=0, phase=0,
                  sck=Pin(cfg.LORA_SCK), mosi=Pin(cfg.LORA_MOSI), miso=Pin(cfg.LORA_MISO))
    return SX1278(
        spi=spi,
        cs=Pin(cfg.LORA_CS, Pin.OUT, value=1),
        reset=Pin(cfg.LORA_RST, Pin.OUT, value=1),
        dio0=Pin(cfg.LORA_DIO0, Pin.IN),
        dio1=Pin(cfg.LORA_DIO1, Pin.IN),
    )
