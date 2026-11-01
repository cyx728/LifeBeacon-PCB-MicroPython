"""Small ST7789V driver + white-background colored scrolling ASCII log.

P169H002-CTP: 240x280, 4-wire SPI. Only the main thread touches the LCD.
The display interface is write-only: init success cannot verify physical pixels.
"""
from machine import Pin, SPI
import framebuf
import struct
import time
import pcb_config as cfg


class ScreenLog:
    def __init__(self):
        self.bl = Pin(cfg.SCREEN_BL, Pin.OUT, value=cfg.SCREEN_BL_ON)
        self.cs = Pin(cfg.SCREEN_CS, Pin.OUT, value=1)
        self.dc = Pin(cfg.SCREEN_DC, Pin.OUT, value=0)
        self.rst = Pin(cfg.SCREEN_RST, Pin.OUT, value=1)
        self.spi = SPI(cfg.SCREEN_SPI_ID, baudrate=cfg.SCREEN_SPI_HZ,
                       polarity=0, phase=0, sck=Pin(cfg.SCREEN_SCK),
                       mosi=Pin(cfg.SCREEN_MOSI))
        self.width, self.height = cfg.SCREEN_WIDTH, cfg.SCREEN_HEIGHT
        # One text row, rather than a 134 kB full-screen framebuffer.
        self.buf = bytearray(self.width * 12 * 2)
        self.fb = framebuf.FrameBuffer(self.buf, self.width, 12, framebuf.RGB565)
        self.lines = []
        self.rows = (self.height - 24) // 12
        self.dirty = True
        self.rst.value(0)
        time.sleep_ms(20)
        self.rst.value(1)
        time.sleep_ms(120)
        self.command(0x01)
        time.sleep_ms(150)
        self.command(0x11)
        time.sleep_ms(120)
        self.command(0x3A, b'\x55')  # RGB565
        self.command(0x36, bytes((cfg.SCREEN_MADCTL,)))
        self.command(0x21 if cfg.SCREEN_INVERT else 0x20)
        self.command(0x13)
        self.command(0x29)
        time.sleep_ms(20)
        # Visible color bars are the optical self-test; then logs replace them.
        for y in range(0, self.height, 12):
            self.draw_row(y, '', (0xF800, 0x07E0, 0x001F, 0xFFFF)[min(3, y * 4 // self.height)])
        time.sleep_ms(180)

    def command(self, cmd, data=None):
        self.cs.value(0)
        try:
            self.dc.value(0)
            self.spi.write(bytes((cmd,)))
            if data:
                self.dc.value(1)
                self.spi.write(data)
        finally:
            self.cs.value(1)

    def draw_row(self, y, text, background=0xFFFF, foreground=0x0000):
        # framebuf stores little-endian RGB565, ST7789 consumes MSB first.
        swap = lambda c: ((c & 255) << 8) | (c >> 8)
        self.fb.fill(swap(background))
        self.fb.text(text, 8, 2, swap(foreground))
        h = min(12, self.height - y)
        self.command(0x2A, struct.pack('>HH', 0, self.width - 1))
        self.command(0x2B, struct.pack('>HH', y + cfg.SCREEN_Y_OFFSET,
                                    y + cfg.SCREEN_Y_OFFSET + h - 1))
        self.command(0x2C, memoryview(self.buf)[:self.width * h * 2])

    def append(self, message, level):
        color = {'INFO': 0x0010, 'OK': 0x03A0, 'WARN': 0xA300, 'ERROR': 0xD800}.get(level, 0)
        text = ''.join(c if 32 <= ord(c) < 127 else '?' for c in message)
        width = (self.width - 16) // 8
        for start in range(0, max(1, len(text)), width):
            self.lines.append((text[start:start + width], color))
        self.lines = self.lines[-self.rows:]
        self.dirty = True

    def render(self):
        if not self.dirty:
            return
        self.draw_row(0, 'PCB TEST  ST7789V', 0x0010, 0xFFFF)
        self.draw_row(12, 'MAX MPU TF LoRa BTN TP', 0xFFFF, 0)
        for index in range(self.rows):
            text, color = self.lines[index] if index < len(self.lines) else ('', 0)
            self.draw_row(24 + index * 12, text, 0xFFFF, color)
        self.dirty = False
