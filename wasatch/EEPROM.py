import logging
import struct
import array
import math
import copy
import json
import re

log = logging.getLogger(__name__)

##
# This class encapsulates the post-read parsing, pre-write marshalling, and current
# state of the 8-page EEPROM used to store non-volatile configuration data in Wasatch
# Photonics spectrometers.  It is essential to keep this class synchronized (in naming,
# datatype / datasize and sequence) with the ENG-0034 customer-facing documentation.
#
# This class is normally accessed as an attribute of SpectrometerSettings.
#
# @see ENG-0034
class EEPROM(object):
    
    USE_REV_4 = True

    def __init__(self):
        self.format = 0

        self.model                       = None
        self.serial_number               = None
        self.baud_rate                   = 0
        self.has_cooling                 = False
        self.has_battery                 = False
        self.has_laser                   = False
        self.excitation_nm               = 0.0
        self.excitation_nm_float         = 0.0
        self.slit_size_um                = 0
        self.startup_integration_time_ms = 10
        self.startup_temp_degC           = 15
        self.startup_triggering_scheme   = 0
        self.detector_gain               = 1.9
        self.detector_offset             = 0
        self.detector_gain_odd           = 1.9
        self.detector_offset_odd         = 0
                                         
        self.wavelength_coeffs           = []
        self.degC_to_dac_coeffs          = []
        self.adc_to_degC_coeffs          = []
        self.max_temp_degC               = 20 # interesting
        self.min_temp_degC               = 10 #    defaults
        self.tec_r298                    = 0
        self.tec_beta                    = 0
        self.calibration_date            = None
        self.calibrated_by               = None
                                         
        self.detector                    = None
        self.active_pixels_horizontal    = 1024
        self.active_pixels_vertical      = 0
        self.min_integration_time_ms     = 10
        self.max_integration_time_ms     = 60000
        self.actual_horizontal           = 0
        self.actual_vertical             = 0     # not a real EEPROM field, though it should be
        self.roi_horizontal_start        = 0
        self.roi_horizontal_end          = 0
        self.roi_vertical_region_1_start = 0
        self.roi_vertical_region_1_end   = 0
        self.roi_vertical_region_2_start = 0
        self.roi_vertical_region_2_end   = 0
        self.roi_vertical_region_3_start = 0
        self.roi_vertical_region_3_end   = 0
        self.linearity_coeffs            = []

        self.max_laser_power_mW          = 0.0
        self.min_laser_power_mW          = 0.0
        self.laser_power_coeffs          = []

        self.user_data                   = None
        self.user_text                   = None

        self.bad_pixels                  = [] # should be set, not list
        self.product_configuration       = None
                                         
        self.format = 0
        self.buffers = []
        self.write_buffers = []

        self.editable = [ "excitation_nm",
                          "excitation_nm_float",
                          "detector_gain",
                          "detector_offset",
                          "detector_gain_odd",
                          "detector_offset_odd",
                          "calibrated_by",
                          "calibration_date", 
                          "user_text",
                          "wavelength_coeffs",
                          "linearity_coeffs",
                          "max_laser_power_mW",
                          "min_laser_power_mW",
                          "laser_power_coeffs",
                          "bad_pixels",
                          "roi_horizontal_end",             
                          "roi_horizontal_start",           
                          "roi_vertical_region_1_end",      
                          "roi_vertical_region_1_start",    
                          "roi_vertical_region_2_end",      
                          "roi_vertical_region_2_start",    
                          "roi_vertical_region_3_end",      
                          "roi_vertical_region_3_start" ]

    ## whether the given field is normally editable by users via ENLIGHTEN
    #
    # @return False otherwise (don't trust in None's truthiness, as you can't 
    #         pass None to Qt's setEnabled)
    def is_editable(self, name):
        s = name.lower()
        for field in self.editable:
            if s == field.lower():
                return True
        return False

    ## @return tuple of (start, end) pixel coordinates (end is last pixel, not last+1),
    #          or None if no valid horizontal ROI
    def get_horizontal_roi(self):
        start = self.roi_horizontal_start
        end   = self.roi_horizontal_end
        pixels = self.active_pixels_horizontal

        if start >= 0 and start < pixels and end >= start and end <= pixels:
            return (start, max(start, min(end, pixels-1)))

    ## 
    # passed a temporary copy of another EEPROM object, copy-over any
    # "editable" fields to this one
    def update_editable(self, new_eeprom):
        for field in self.editable:
            log.debug("Updating %s", field)
            old = getattr(self, field)
            new = copy.deepcopy(getattr(new_eeprom, field))

            if old == new:
                log.debug("  no change")
            else:
                setattr(self, field, new)
                log.debug("  old: %s", old)
                log.debug("  new: %s", getattr(self, field))
    ## 
    # given a set of the 6 buffers read from a spectrometer via USB,
    # parse those into the approrpriate fields and datatypes
    def parse(self, buffers):
        if len(buffers) < 6:
            log.error("EEPROM.parse expects at least 6 buffers")
            return

        # store these locally so self.unpack() can access them
        self.buffers = buffers

        # unpack all the fields we know about
        self.read_eeprom()

    ## render the attributes of this object as a JSON string
    def json(self, allow_nan=True):
        tmp_buf  = self.buffers
        tmp_data = self.user_data

        self.buffers   = str(self.buffers)
        self.user_data = str(self.user_data)

        # this does take an allow_nan argument, but it throws an exception on NaN, 
        # rather than replacing with null :-(
        # https://stackoverflow.com/questions/6601812/sending-nan-in-json
        s = json.dumps(self.__dict__, indent=2, sort_keys=True)
        if not allow_nan:
            s = re.sub(r"\bNaN\b", "null", s)

        self.buffers   = tmp_buf
        self.user_data = tmp_data

        return s

    ## log this object
    def dump(self):
        log.debug("EEPROM settings:")
        log.debug("  Model:            %s", self.model)
        log.debug("  Serial Number:    %s", self.serial_number)
        log.debug("  Baud Rate:        %d", self.baud_rate)
        log.debug("  Has Cooling:      %s", self.has_cooling)
        log.debug("  Has Battery:      %s", self.has_battery)
        log.debug("  Has Laser:        %s", self.has_laser)
        log.debug("  Excitation:       %s nm", self.excitation_nm)
        log.debug("  Excitation (f):   %.2f nm", self.excitation_nm_float)
        log.debug("  Slit size:        %s um", self.slit_size_um)
        log.debug("  Start Integ Time: %d ms", self.startup_integration_time_ms)
        log.debug("  Start Temp:       %.2f degC", self.startup_temp_degC)
        log.debug("  Start Triggering: 0x%04x", self.startup_triggering_scheme)
        log.debug("  Det Gain:         %f", self.detector_gain)
        log.debug("  Det Offset:       %d", self.detector_offset)
        log.debug("  Det Gain Odd:     %f", self.detector_gain_odd)
        log.debug("  Det Offset Odd:   %d", self.detector_offset_odd)
        log.debug("")
        log.debug("  Wavecal coeffs:   %s", self.wavelength_coeffs)
        log.debug("  degCToDAC coeffs: %s", self.degC_to_dac_coeffs)
        log.debug("  adcToDegC coeffs: %s", self.adc_to_degC_coeffs)
        log.debug("  Det temp max:     %s degC", self.max_temp_degC)
        log.debug("  Det temp min:     %s degC", self.min_temp_degC)
        log.debug("  TEC R298:         %s", self.tec_r298)
        log.debug("  TEC beta:         %s", self.tec_beta)
        log.debug("  Calibration Date: %s", self.calibration_date)
        log.debug("  Calibration By:   %s", self.calibrated_by)
        log.debug("")
        log.debug("  Detector name:    %s", self.detector)
        log.debug("  Active horiz:     %d", self.active_pixels_horizontal)
        log.debug("  Active vertical:  %d", self.active_pixels_vertical)
        log.debug("  Min integration:  %d ms", self.min_integration_time_ms)
        log.debug("  Max integration:  %d ms", self.max_integration_time_ms)
        log.debug("  Actual Horiz:     %d", self.actual_horizontal)
        log.debug("  ROI Horiz Start:  %d", self.roi_horizontal_start)
        log.debug("  ROI Horiz End:    %d", self.roi_horizontal_end)
        log.debug("  ROI Vert Reg 1:   (%d, %d)", self.roi_vertical_region_1_start, self.roi_vertical_region_1_end)
        log.debug("  ROI Vert Reg 2:   (%d, %d)", self.roi_vertical_region_2_start, self.roi_vertical_region_2_end)
        log.debug("  ROI Vert Reg 3:   (%d, %d)", self.roi_vertical_region_3_start, self.roi_vertical_region_3_end)
        log.debug("  Linearity Coeffs: %s", self.linearity_coeffs)
        log.debug("")
        log.debug("  Laser coeffs:     %s", self.laser_power_coeffs)
        log.debug("  Max Laser Power:  %s mW", self.max_laser_power_mW)
        log.debug("  Min Laser Power:  %s mW", self.min_laser_power_mW)
        log.debug("")
        log.debug("  User Text:        %s", self.user_text)
        log.debug("")
        log.debug("  Bad Pixels:       %s", self.bad_pixels)
        log.debug("  Product Config:   %s", self.product_configuration)

    # ##########################################################################
    #                                                                          #
    #                             Private Methods                              #
    #                                                                          #
    # ##########################################################################

    ## 
    # Assuming a set of 6+ buffers have been passed in via parse(), actually
    # unpack (deserialize / unmarshall) the binary data into the approriate
    # fields and datatypes.
    # 
    # @see https://docs.python.org/2/library/struct.html#format-characters
    # (capitals are unsigned)
    def read_eeprom(self):
        self.format = self.unpack((0, 63,  1), "B", "format")
        log.debug("parsing EEPROM format %d", self.format)

        # ######################################################################
        # Page 0
        # ######################################################################

        self.model                           = self.unpack((0,  0, 16), "s", "model")
        self.serial_number                   = self.unpack((0, 16, 16), "s", "serial")
        self.baud_rate                       = self.unpack((0, 32,  4), "I", "baud")
        self.has_cooling                     = self.unpack((0, 36,  1), "?", "cooling")
        self.has_battery                     = self.unpack((0, 37,  1), "?", "battery")
        self.has_laser                       = self.unpack((0, 38,  1), "?", "laser")
        self.excitation_nm                   = self.unpack((0, 39,  2), "H" if self.format >= 3 else "h", "excitation(ushort)")
        self.slit_size_um                    = self.unpack((0, 41,  2), "H" if self.format >= 4 else "h", "slit")

        # NOTE: the new InGaAs detector gain/offset won't be usable from 
        #       EEPROM until we start bumping production spectrometers to
        #       EEPROM Page 0 Revision 3!
        if self.format >= 3:
            self.startup_integration_time_ms = self.unpack((0, 43,  2), "H", "start_integ")
            self.startup_temp_degC           = self.unpack((0, 45,  2), "h", "start_temp")
            self.startup_triggering_scheme   = self.unpack((0, 47,  1), "B", "start_trigger")
            self.detector_gain               = self.unpack((0, 48,  4), "f", "gain") # "even pixels" for InGaAs
            self.detector_offset             = self.unpack((0, 52,  2), "h", "offset") # "even pixels" for InGaAs
            self.detector_gain_odd           = self.unpack((0, 54,  4), "f", "gain_odd") # InGaAs-only
            self.detector_offset_odd         = self.unpack((0, 58,  2), "h", "offset_odd") # InGaAs-only

        # ######################################################################
        # Page 1
        # ######################################################################

        self.wavelength_coeffs = []
        self.wavelength_coeffs         .append(self.unpack((1,  0,  4), "f", "wavecal_coeff_0"))
        self.wavelength_coeffs         .append(self.unpack((1,  4,  4), "f"))
        self.wavelength_coeffs         .append(self.unpack((1,  8,  4), "f"))
        self.wavelength_coeffs         .append(self.unpack((1, 12,  4), "f"))
        self.degC_to_dac_coeffs = []
        self.degC_to_dac_coeffs        .append(self.unpack((1, 16,  4), "f", "degCtoDAC_coeff_0"))
        self.degC_to_dac_coeffs        .append(self.unpack((1, 20,  4), "f"))
        self.degC_to_dac_coeffs        .append(self.unpack((1, 24,  4), "f"))
        self.max_temp_degC                   = self.unpack((1, 28,  2), "h", "max_temp")
        self.min_temp_degC                   = self.unpack((1, 30,  2), "h", "min_temp")
        self.adc_to_degC_coeffs = []
        self.adc_to_degC_coeffs        .append(self.unpack((1, 32,  4), "f", "adcToDegC_coeff_0"))
        self.adc_to_degC_coeffs        .append(self.unpack((1, 36,  4), "f"))
        self.adc_to_degC_coeffs        .append(self.unpack((1, 40,  4), "f"))
        self.tec_r298                        = self.unpack((1, 44,  2), "h", "r298")
        self.tec_beta                        = self.unpack((1, 46,  2), "h", "beta")
        self.calibration_date                = self.unpack((1, 48, 12), "s", "date")
        self.calibrated_by                   = self.unpack((1, 60,  3), "s", "tech")
                                    
        # ######################################################################
        # Page 2                    
        # ######################################################################

        self.detector                        = self.unpack((2,  0, 16), "s", "detector")
        self.active_pixels_horizontal        = self.unpack((2, 16,  2), "H", "pixels")
        self.active_pixels_vertical          = self.unpack((2, 19,  2), "H" if self.format >= 4 else "h")
        if self.format < 5:
            self.min_integration_time_ms         = self.unpack((2, 21,  2), "H", "min_integ(ushort)")
            self.max_integration_time_ms         = self.unpack((2, 23,  2), "H", "max_integ(ushort)") 
        self.actual_horizontal               = self.unpack((2, 25,  2), "H" if self.format >= 4 else "h", "actual_horiz")
        self.actual_vertical                 = self.active_pixels_vertical  # approximate for now
        self.roi_horizontal_start            = self.unpack((2, 27,  2), "H" if self.format >= 4 else "h")
        self.roi_horizontal_end              = self.unpack((2, 29,  2), "H" if self.format >= 4 else "h")
        self.roi_vertical_region_1_start     = self.unpack((2, 31,  2), "H" if self.format >= 4 else "h")
        self.roi_vertical_region_1_end       = self.unpack((2, 33,  2), "H" if self.format >= 4 else "h")
        self.roi_vertical_region_2_start     = self.unpack((2, 35,  2), "H" if self.format >= 4 else "h")
        self.roi_vertical_region_2_end       = self.unpack((2, 37,  2), "H" if self.format >= 4 else "h")
        self.roi_vertical_region_3_start     = self.unpack((2, 39,  2), "H" if self.format >= 4 else "h")
        self.roi_vertical_region_3_end       = self.unpack((2, 41,  2), "H" if self.format >= 4 else "h")
        self.linearity_coeffs = []
        self.linearity_coeffs          .append(self.unpack((2, 43,  4), "f", "linearity_coeff_0")) # overloading for secondary ADC
        self.linearity_coeffs          .append(self.unpack((2, 47,  4), "f"))
        self.linearity_coeffs          .append(self.unpack((2, 51,  4), "f"))
        self.linearity_coeffs          .append(self.unpack((2, 55,  4), "f"))
        self.linearity_coeffs          .append(self.unpack((2, 59,  4), "f"))

        # ######################################################################
        # Page 3
        # ######################################################################
        
        self.laser_power_coeffs = []
        self.laser_power_coeffs        .append(self.unpack((3, 12,  4), "f", "laser_power_coeff_0"))
        self.laser_power_coeffs        .append(self.unpack((3, 16,  4), "f"))
        self.laser_power_coeffs        .append(self.unpack((3, 20,  4), "f"))
        self.laser_power_coeffs        .append(self.unpack((3, 24,  4), "f"))
        self.max_laser_power_mW              = self.unpack((3, 28,  4), "f", "max_laser_mW")
        self.min_laser_power_mW              = self.unpack((3, 32,  4), "f", "min_laser_mW")

        self.excitation_nm_float             = self.unpack((3, 36,  4), "f", "excitation(float)")
        if self.format < 4:
            self.excitation_nm_float = self.excitation_nm

        if self.format >= 5:
            self.min_integration_time_ms     = self.unpack((3, 40,  4), "I", "min_integ(uint)")
            self.max_integration_time_ms     = self.unpack((3, 44,  4), "I", "max_integ(uint)") 

        # ######################################################################
        # Page 4
        # ######################################################################

        self.user_data = self.buffers[4][:63]
        self.user_text = self.printable(self.user_data)

        # ######################################################################
        # Page 5
        # ######################################################################

        bad = set()
        for count in range(15):
            pixel = self.unpack((5, count * 2, 2), "h")
            if pixel != -1:
                bad.add(pixel)
        self.bad_pixels = list(bad)
        self.bad_pixels.sort()

        self.product_configuration           = self.unpack((5,  30, 16), "s", "product_config")

    ## make a printable ASCII string out of possibly-binary data
    def printable(self, buf):
        s = ""
        for c in buf:
            if 31 < c < 127:
                s += chr(c)
            elif c == 0:
                break
            else:
                s += '.'
        return s

    ## 
    # Unpack a single field at a given buffer offset of the given datatype.
    #
    # @param address    a tuple of the form (buf, offset, len)
    # @param data_type  see https://docs.python.org/2/library/struct.html#format-characters
    def unpack(self, address, data_type, label=None):
        page       = address[0]
        start_byte = address[1]
        length     = address[2]
        end_byte   = start_byte + length

        buf = self.buffers[page]
        if buf is None or end_byte > len(buf):
            log.error("error unpacking EEPROM page %d, offset %d, len %d as %s: buf is %s (label %s)", 
                page, start_byte, length, data_type, buf, label, exc_info=1)
            return

        if data_type == "s":
            # This stops at the first NULL, so is not appropriate for binary data (user_data).
            # OTOH, it doesn't currently enforce "printable" characters either (nor support Unicode).
            unpack_result = ""
            for c in buf[start_byte:end_byte]:
                if c == 0:
                    break
                unpack_result += chr(c)
        else:
            unpack_result = 0
            try:
                unpack_result = struct.unpack(data_type, buf[start_byte:end_byte])[0]
            except:
                log.error("error unpacking EEPROM page %d, offset %d, len %d as %s", page, start_byte, length, data_type, exc_info=1)

        if label is None:
            log.debug("Unpacked [%s]: %s", data_type, unpack_result)
        else:
            log.debug("Unpacked [%s]: %s (%s)", data_type, unpack_result, label)
        return unpack_result

    ## 
    # Marshall or serialize a single field at a given buffer offset of the given datatype.
    #
    # @param address    a tuple of the form (buf, offset, len)
    # @param data_type  see https://docs.python.org/2/library/struct.html#format-characters
    # @param value      value to serialize
    def pack(self, address, data_type, value):
        page       = address[0]
        start_byte = address[1]
        length     = address[2]
        end_byte   = start_byte + length

        # don't try to write negatives to unsigned types
        if data_type in ["H", "I"] and value < 0:
            log.error("rounding negative to zero when writing to unsigned field (address %s, data_type %s, value %s)", address, data_type, value)
            value = 0

        buf = self.write_buffers[page]
        if buf is None or end_byte > 63: # byte [63] for revision
            raise Exception("error packing EEPROM page %d, offset %2d, len %2d as %s: buf is %s" % (
                page, start_byte, length, data_type, buf))

        if data_type == "s":
            for i in range(min(length, len(value))):
                if i < len(value):
                    buf[start_byte + i] = ord(value[i])
                else:
                    buf[start_byte + i] = 0
        else:
            struct.pack_into(data_type, buf, start_byte, value)

        log.debug("Packed (%d, %2d, %2d) '%s' value %s -> %s", 
            page, start_byte, length, data_type, value, buf[start_byte:end_byte])

    ##
    # Call this to populate an internal array of "write buffers" which may be written back
    # to spectrometers.
    def generate_write_buffers(self):
        # stub-out 6 blank buffers
        self.write_buffers = []
        for page in range(6):
            self.write_buffers.append(array.array('B', [0] * 64))

        # ideally we should apply LATEST page revision numbers per ENG-0034, but
        # for now maintain compatibility with StrokerConsole/ModelConfigurationFormat.cs
        revs = { 0: 1,
                 1: 1,
                 2: 2, 
                 3: 255,
                 4: 1, 
                 5: 1 }

        # copy the above revision numbers into the last byte of each buffer
        for page in list(revs.keys()):
            self.write_buffers[page][63] = revs[page]

        if EEPROM.USE_REV_4:
            self.write_buffers[0][63] = 5

        # Page 0
        self.pack((0,  0, 16), "s", self.model                       )
        self.pack((0, 16, 16), "s", self.serial_number               )
        self.pack((0, 32,  4), "I", self.baud_rate                   )
        self.pack((0, 36,  1), "?", self.has_cooling                 )
        self.pack((0, 37,  1), "?", self.has_battery                 )
        self.pack((0, 38,  1), "?", self.has_laser                   )
        self.pack((0, 39,  2), "H", int(round(self.excitation_nm, 0)))
        self.pack((0, 41,  2), "H", self.slit_size_um                )
        self.pack((0, 43,  2), "H", self.startup_integration_time_ms )
        self.pack((0, 45,  2), "h", self.startup_temp_degC           )
        self.pack((0, 47,  1), "B", self.startup_triggering_scheme   )
        self.pack((0, 48,  4), "f", self.detector_gain               )
        self.pack((0, 52,  2), "h", self.detector_offset             )
        self.pack((0, 54,  4), "f", self.detector_gain_odd           )
        self.pack((0, 58,  2), "h", self.detector_offset_odd         )

        # Page 1
        self.pack((1,  0,  4), "f", self.wavelength_coeffs[0]  )
        self.pack((1,  4,  4), "f", self.wavelength_coeffs[1]  )
        self.pack((1,  8,  4), "f", self.wavelength_coeffs[2]  )
        self.pack((1, 12,  4), "f", self.wavelength_coeffs[3]  )
        self.pack((1, 16,  4), "f", self.degC_to_dac_coeffs[0] )
        self.pack((1, 20,  4), "f", self.degC_to_dac_coeffs[1] )
        self.pack((1, 24,  4), "f", self.degC_to_dac_coeffs[2] )
        self.pack((1, 32,  4), "f", self.adc_to_degC_coeffs[0] )
        self.pack((1, 36,  4), "f", self.adc_to_degC_coeffs[1] )
        self.pack((1, 40,  4), "f", self.adc_to_degC_coeffs[2] )
        self.pack((1, 28,  2), "h", self.max_temp_degC         )
        self.pack((1, 30,  2), "h", self.min_temp_degC         )
        self.pack((1, 44,  2), "h", self.tec_r298              )
        self.pack((1, 46,  2), "h", self.tec_beta              )
        self.pack((1, 48, 12), "s", self.calibration_date      )
        self.pack((1, 60,  3), "s", self.calibrated_by         )
                                    
        # Page 2                    
        self.pack((2,  0, 16), "s", self.detector                    )
        self.pack((2, 16,  2), "H", self.active_pixels_horizontal    )
        #        skip 18
        self.pack((2, 19,  2), "H", self.active_pixels_vertical      )
        self.pack((2, 21,  2), "H", max(0xffff, self.min_integration_time_ms)) # by default, keep populating these with 
        self.pack((2, 23,  2), "H", max(0xffff, self.max_integration_time_ms)) # old values
        self.pack((2, 25,  2), "H", self.actual_horizontal           )
        self.pack((2, 27,  2), "H", self.roi_horizontal_start        )
        self.pack((2, 29,  2), "H", self.roi_horizontal_end          )
        self.pack((2, 31,  2), "H", self.roi_vertical_region_1_start )
        self.pack((2, 33,  2), "H", self.roi_vertical_region_1_end   )
        self.pack((2, 35,  2), "H", self.roi_vertical_region_2_start )
        self.pack((2, 37,  2), "H", self.roi_vertical_region_2_end   )
        self.pack((2, 39,  2), "H", self.roi_vertical_region_3_start )
        self.pack((2, 41,  2), "H", self.roi_vertical_region_3_end   )
        self.pack((2, 43,  4), "f", self.linearity_coeffs[0]         )
        self.pack((2, 47,  4), "f", self.linearity_coeffs[1]         )
        self.pack((2, 51,  4), "f", self.linearity_coeffs[2]         )
        self.pack((2, 55,  4), "f", self.linearity_coeffs[3]         )
        self.pack((2, 59,  4), "f", self.linearity_coeffs[4]         )

        # Page 3
        self.pack((3, 12,  4), "f", self.laser_power_coeffs[0])
        self.pack((3, 16,  4), "f", self.laser_power_coeffs[1])
        self.pack((3, 20,  4), "f", self.laser_power_coeffs[2])
        self.pack((3, 24,  4), "f", self.laser_power_coeffs[3])
        self.pack((3, 28,  4), "f", self.max_laser_power_mW)
        self.pack((3, 32,  4), "f", self.min_laser_power_mW)
        self.pack((3, 36,  4), "f", self.excitation_nm_float)
        self.pack((3, 40,  4), "I", self.min_integration_time_ms     )
        self.pack((3, 44,  4), "I", self.max_integration_time_ms     )

        # Page 4
        self.pack((4,  0, 63), "s", self.user_text)

        # Page 5
        bad_pixel_set = set()
        for i in self.bad_pixels:
            if i >= 0:
                bad_pixel_set.add(i)
        bad_pixels = list(bad_pixel_set)
        bad_pixels.sort()
        for i in range(15):
            if i < len(bad_pixels):
                value = bad_pixels[i]
            else:
                value = -1
            self.pack((5, i * 2, 2), "h", value)

        self.pack((5, 30, 16), "s", self.product_configuration)

    ## can be used as a sanity-check for any set of coefficients
    def coeffs_look_valid(self, coeffs, count=None):

        if coeffs is None:
            return False

        if count is not None and len(coeffs) != count:
            return False

        # check for [0, 1, 0...] default pattern
        all_default = True
        for i in range(len(coeffs)):
            c = coeffs[i]
            if math.isnan(c):
                return False # always invalid
            if i == 1:
                if c != 1.0:
                    all_default = False
                    break
            else:
                if c != 0.0:
                    all_default = False
                    break
        if all_default:
            return false

        # check for constants (all negative, all zero, etc)
        for const in [-1.0, 0.0]:
            all_const = True
            for c in coeffs:
                if c != const:
                    all_const = False
                    break
            if all_const:
                return False

        return True

    # ##########################################################################
    # Laser Power convenience accessors
    # ##########################################################################

    def has_laser_power_calibration(self):
        if self.max_laser_power_mW <= 0:
            return False
        return self.coeffs_look_valid(self.laser_power_coeffs, count=4)

    ## convert the given laser output power from milliwatts to percentage
    #  using the configured calibration
    def laser_power_mW_to_percent(self, mW):
        if not self.has_laser_power_calibration():
            return 0

        perc = self.laser_power_coeffs[0] \
             + self.laser_power_coeffs[1] * mW \
             + self.laser_power_coeffs[2] * mW * mW \
             + self.laser_power_coeffs[3] * mW * mW * mW

        return perc
