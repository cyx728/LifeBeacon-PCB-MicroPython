"""C sqi.c equivalent metrics; bounded storage, radix-2 FFT at 1 Hz.

Fixes C reference reading 256 samples from a 200-sample ring: use 200
chronological samples with Hann window and zero pad to 256.
"""
import math


def spectral_snr(samples):
    size = 256
    count = len(samples)
    mean = sum(samples)/count
    real = [0.0]*size
    imag = [0.0]*size
    for i in range(count):
        real[i] = (samples[i]-mean)*(0.5-0.5*math.cos(2*math.pi*i/(count-1)))
    j = 0
    for i in range(1, size):
        bit = size >> 1
        while j & bit:
            j ^= bit
            bit >>= 1
        j ^= bit
        if i < j:
            real[i], real[j] = real[j], real[i]
    width = 2
    while width <= size:
        angle = -2*math.pi/width
        step_r, step_i = math.cos(angle), math.sin(angle)
        for base in range(0, size, width):
            wr, wi = 1.0, 0.0
            for k in range(width//2):
                a, b = base+k, base+k+width//2
                tr, ti = wr*real[b]-wi*imag[b], wr*imag[b]+wi*real[b]
                real[b], imag[b] = real[a]-tr, imag[a]-ti
                real[a], imag[a] = real[a]+tr, imag[a]+ti
                wr, wi = wr*step_r-wi*step_i, wr*step_i+wi*step_r
        width *= 2
    power = [real[i]**2+imag[i]**2 for i in range(size//2)]
    # Match the C integer-bin conventions, Fs=100 Hz.
    low, high, total_high = int(0.5*size/100), int(4*size/100), int(5*size/100)
    peak = max(range(low, high+1), key=lambda i: power[i])
    total = sum(power[low:total_high])
    return min(1.0, sum(power[max(low,peak-1):min(total_high,peak+2)])/total) if total > 1e-6 else 0.0


def correlation(a, b):
    ma, mb = sum(a)/len(a), sum(b)/len(b)
    num = sum((x-ma)*(y-mb) for x,y in zip(a,b))
    den = math.sqrt(sum((x-ma)**2 for x in a)*sum((y-mb)**2 for y in b))
    return max(-1.0, min(1.0, num/den)) if den > 1e-6 else 0.0


def score(value, threshold, higher=True):
    return min(1.0, value/threshold) if higher else (1.0 if value <= threshold else threshold/value)


class Quality:
    def __init__(self):
        self.template = None
        self.metrics = {}
        self.valid = False

    def evaluate(self, ir, motion, intervals, age):
        dc = sum(ir)/len(ir)
        pi = 100*(max(ir)-min(ir))/dc if dc > 1 else 0.0
        snr = spectral_snr(ir)
        if len(intervals) >= 3:
            mean = sum(intervals)/len(intervals)
            cv = math.sqrt(sum((x-mean)**2 for x in intervals)/len(intervals))/mean
        else:
            cv = 0.0 if intervals else 1.0
        current = ir[-50:]
        template = 1.0 if self.template is None else (correlation(self.template,current)+1)/2
        self.template = current
        age_score = 1.0 if age <= 10 else max(0.0, 1-(age-10)/50)
        # Unknown motion is not a fabricated zero acceleration measurement.
        acc = abs(motion) if motion is not None else None
        acc_score = score(acc,0.035,False) if acc is not None else 0.0
        value = 100*(0.30*score(pi,0.5)+0.20*score(cv,0.15,False)+
                     0.15*score(snr,0.3)+0.10*score(template,0.7)+
                     0.10*acc_score+0.15*age_score)
        self.valid = value >= 60 and pi >= 0.5 and (acc is None or acc <= 0.035) and age < 60
        self.metrics = dict(sqi=value, pi=pi, snr=snr, rri_cv=cv,
                            template=template, age=age, age_score=age_score,
                            motion=acc, motion_verified=acc is not None, valid=self.valid)
        return self.valid
