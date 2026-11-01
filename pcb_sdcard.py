"""Minimal SD SPI block device (SD v1/v2, SDHC), 512-byte read/write.

Caller supplies the already-created LoRa SPI bus; no extra host is allocated.
This driver never formats the card. Timeouts raise OSError.
"""
import time


class SDCard:
    def __init__(self, spi, cs):
        self.spi, self.cs = spi, cs
        self.cs.value(1)
        self.address_factor = 512
        self.sectors = 0
        spi.init(baudrate=100000, polarity=0, phase=0)
        try:
            spi.write(b'\xff' * 10)
            for _ in range(10):
                if self.command(0, 0, 0x95)[0] == 1:
                    break
            else:
                raise OSError('SD CMD0 failed')
            response, extra = self.command(8, 0x1AA, 0x87, extra=4)
            if response == 1:
                if extra[2:] != b'\x01\xaa':
                    raise OSError('SD voltage/check pattern')
                version2 = True
            elif response & 4:
                version2 = False
            else:
                raise OSError('SD CMD8 failed')
            deadline = time.ticks_add(time.ticks_ms(), 2000)
            while True:
                self.command(55, 0)
                if self.command(41, 0x40000000 if version2 else 0)[0] == 0:
                    break
                if time.ticks_diff(deadline, time.ticks_ms()) <= 0:
                    raise OSError('SD ACMD41 timeout')
                time.sleep_ms(10)
            if version2:
                r, ocr = self.command(58, 0, extra=4)
                if r != 0:
                    raise OSError('SD CMD58 failed')
                if ocr[0] & 0x40:
                    self.address_factor = 1
            if self.address_factor == 512 and self.command(16, 512)[0] != 0:
                raise OSError('SD CMD16 failed')
            r, _ = self.command(9, 0, hold=True)
            try:
                if r:
                    raise OSError('SD CMD9 failed')
                self.wait_token(0xFE)
                csd = spi.read(16, 0xFF)
                spi.read(2, 0xFF)
            finally:
                self.release()
            if csd[0] >> 6 == 1:
                size = ((csd[7] & 63) << 16) | (csd[8] << 8) | csd[9]
                self.sectors = (size + 1) * 1024
            elif csd[0] >> 6 == 0:
                size = ((csd[6] & 3) << 10) | (csd[7] << 2) | (csd[8] >> 6)
                mult = ((csd[9] & 3) << 1) | (csd[10] >> 7)
                self.sectors = ((size + 1) * (1 << (mult + 2)) * (1 << (csd[5] & 15))) // 512
            else:
                raise OSError('Unsupported SD CSD')
        finally:
            self.release()
            spi.init(baudrate=10000000, polarity=0, phase=0)

    def release(self):
        self.cs.value(1)
        self.spi.write(b'\xff')

    def command(self, cmd, arg, crc=1, extra=0, hold=False):
        self.cs.value(0)
        try:
            self.spi.write(bytes((0x40 | cmd, (arg >> 24) & 255, (arg >> 16) & 255,
                                  (arg >> 8) & 255, arg & 255, crc)))
            for _ in range(100):
                result = self.spi.read(1, 0xFF)[0]
                if result & 0x80 == 0:
                    tail = self.spi.read(extra, 0xFF) if extra else b''
                    return result, tail
            raise OSError('SD CMD%d response timeout' % cmd)
        except Exception:
            self.release()
            raise
        finally:
            if not hold:
                self.release()

    def wait_token(self, value):
        deadline = time.ticks_add(time.ticks_ms(), 1000)
        while time.ticks_diff(deadline, time.ticks_ms()) > 0:
            b = self.spi.read(1, 0xFF)[0]
            if b == value:
                return
            time.sleep_ms(1)
        raise OSError('SD token/busy timeout')

    def readblocks(self, block, buf):
        view = memoryview(buf)
        for offset in range(0, len(buf), 512):
            r, _ = self.command(17, block * self.address_factor, hold=True)
            try:
                if r:
                    raise OSError('SD CMD17 error %d' % r)
                self.wait_token(0xFE)
                self.spi.readinto(view[offset:offset + 512], 0xFF)
                self.spi.read(2, 0xFF)
            finally:
                self.release()
            block += 1
        return 0

    def writeblocks(self, block, buf):
        view = memoryview(buf)
        for offset in range(0, len(buf), 512):
            r, _ = self.command(24, block * self.address_factor, hold=True)
            try:
                if r:
                    raise OSError('SD CMD24 error %d' % r)
                self.spi.write(b'\xff\xfe')
                self.spi.write(view[offset:offset + 512])
                self.spi.write(b'\xff\xff')
                response = self.spi.read(1, 0xFF)[0]
                if response & 0x1F != 5:
                    raise OSError('SD write rejected: %d' % response)
                self.wait_token(0xFF)
            finally:
                self.release()
            block += 1
        return 0

    def ioctl(self, op, arg):
        if op == 4:
            return self.sectors
        if op == 5:
            return 512
        if op in (1, 2, 3, 6):
            return 0
