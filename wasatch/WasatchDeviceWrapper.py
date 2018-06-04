""" Wrap WasatchDevice in a non-blocking interface run in a separate
    process. Use a summary queue to pass meta information about the
    device for multiprocessing-safe spectrometer settings and spectral 
    acquisition on Windows. 

    From ENLIGHTEN's standpoint (one Wasatch.PY user), here's what's going on:

    1. MainProcess creates a Controller.bus_timer which on timeout (tick) calls
        Controller.update_connections()
    2. Controller.update_connections() calls Controller.connect_new()
    3. connect_new(): if we're not already connected to a spectrometer, yet
        bus.device_1 is not "disconnected" (implying something was found on the
        bus), then connect_new() instantiates a WasatchDeviceWrapper and then
        calls connect() on it
    4. WasatchDeviceWrapper.connect() forks a child process running the
        continuous_poll() method of the same WasatchDeviceWrapper instance,
        then waits on SpectrometerSettings to be returned via a pipe (queue).

       [-- at this point, the same WasatchDeviceWrapper instance is being 
           accessed by two processes...be careful! --]

    5. continuous_poll() instantiantes a WasatchDevice.  This object will only
        ever be referenced within the subprocess.
    6. continuous_poll() calls WasatchDevice.connect() (exits on failure)
        6.a WasatchDevice instantiates a Sim, FID or SP based on UID
        6.b if FID, WasatchDevice.connect() loads the EEPROM
    7. continuous_poll() populates (exactly once) SpectrometerSettings object,
       then feeds it back to MainProcess
"""

import sys
import time
import Queue
import common
import logging
import multiprocessing

from . import applog
from . import utils

from SpectrometerSettings import SpectrometerSettings
from ControlObject        import ControlObject
from WasatchDevice        import WasatchDevice
from Reading              import Reading

log = logging.getLogger(__name__)

class WasatchDeviceWrapper(object):

    ACQUISITION_MODE_KEEP_ALL      = 0 # don't drop frames
    ACQUISITION_MODE_LATEST        = 1 # only grab most-recent frame (allow dropping)
    ACQUISITION_MODE_KEEP_COMPLETE = 2 # generally grab most-recent frame (allow dropping),
                                       # except don't drop FULLY AVERAGED frames (summation
                                       # contributors can be skipped).  If the most-recent
                                       # frame IS a partial summation contributor, then send it on.

    DEVICE_CONTROL_COMMANDS = {
        # eeprom
        "adc_to_degC_coeffs":              { "datatype": "float[]" },
        "degC_to_dac_coeffs":              { "datatype": "float[]" },
        "wavelength_coeffs":               { "datatype": "float[]" },

        # state
        "area_scan_enable":                { "datatype": "bool" },
        "bad_pixel_mode":                  { "datatype": "int" },
        "detector_tec_enable":             { "datatype": "bool" },
        "detector_tec_setpoint_degC":      { "datatype": "int" },

        # FPGA registers
        "ccd_gain":                        { "datatype": "float" },
        "ccd_offset":                      { "datatype": "int" },

        "enable_secondary_adc":            { "datatype": "bool" },
        "high_gain_mode_enable":           { "datatype": "bool" },
        "integration_time_ms":             { "datatype": "int" },
        "invert_x_axis":                   { "datatype": "bool" },
        "laser_enable":                    { "datatype": "bool" },
        "laser_power_perc":                { "datatype": "int" },
        "laser_power_ramp_increments":     { "datatype": "int" },
        "laser_power_ramping_enabled":     { "datatype": "bool" },
        "laser_temperature_setpoint_raw":  { "datatype": "int" },
        "log_level":                       { "datatype": "string" },
        "max_usb_interval_ms":             { "datatype": "int" },
        "min_usb_interval_ms":             { "datatype": "int" },
        "scans_to_average":                { "datatype": "int" },
        "trigger_source":                  { "datatype": "int" },

        # other
        "reset_fpga":                      { "datatype": "bool" },
    }

    ############################################################################
    #                                                                          #
    #                                MainProcess                               #
    #                                                                          #
    ############################################################################

    # Instantiated by Controller.connect_new(), if-and-only-if a WasatchBus
    # reports a device UID which has not already connected to the GUI.  The UID
    # is "unique and relevant" to the bus which reported it, but neither the
    # bus class nor instance is passed in to this object.  If the UID looks like
    # a "VID:PID" string, then it is probably live hardware (or maybe a SimulationBus
    # virtual spectrometer).  If the UID looks like a /path/to/dir, then assume
    # it is a FileSpectrometer.
    def __init__(self, uid, bus_order, log_queue, log_level):
        self.uid       = uid
        self.bus_order = bus_order
        self.log_queue = log_queue
        self.log_level = log_level

        # TODO: see if the managed queues would be more robust
        #
        # self.manager = multiprocessing.Manager()
        # self.command_queue               = self.manager.Queue()
        # self.response_queue              = self.manager.Queue()
        # self.spectrometer_settings_queue = self.manager.Queue()

        self.command_queue    = multiprocessing.Queue()
        self.response_queue   = multiprocessing.Queue()
        self.spectrometer_settings_queue = multiprocessing.Queue()

        self.poller_wait  = 0.05    # MZ: update from hardware device at 20Hz
        self.closing      = False   # Don't permit new acquires during close
        self.poller       = None    # a handle to the subprocess

        # this will contain a populated SpectrometerSettings object from the 
        # WasatchDevice, for relay to the instantiating Controller
        self.settings     = None    

    # called by Controller.connect_new() immediately after instantiation
    def connect(self):
        """ Create a low level device object with the specified
            identifier, kick off the subprocess to attempt to read from it. """

        if self.poller != None:
            log.critical("WasatchDeviceWrapper.connect: already polling, cannot connect")
            return False

        # Fork a child process running the continuous_poll() method on THIS 
        # object instance.  Each process will end up with a copy of THIS Wrapper
        # instance, but they won't be "the same instance" (as they're in different
        # processes).  
        #
        # The two instances are joined by the 3 queues:
        #
        #   spectrometer_settings_queue: 
        #       Lets the subprocess (WasatchDevice) send a single, one-time copy
        #       of a populated SpectrometerSettings object back through the 
        #       Wrapper to the calling Controller.  This is the primary way the 
        #       Controller knows what kind of spectrometer it has connected to, 
        #       what hardware features and EEPROM settings are applied etc.  
        #
        #       Thereafter both WasatchDevice and Controller will maintain
        #       their own copies of SpectrometerSettings, and they are not 
        #       automatically synchronized (is there a clever way to do this?).
        #       They may well drift out-of-sync regarding state, although the 
        #       command_queue helps keep them somewhat in sync.  
        #
        #   command_queue:
        #       The Controller will send ControlObject instances (basically
        #       (name, value) pairs) through the Wrapper to the WasatchDevice
        #       to set attributes or commands on the WasatchDevice spectrometer.
        #       These may be volatile hardware settings (laser power, integration 
        #       time), meta-commands to the WasatchDevice class (scan averaging),
        #       or EEPROM updates.
        #
        #   response_queue:
        #       The WasatchDevice will stream a continuous series of Reading
        #       instances back through the Wrapper to the Controller.  These
        #       each contain a newly read spectrum, as well as metadata about
        #       the spectrometer at the time the spectrum was taken (integration
        #       time, laser power), plus additional readings from the spectrometer
        #       (detector and laser temperature, secondary ADC).  
        #
        args = (self.uid, 
                self.bus_order,
                self.log_queue,
                self.command_queue, 
                self.response_queue,
                self.spectrometer_settings_queue,
                log.getEffectiveLevel())
        log.debug("forking continuous_poll")
        self.poller = multiprocessing.Process(target=self.continuous_poll, args=args)
        log.debug("starting continuous_poll")
        self.poller.start()
        log.debug("after starting continuous_poll")
        time.sleep(0.1)
        log.debug("after starting continuous_poll + 0.1")

        # read the post-initialization SpectrometerSettings object off of the queue
        try:
            log.debug("blocking on spectrometer_settings_queue (waiting on forked continuous_poll)")
            # note: testing indicates it may take more than 2.5sec for the forked
            # continuous_poll to actually start moving.  5sec timeout may be on the short side?
            self.settings = self.spectrometer_settings_queue.get(timeout=8)
            log.info("connect: received SpectrometerSettings response")
            if self.settings is None:
                raise Exception("MainProcess WasatchDeviceWrapper received poison-pill from forked continuous_poll")
            self.settings.dump()

        except Exception as exc:
            # Apparently something failed in initialization of the subprocess, and it
            # never succeeded in sending a SpectrometerSettings object. Do our best to
            # kill the subprocess (they can be hard to kill), then report upstream
            # that we were unable to connect (Controller will allow this Wrapper object 
            # to exit from scope).

            # MZ: should this bit be merged with disconnect()?

            log.warn("connect: spectrometer_settings_queue caught exception", exc_info=1)
            self.settings = None
            self.closing = True

            log.warn("connect: sending poison pill to poller")
            self.command_queue.put(None, 2)

            log.warn("connect: waiting .5 sec")
            time.sleep(.5)

            # log.warn("connect: terminating poller")
            # self.poller.terminate()

            log.warn("releasing poller")
            self.poller = None

            return False

        # After we return True, the Controller will then take a handle to our received
        # settings object, and will keep a reference to this Wrapper object for sending
        # commands and reading spectra.
        log.debug("connect: succeeded")
        return True

    def disconnect(self):
        # send poison pill to the subprocess
        self.closing = True
        self.command_queue.put(None, 2)

        try:
            self.poller.join(timeout=2)
        except NameError as exc:
            log.warn("disconnect: Poller previously disconnected", exc_info=1)
        except Exception as exc:
            log.critical("disconnect: Cannot join poller", exc_info=1)

        log.debug("Post poller join")

        try:
            self.poller.terminate()
            log.debug("disconnect: poller terminated")
        except Exception as exc:
            log.critical("disconnect: Cannot terminate poller", exc_info=1)

        time.sleep(0.1)
        log.debug("WasatchDeviceWrapper.disconnect: done")

        return True

    def acquire_data(self, mode=None):
        """ Don't use if queue.empty() for flow control on python 2.7 on
            windows, as it will hang. Use the catch of the queue empty exception as
            shown below instead.
            
            It is the upstream interface's job to decide how to process the
            potentially voluminous amount of data returned from the device.
            get_last by default will make sure the queue is cleared, then
            return the most recent reading from the device. """

        if self.closing:
            log.debug("WasatchDeviceWrapper.acquire_data: closing")
            return None

        # ENLIGHTEN "default" - if we're doing scan averaging, take either
        #
        # 1. the NEWEST fully-averaged spectrum (purge the queue), or
        # 2. if no fully-averaged spectra are in the queue, then the NEWEST
        #    incomplete (pre-averaged) spectrum (purging the queue).
        # 
        # If we're not doing scan averaging, then take the NEWEST spectrum
        # and purge the queue.
        #
        if mode is None or mode == self.ACQUISITION_MODE_KEEP_COMPLETE:
            return self.get_final_item(keep_averaged=True)

        if mode == self.ACQUISITION_MODE_LATEST:
            return self.get_final_item()

        # presumably mode == self.ACQUISITION_MODE_KEEP_ALL:

        # Get the oldest entry off of the queue. This expects the Controller to be
        # able to process them upstream as fast as possible, because otherwise
        # the queue will grow (we're not currently limiting its size) and the
        # process will eventually crash with memory issues.

        # Note that these two calls to response_queue aren't synchronized
        reading = None
        qsize = self.response_queue.qsize()
        try:
            reading = self.response_queue.get_nowait()
            log.debug("acquire_data: read Reading %d (qsize %d)", reading.session_count, qsize)
        except Queue.Empty:
            pass

        return reading

    def get_final_item(self, keep_averaged=False):
        """ Read from the response queue until empty (or we find an averaged item) """
        reading = None
        dequeue_count = 0
        last_averaged = None
        while True:
            try:
                reading = self.response_queue.get_nowait()
                if reading:
                    log.debug("get_final_item: read Reading %d", reading.session_count)
            except Queue.Empty:
                break

            if reading is None:
                break

            dequeue_count += 1

            # was this the final spectrum in an averaged sequence?
            if keep_averaged and reading.averaged:
                last_averaged = reading

        # if we're doing averaging, take the latest of those
        if last_averaged:
            reading = last_averaged

        if reading is not None and dequeue_count > 1:
            log.debug("discarded %d spectra", dequeue_count - 1)

        return reading

    # called by MainProcess.Controller
    def change_setting(self, setting, value):
        """ Add the specified setting and value to the local control queue. """
        log.debug("WasatchDeviceWrapper.change_setting: %s => %s", setting, value)
        control_object = ControlObject(setting, value)
        try:
            self.command_queue.put(control_object)
        except Exception as exc:
            log.critical("WasatchDeviceWrapper.change_setting: Problem enqueuing %s", setting, exc_info=1)

    ############################################################################
    #                                                                          #
    #                                 Subprocess                               #
    #                                                                          #
    ############################################################################

    def continuous_poll(self, 
                        uid, 
                        bus_order, 
                        log_queue, 
                        command_queue, 
                        response_queue, 
                        spectrometer_settings_queue,
                        log_level):

        """ Continuously process with the simulated device. First setup
            the log queue handler. While waiting forever for the None poison
            pill on the command queue, continuously read from the device and
            post the results on the response queue. 
            
            MZ: This is essentially the main() loop in a forked process (not 
                thread).  Hopefully we can scale this to one per spectrometer.  
                All communications with the parent process are routed through
                one of the three queues (cmd inputs, response outputs, and
                a one-shot SpectrometerSettings).
        """

        # We have just forked into a new process, so the first thing we do is
        # configure logging for this process.
        applog.process_log_configure(log_queue, log_level)

        log.info("continuous_poll: start (uid %s, bus_order %d)", uid, bus_order)

        # The second thing we do is actually instantiate a WasatchDevice.  Note
        # that WasatchDevice objects (and by implication, FeatureIdentificationDevice,
        # StrokerProtocolDevice etc) are only instantiated inside the subprocess.
        #
        # Another way to say that is, we always go the full distance of forking
        # the subprocess even before we try to instantiate / connect to a device,
        # so if for some reason this WasatchBus entry was a misfire (bad PID, whatever),
        # we've gone to the effort of creating a process and several queues just to
        # find out.
        #
        # Finally, note that the only "hints" we are passing into WasatchDriver 
        # in terms of what type of device it should be instantiating are the UID
        # (a string with no conventions enforced, though it's traditionally USB 
        # "VID:PID" in hex, e.g. "24aa:4000") and bus_order (an integer).  These
        # two parameters are all that WasatchDevice has to decide whether to 
        # instantiate a StrokerProtocolDevice, FeatureIdentificationDevice or
        # something else.  
        #
        # If we want to move into fancy "virtual", "simulated" or "remote" 
        # spectrometers via this interface, we may want to use a more flexible 
        # format for specifying such via UID ("local_usb://vid/pid", etc).
        #
        # Regardless, if anything goes wrong here, we may need to do more to
        # cleanup these processes, queues etc.
        try:
            wasatch_device = WasatchDevice(uid, bus_order)
        except:
            log.critical("continuous_poll: exception instantiating WasatchDevice", exc_info=1)
            return spectrometer_settings_queue.put(None, timeout=2)

        ok = False
        try:
            ok = wasatch_device.connect()
        except:
            log.critical("continuous_poll: exception connecting", exc_info=1)
            return spectrometer_settings_queue.put(None, timeout=2)

        if not ok:
            log.critical("continuous_poll: failed to connect")
            return spectrometer_settings_queue.put(None, timeout=2)

        log.debug("continuous_poll: connected to a spectrometer")

        # send the SpectrometerSettings back to the GUI process
        log.debug("continuous_poll: returning SpectrometerSettings to GUI process")
        spectrometer_settings_queue.put(wasatch_device.settings, timeout=3)

        # Read forever until the None poison pill is received
        log.debug("continuous_poll: entering loop")
        while True:
            poison_pill = False
            queue_empty = False

            # only keep the MOST RECENT of any given command (but retain order otherwise)
            dedupped = self.dedupe(command_queue)

            # apply dedupped commands
            if dedupped:
                for record in dedupped:
                    if record is None:
                        poison_pill = True
                    else:
                        log.debug("continuous_poll: Processing command queue: %s", record.setting)
                        wasatch_device.change_setting(record.setting, record.value)
            else:
                log.debug("continuous_poll: Command queue empty")

            if poison_pill:
                log.debug("continuous_poll: Exit command queue (poison pill received)")
                break

            try:
                log.debug("continuous_poll: acquiring data")
                reading = wasatch_device.acquire_data()
            except Exception as exc:
                log.critical("continuous_poll: Exception", exc_info=1)
                break

            if reading is None:
                # FileSpectrometer does this right now...hardware "can't" really, 
                # because we use blocking calls, although we could probably add
                # timeouts to break those blocks.
                log.debug("continuous_poll: no Reading to be had")
            else:
                if reading.spectrum is None:
                    log.debug("continuous_poll: sending Reading %d back to GUI process (no spectrum)", reading.session_count)
                else:
                    log.debug("continuous_poll: sending Reading %d back to GUI process (%s)", reading.session_count, reading.spectrum[0:5])
                response_queue.put(reading, timeout=1)

                if reading.failure is not None:
                    log.critical("continuous_poll: Hardware level ERROR")
                    break

            # only poll hardware at 20Hz
            time.sleep(self.poller_wait)

        # send poison-pill upstream to Controller, then quit
        response_queue.put(None, timeout=1)

        log.info("continuous_poll: done")
        sys.exit()

    def dedupe(self, q):
        keep = [] 
        indices = {} 
        while True:
            try:
                control_object = q.get_nowait()

                # treat None elements (poison pills) same as everything else
                if control_object is None:
                    setting = None
                    value = None
                else:
                    setting = control_object.setting
                    value = control_object.value

                # remove previous setting if duplicate
                if setting in indices:
                    index = indices[setting]
                    del keep[index]
                    del indices[setting]

                # append the setting to the de-dupped list and track index
                keep.append(control_object)
                indices[setting] = len(keep) - 1

            except Queue.Empty as exc:
                break

        return keep

    def get_settings(self):
        return sorted(self.DEVICE_CONTROL_COMMANDS.keys())

    def get_setting_type(self, setting):
        if not setting in self.DEVICE_CONTROL_COMMANDS:
            return None

        return self.DEVICE_CONTROL_COMMANDS[setting]["datatype"]

    def is_setting(self, setting):
        return setting in self.DEVICE_CONTROL_COMMANDS

    def convert_type(self, setting, value):
        if not setting in self.DEVICE_CONTROL_COMMANDS:
            log.error("invalid setting: %s", setting)
            return None

        dt = self.DEVICE_CONTROL_COMMANDS[setting]["datatype"]
        if dt == "bool":
            return "true" in value.lower()
        elif dt == "int":
            return int(value)
        elif dt == "float":
            return float(value)
        elif dt == "string":
            return str(value)
        elif dt == "float[]":
            values = []
            for tok in value.split(','):
                values.append(float(tok))
            return values
            
