"""100 Hz PPG processing adapted from hr/spo2/anc C reference.

Lightweight time-domain quality gates replace ESP-DSP FFT SQI. Not calibrated
medical measurements; A=110, B=25 must be fitted on the assembled sensor.
"""
import math


class Vitals:
    def __init__(self):
        self.reset()

    def reset(self):
        self.dc = [0.0, 0.0]
        self.lp = [0.0, 0.0]
        self.weights = [[0.0] * 16 for _ in range(2)]
        self.ref = [0.0] * 16
        self.red, self.ir, self.peaks = [], [], []
        self.index = 0
        self.last_peak = None
        self.rri = None
        self.above = False
        self.bpm = self.spo2 = None
        self.quality = 'warming'
        self.clean = 0

    def update(self, red, ir, motion=None):
        self.index += 1
        if min(red, ir) < 10000 or max(red, ir) >= 260000:
            self.reset()
            self.quality = 'contact/saturation'
            return
        if self.dc[1] and (abs(ir-self.dc[1])/self.dc[1] > 0.25 or
                           abs(red-self.dc[0])/self.dc[0] > 0.25):
            self.reset()
            self.quality = 'step artifact'
            return
        if motion is not None and abs(motion) > 0.15:
            self.reset()
            self.quality = 'motion artifact'
            return
        self.clean += 1
        self.ref.insert(0, motion if motion is not None else 0.0)
        self.ref.pop()
        energy = 1e-6 + sum(x*x for x in self.ref)
        signals = []
        for channel, raw in enumerate((red, ir)):
            if not self.dc[channel]:
                self.dc[channel] = float(raw)
            # DC removal (~0.5 Hz) and low pass (~4 Hz), Fs=100 Hz.
            self.dc[channel] += 0.03 * (raw-self.dc[channel])
            ac = raw-self.dc[channel]
            if motion is not None:
                w = self.weights[channel]
                ac -= sum(w[i]*self.ref[i] for i in range(16))
                for i in range(16):
                    w[i] += 0.001 * ac * self.ref[i] / energy
            self.lp[channel] += 0.2 * (ac-self.lp[channel])
            signals.append(self.lp[channel])
        self.red.append(signals[0])
        self.ir.append(signals[1])
        if len(self.ir) > 200:
            self.red.pop(0)
            self.ir.pop(0)
        if len(self.ir) < 200:
            return
        low, high = min(self.ir[-100:]), max(self.ir[-100:])
        amplitude = high-low
        above = signals[1] > low+0.6*amplitude
        if above and not self.above and amplitude/self.dc[1] > 0.001:
            if self.last_peak is not None:
                interval = self.index-self.last_peak
                if 25 <= interval <= 200 and (self.rri is None or 0.6 <= interval/self.rri <= 1.7):
                    self.rri = interval
                    self.peaks.append(interval)
                    self.peaks = self.peaks[-8:]
                    self.bpm = round(6000/(sum(self.peaks)/len(self.peaks)))
            self.last_peak = self.index
        self.above = above
        if self.last_peak is None or self.index-self.last_peak > 300:
            self.bpm = None
            self.rri = None
            self.peaks = []
        if self.index % 25:
            return
        ir_ac = max(self.ir)-min(self.ir)
        red_ac = max(self.red)-min(self.red)
        pi = ir_ac/self.dc[1]
        correlation = sum(a*b for a,b in zip(self.red,self.ir))
        norm = math.sqrt(sum(a*a for a in self.red)*sum(b*b for b in self.ir))
        good = pi > 0.001 and norm > 0 and correlation/norm > 0.7
        ratio = (red_ac/self.dc[0])/pi if pi else 0
        estimate = 110-25*ratio
        self.spo2 = round(estimate) if good and 0.4 <= ratio <= 3 and 70 <= estimate <= 100 else None
        if not good:
            self.bpm = None
        self.quality = 'optical OK; motion unverified' if good and motion is None else ('OK' if good else 'low SQI')
