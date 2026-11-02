"""Offline synthetic checks; no serial or RF access."""
import ast
import binascii
import math
from pathlib import Path
import struct
from vitals import Vitals
from ppg_quality import Quality, spectral_snr

root = Path(__file__).resolve().parent
wave = [50000+1000*math.sin(2*math.pi*1.2*n/100) for n in range(200)]
assert spectral_snr(wave) > 0.8
q = Quality()
assert q.evaluate(wave, 0.0, [83,84,83,84], 0)
assert not q.evaluate(wave, 0.1, [83,84,83,84], 0)
assert not q.evaluate(wave, 0.0, [83,84,83,84], 60)
assert not q.evaluate([50000]*200, 0.0, [], 0)
v = Vitals()
for n in range(1500):
    pulse = math.sin(2*math.pi*1.2*n/100)
    v.update(50000+600*pulse, 50000+1000*pulse)
assert v.bpm is not None and abs(v.bpm-72) <= 2, v.bpm
assert v.spo2 is not None and abs(v.spo2-95) <= 2, v.spo2
v.update(0, 0)
assert v.bpm is None and v.spo2 is None
v.update(50000, 50000, 0.5)
assert v.bpm is None and v.spo2 is None
for n in range(400):
    v.update(50000, 50000)
assert v.bpm is None and v.spo2 is None

tree = ast.parse((root/'main.py').read_text(encoding='utf-8-sig'))
scope = {'struct': struct, 'PERSON_NAME': 'DEMO', 'device_id': lambda: 0x12345678}
nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and
         node.name in ('crc16_ccitt', 'build_beacon', 'build_call_packet')]
exec(compile(ast.Module(body=nodes, type_ignores=[]), 'protocol', 'exec'), scope)
response = scope['build_beacon'](1, 50, (72,95,0), 3, 1, message_type=2)
assert response[3] == 2 and len(response) == 25
assert int.from_bytes(response[-2:], 'little') == binascii.crc_hqx(response[:-2], 0xffff)
call = scope['build_call_packet'](0x12345678)
assert len(call) == 10 and call[7] == 0
assert int.from_bytes(call[-2:], 'little') == binascii.crc_hqx(call[:-2], 0xffff)
for path in root.glob('*.py'):
    ast.parse(path.read_text(encoding='utf-8-sig'))
print('PASS: synthetic 72 bpm/95% SpO2, artifact/contact rejection, type-2 CRC and short CQ')
