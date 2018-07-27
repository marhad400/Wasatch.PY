import logging
import re

log = logging.getLogger(__name__)

class BalanceAcquisition(object):

    INTEGRATION            = 0
    LASER                  = 1
    LASER_THEN_INTEGRATION = 2

    def __init__(self, mode=INTEGRATION, intensity=45000, threshold=2500, pixel=None, device=None):
        self.mode      = mode
        self.intensity = intensity
        self.threshold = threshold
        self.pixel     = pixel
        self.device    = device

        if not isinstance(self.mode, int):
            self.mode = self.parse_mode(self.mode)

    def balance(self):
        if self.mode == self.INTEGRATION:
            return self.balance_pass(self.adjust_integration)
        elif self.mode == self.LASER:
            return self.balance_pass(self.adjust_laser)
        else:
            # due to the way we're halving overshoots, the laser+integration
            # combination likely adds little value over integration alone
            if not self.balance_pass(self.adjust_laser):
                return False
            return self.balance_pass(self.adjust_integration)

    def balance_pass(self, adjust_func):
        if self.device is None:
            log.error("missing device")
            return

        self.overshoot_count = 0
        while True:
            reading = self.device.acquire_data()
            if reading is None or reading.spectrum is None:
                log.error("failed to get spectrum")
                return False
            spectrum = reading.spectrum

            state = self.device.settings.state

            peak = spectrum[self.pixel] if self.pixel is not None else max(spectrum)
            delta = self.intensity - peak

            log.debug("integration_time_ms %d, laser_power %d, peak %d, delta %d", 
                state.integration_time_ms, state.laser_power, peak, delta)

            # exit case
            if abs(delta) <= self.threshold:
                log.debug("balanced")
                return True

            # adjust
            if not adjust_func(peak):
                return False

    def adjust_integration(self, peak):
        state = self.device.settings.state
        if peak > self.intensity:
            n = int(state.integration_time_ms / 2)
            self.overshoot_count += 1
            if self.overshoot_count > 5:
                log.error("too many overshoots")
                return False
        else:
            n = int(1.0 * state.integration_time_ms * self.intensity / peak)

        n = max(10, min(5000, n))

        log.debug("new integ = %d", n)
        self.device.hardware.set_integration_time_ms(n)
        return True

    def adjust_laser(self, peak):
        state = self.device.settings.state
        if peak > self.intensity:
            n = int(state.laser_power / 2)
            self.overshoot_count += 1
            if self.overshoot_count > 5:
                log.error("too many overshoots")
                return False
        else:
            n = int(1.0 * state.laser_power * self.intensity / peak)

        n = max(1, min(100, n))

        log.debug("new power = %d", n)
        self.device.hardware.set_laser_power_perc(n)
        return True

    def parse_mode(self, s):
        s = s.strip().lower()
        if re.match("integ", s):
            return self.INTEGRATION
        elif re.match("laser.*integ", s):
            return self.LASER_THEN_INTEGRATION
        elif re.match("laser", s):
            return self.LASER
        else:
            raise Exception("invalid BalanceAcquisition mode: " + s)

