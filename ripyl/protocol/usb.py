#!/usr/bin/python
# -*- coding: utf-8 -*-

'''Ripyl protocol decode library
   USB protocol decoder
   
   This Supports all of USB 2.0 including Low, Full, and High speed;
   Link Power Management extended tokens; and USB 1.x mixed Low and
   Full speed transmissions.
'''

# Copyright © 2013 Kevin Thibedeau

# This file is part of Ripyl.

# Ripyl is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as
# published by the Free Software Foundation, either version 3 of
# the License, or (at your option) any later version.

# Ripyl is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Lesser General Public License for more details.

# You should have received a copy of the GNU Lesser General Public
# License along with Ripyl. If not, see <http://www.gnu.org/licenses/>.

from __future__ import print_function, division

import itertools

from ripyl.decode import *
from ripyl.streaming import *
from ripyl.util.enum import Enum
from ripyl.util.bitops import *
from ripyl.sigproc import remove_excess_edges


class USBSpeed(Enum):
    '''Enumeration for the USB bus speeds'''
    LowSpeed = 0
    FullSpeed = 1
    HighSpeed = 2

USBClockPeriod = {
    USBSpeed.LowSpeed: 1.0 / 1.5e6,
    USBSpeed.FullSpeed: 1.0 / 12.0e6,
    USBSpeed.HighSpeed: 1.0 / 480.0e6
}
    
class USBPID(Enum):
    '''Enumeration for the packet PIDs'''
    # The comments indicate the USBPacket() object that should be used with each PID
    
    # Token PIDs
    TokenOut   = 0b0001  # USBTokenPacket()
    TokenIn    = 0b1001  # USBTokenPacket()
    SOF        = 0b0101  # USBSOFPacket()
    TokenSetup = 0b1101  # USBTokenPacket()
    
    # Data PIDs
    Data0 = 0b0011  # USBDataPacket() ...
    Data1 = 0b1011
    Data2 = 0b0111
    MData = 0b1111
    
    # Handshake PIDs
    ACK   = 0b0010  # USBHandshakePacket() ...
    NAK   = 0b1010
    STALL = 0b1110
    NYET  = 0b0110
    
    # Special PIDs
    PRE   = 0b1100 # USBHandshakePacket()  (Low and Full speed only)
    ERR   = 0b1100 # USBHandshakePacket()  (Reused PREamble PID High speed only)
    SPLIT = 0b1000 # USBSplitPacket()
    PING  = 0b0100 # USBTokenPacket()
    EXT   = 0b0000 # USBEXTPacket()  (extended token format from Link Power Management ECN)

class USBPacketKind(Enum):
    '''Enumeration for packet kind (lower two bits of PID)'''
    Token     = 0b01
    Data      = 0b11
    Handshake = 0b10
    Special   = 0b00

def _get_packet_kind(pid):
    '''Extract the packet kind from the PID'''
    return pid & 0x03


class USBState(Enum):
    '''Enumeration for logical bus states'''
    SE0 = 0
    J   = 1
    K   = 2
    SE1 = 3 # error condition

    
class USBStreamStatus(Enum):
    '''Enumeration for USBStreamPacket and USBStreamError status codes'''
    ShortPacketError = StreamStatus.Error + 1
    MissingEOPError  = StreamStatus.Error + 2
    BitStuffingError = StreamStatus.Error + 3
    CRCError         = StreamStatus.Error + 4

    
class USBStreamPacket(StreamSegment):
    '''Encapsulates a USBPacket object (see below) into a StreamSegment'''
    def __init__(self, bounds, sop_end, packet, crc=None, status=StreamStatus.Ok):
        '''
        bounds
            2-tuple (start_time, end_time) for the packet
            
        sop_end
            The time for the end of the SOP portion of the packet. Used to measure
            bit positions for the packet fields.
            
        packet
            USBPacket object

        crc
            Optional CRC extracted from a decoded packet. Not used for encoding
            with usb_synth().
            
        status
            status code for the packet
        '''
        StreamSegment.__init__(self, bounds, data=None, status=status)
        self.kind = 'USB packet'
        
        self.sop_end = sop_end
        self.packet = packet # USBPacket object
        self.crc = crc

    @classmethod
    def status_text(cls, status):
        if status >= USBStreamStatus.ShortPacketError and \
            status <= USBStreamStatus.CRCError:
            
            return USBStreamStatus(status)
        else:
            return StreamSegment.status_text(status)


    def field_offsets(self):
        '''Get a dict of packet field bit offsets
        Returns a dict keyed by the field name and a pair (start, end) for each value.
          Start and end are the inclusive times for the start and end of a field.
        '''

        bit_fields = self.packet.field_offsets(with_stuffing = True)
        
        fields = {}
        clock_period = USBClockPeriod[self.packet.speed]
        for field, (start, end) in bit_fields.items():
            fields[field] = (self.sop_end + start * clock_period, self.sop_end + (end + 1) * clock_period)

        if self.packet.pid == USBPID.EXT:
            # EXT packets need a special adjustment for the fields in the second half
            part2_delta = self.sop_end2 - self.sop_end
            start, end = fields['SubPID']
            fields['SubPID'] = (start + part2_delta, end + part2_delta)
            
            start, end = fields['Variable']
            fields['Variable'] = (start + part2_delta, end + part2_delta)
            
            start, end = fields['CRC5_2']
            fields['CRC5_2'] = (start + part2_delta, end + part2_delta)
            
        return fields
        
    def __repr__(self):
        status_text = USBStreamPacket.status_text(self.status)
        return 'USBStreamPacket({}, {})'.format(self.packet, status_text)


class USBStreamError(StreamSegment):
    '''Contains partially decoded packet data after an error has been found
    in the data stream'''
    def __init__(self, bounds, error_data, pid=-1, status=StreamStatus.Error):
        '''
        bounds
            2-tuple (start_time, end_time) for the packet
            
        error_data
            An array of bits (potentially unstuffed) for the packet

        pid
            The PID for the packet if it was successfully extracted. -1 if the
            PID was invalid or unavailable.
        
        status
            status code for the packet
        '''    
        StreamSegment.__init__(self, bounds, data=error_data, status=status)
        self.kind = 'USB error'
        
        self.pid = pid
        
    def __repr__(self):
        status_text = StreamSegment.status_text(self.status)
        return 'USBStreamError({}, {}, {})'.format(self.data, hex(self.pid), status_text)
    

        
class USBPacket(object):
    '''Base class for USB packet objects
    
    This class should not be instanced directly. Use the various subclasses instead.

    These objects have methods meant to be used by the usb_synth() routine. When
    these objects are embedded in a USBStreamPacket object they are used for attribute
    access only.
    '''
    def __init__(self, pid, speed=USBSpeed.FullSpeed, delay=0.0 ):
        self.speed = speed
        self.pid = pid & 0x0f
        self.delay = delay
        self.hs_eop_bits = 8 # High-speed EOP is normally 8-bits. SOF Packet overrides this to 40
        self.hs_sync_dropped_bits = 0 # USB 2.0 7.1.10: Up to 20 bits may be dropped from High-speed sync
        self.idle_cycles = 3 # number of idle cycles at start of a packet
        self.swap_jk = False # Set True for low-speed packets transmitted after a PREamble
        
    def get_bits(self):
        '''Generate the raw data bits of a packet in LSB-first order'''
        raise NotImplementedError('USBPacket must be sub-classed')
        
    def _init_bits(self):
        '''Generate the common sync and PID bits used by all packet types'''
        # sync and PID generation
        bits = []
        
        # generate sync
        if self.speed == USBSpeed.LowSpeed or self.speed == USBSpeed.FullSpeed:
            bits += [0, 0, 0, 0, 0, 0, 0, 1]
        else: # High-speed: 15 KJ pairs followed by 2 K's
            drop_bits = self.hs_sync_dropped_bits
            if drop_bits > 20:
                drop_bits = 20
                
            bits += [0] * (30 - drop_bits) + [0, 1]
        
        # generate PID
        pid_rev = int('{:04b}'.format(self.pid)[::-1], base=2) # reverse the bits
        pid_enc = pid_rev << 4 | (pid_rev ^ 0x0f)
        bits += split_bits(pid_enc, 8)
        
        return bits

    def _bit_stuff(self, bits):
        '''Perform USB bit-stuffing'''
        sbits = []
        ones = 0
        for b in bits:
            sbits.append(b)
            
            if b == 1:
                ones += 1
            else:
                ones = 0
                
            if ones == 6:
                # stuff a 0 in the bit stream
                sbits.append(0)
                ones = 0
        return sbits
        
    def _bit_stuff_offsets(self, bits):
        '''Get list of stuffed bit offsets in the unstuffed bit vector'''
        stuff_offsets = []
        ones = 0
        for i, b in enumerate(bits):
            if b == 1:
                ones += 1
            else:
                ones = 0
                
            if ones == 6:
                stuff_offsets.append(i)
                
        return stuff_offsets
        
    def _get_NRZI(self):
        '''Apply bit stuffing and convert bits to J/K states'''
        period = USBClockPeriod[self.speed]
        t = 0.0

        J = USBState.J
        K = USBState.K
        SE0 = USBState.SE0
        
        # initial state idle
        if self.speed == USBSpeed.LowSpeed or self.speed == USBSpeed.FullSpeed:
            states = [(t, J)]
        else: # HighSpeed
            states = [(t, SE0)]
            
        t += period * self.idle_cycles
        
        # bit stuff
        stuffed_bits = self._bit_stuff(self.get_bits())
        #stuffed_bits = self.get_bits()

        # High-speed EOP
        if self.speed == USBSpeed.HighSpeed:
            # EOP is signalled with intentional bit-stuff error(s)
            stuffed_bits += [0] + [1] * (self.hs_eop_bits-1)
            #print('## EOP bits:', self.hs_eop_bits, bin(self.pid))
            
        
        # convert bits to NRZI J/K states
        prev_state = J
        for b in stuffed_bits:
            if b == 0: # toggle
                ns = J if prev_state == K else K
            else: # b == 1, keep state
                ns = prev_state
                
            prev_state = ns
                
            states.append((t, ns))
            t += period
        
        # generate EOP
        if self.speed == USBSpeed.LowSpeed or self.speed == USBSpeed.FullSpeed:
            states.append((t, SE0)) # SE0 for two cycles
            t += 2 * period
        
            states.append((t, J))
        else: # HighSpeed
            states.append((t, SE0))
        

        #print('$$$$ NRZI states:', zip(*states)[1])
        return states
        
        
    def get_edges(self, cur_time = 0.0):
        '''Produce a set of edges corresponding to USB D+ and D- signals
        
        Returns a 2-tuple containing the D+ and D- edge lists
        '''
        if self.speed == USBSpeed.LowSpeed and self.swap_jk == False:
            J_DP = 0
            J_DM = 1
        else:
            J_DP = 1
            J_DM = 0

        K_DP = 1 - J_DP
        K_DM = 1 - J_DM
            
        edges_dp = []
        edges_dm = []
        
        for s in self._get_NRZI():
            t = s[0] + self.delay + cur_time
            if s[1] == USBState.J:
                dp = J_DP
                dm = J_DM
            elif s[1] == USBState.K:
                dp = K_DP
                dm = K_DM
            else: # SE0
                dp = 0
                dm = 0

            edges_dp.append((t, dp))
            edges_dm.append((t, dm))
            
        return (edges_dp, edges_dm)

    def get_diff_edges(self, cur_time = 0.0):
        '''Produce a set of edges corresponding to USB differential (D+ - D-) signal
        
        Returns a list of differential edges
        '''
        if self.speed == USBSpeed.LowSpeed and self.swap_jk == False:
            DIFF_J = -1
            DIFF_K = 1
        else:
            DIFF_J = 1
            DIFF_K = -1
            
        edges_diff = []
        
        for s in self._get_NRZI():
            t = s[0] + self.delay + cur_time
            if s[1] == USBState.J:
                diff = DIFF_J
            elif s[1] == USBState.K:
                diff = DIFF_K
            else: # SE0
                diff = 0

            edges_diff.append((t, diff))
            
        return edges_diff

    def _adjust_stuffing(self, fields):
        import bisect
        stuff_pos = self._bit_stuff_offsets(self.get_bits())
        
        if len(stuff_pos) == 0: # No bit stuffing present
            return fields
        
        cum_offsets = list(xrange(len(stuff_pos)))
        
        adj_fields = {}
        for field, (start, end) in fields.items():
            # Find the bit offsets that apply to the field range
            i = bisect.bisect_left(stuff_pos, start)
            if i < len(cum_offsets):
                start += cum_offsets[i]
            else:
                start += cum_offsets[-1] + 1
            
            i = bisect.bisect_left(stuff_pos, end)
            if i < len(cum_offsets):
                end += cum_offsets[i]
            else:
                end += cum_offsets[-1] + 1

            adj_fields[field] = (start, end)
            
        return adj_fields
        
    def field_offsets(self, with_stuffing=None):
        '''Get a dict of packet field bit offsets
        Returns a dict keyed by the field name and a pair (start, end) for each value.
          Start and end are the inclusive bit offsets for the start and end of a field
          relative to the end of SOP.
        '''
        # No bit stuffing will happen in the PID
        return {'PID': (0, 7)}
        
    def __eq__(self, other):
        raise NotImplementedError('Must use from subclassed objects')
        
    def __ne__(self, other):
        return not self == other



class USBTokenPacket(USBPacket):
    '''Token packet'''
    def __init__(self, pid, addr, endp, speed=USBSpeed.FullSpeed, delay=0.0):
        USBPacket.__init__(self, pid, speed, delay)
        self.addr = addr
        self.endp = endp
        
    def get_bits(self):
        '''Generate token packet bits'''
        # Token packet format:
        #  sync, PID, Addr, Endp, CRC5
        
        start_bits = self._init_bits() # sync and PID
            
        # generate address
        check_bits = []
        check_bits += reversed(split_bits(self.addr, 7))
        
        # generate Endp
        check_bits += reversed(split_bits(self.endp, 4))
        
        # generate CRC5
        crc_bits = usb_crc5(check_bits)
        #print('$$$$ Token CRC5', crc_bits)
        
        return start_bits + check_bits + crc_bits
        
    def field_offsets(self, with_stuffing=False):
        fields = {'PID': (0, 7), 'Addr': (8, 14), 'Endp': (15, 18), 'CRC5': (19, 23)}
        if with_stuffing:
            fields = self._adjust_stuffing(fields)
        return fields
        
    def __repr__(self):
        return 'USBTokenPacket({}, {}, {}, {}, {})'.format(hex(self.pid), hex(self.addr), \
            hex(self.endp), self.speed, self.delay)

    def __eq__(self, other):
        if self.pid != other.pid or self.speed != other.speed:
            match = False
        else:
            match = True
            
            try:
                if self.addr != other.addr: match = False
                if self.endp != other.endp: match = False
            except AttributeError:
                match = False
                
        return match
        
class USBDataPacket(USBPacket):
    '''Data packet'''
    def __init__(self, pid, data, speed=USBSpeed.FullSpeed, delay=0.0):
        USBPacket.__init__(self, pid, speed, delay)
        self.data = data
    
    def get_bits(self):
        '''Generate data packet bits'''
        # Data packet format:
        #  sync, PID, Data, CRC16
        
        start_bits = self._init_bits() # sync and PID
        
        # calculate CRC16
        crc_bits = table_usb_crc16(self.data)
        #print('$$$$ Data CRC16', crc_bits)
        
        # add data bits LSB first
        data_bits = []
        for byte in self.data:
            data_bits += reversed(split_bits(byte, 8))
                
        return start_bits + data_bits + crc_bits
        
    def field_offsets(self, with_stuffing=False):
        data_bits = len(self.data) * 8
        fields = {'PID': (0, 7), 'Data': (8, 8 + data_bits-1), 'CRC16': (8 + data_bits, 8 + data_bits + 16 - 1)}
        if with_stuffing:
            fields = self._adjust_stuffing(fields)
        return fields
        
    def __repr__(self):
        return 'USBDataPacket({}, {}, {}, {})'.format(hex(self.pid), self.data, \
            self.speed, self.delay)
            
    def __eq__(self, other):
        if self.pid != other.pid or self.speed != other.speed:
            match = False
        else:
            match = True
            
            try:
                if len(self.data) != len(other.data):
                    match = False
                else:
                    for d, o in zip(self.data, other.data):
                        if d != o:
                            match = False
                            break
            except AttributeError:
                match = False
                
        return match

        
class USBHandshakePacket(USBPacket):
    '''Handshake packet'''
    def __init__(self, pid, speed=USBSpeed.FullSpeed, delay=0.0):
        USBPacket.__init__(self, pid, speed, delay)

    def get_bits(self):
        '''Generate handshake packet bits'''
        # Handshake packet format:
        #  sync, PID
        
        start_bits = self._init_bits() # sync and PID
        
        return start_bits
        
    def _get_NRZI(self):
        nrzi = USBPacket._get_NRZI(self)
    
        # Override default behavior for PRE PIDs so we can remove the EOP
        if (self.speed == USBSpeed.LowSpeed or self.speed == USBSpeed.FullSpeed) \
            and self.pid == USBPID.PRE:
            # Technically, PRE packets are only transmitted at Full-speed but we include
            # Low-speed to maintain full coverate across all speeds. At High-speed
            # the PRE PID becomes the ERR handshake which has a normal EOP.
            
            end_t = nrzi[-2][0]
            end_state = nrzi[-1][1]
            
            nrzi = nrzi[0:-2] + [(end_t, end_state)] # strip off EOP

        return nrzi

    def field_offsets(self, with_stuffing=False):
        # There can be no bit stuffing on just a PID
        fields = {'PID': (0, 7)}
        return fields

    def __repr__(self):
        return 'USBHandshakePacket({}, {}, {})'.format(hex(self.pid), self.speed, self.delay)        

    def __eq__(self, other):
        return self.pid == other.pid
        

class USBSOFPacket(USBPacket):
    '''Start of Frame packet'''
    def __init__(self, pid, frame_num, speed=USBSpeed.FullSpeed, delay=0.0):
        USBPacket.__init__(self, pid, speed, delay)
        self.frame_num = frame_num
        self.hs_eop_bits = 40

    def get_bits(self):
        '''Generate SOF packet bits'''
        # SOF packet format:
        #  sync, PID, Frame, CRC5
        
        start_bits = self._init_bits() # sync and PID
        
        # generate frame
        check_bits = []
        check_bits += reversed(split_bits(self.frame_num, 11))
        
        # generate CRC5
        crc_bits = usb_crc5(check_bits)
        #print('$$$$ SOF CRC5', crc_bits)
        
        return start_bits + check_bits + crc_bits


    def field_offsets(self, with_stuffing=False):
        '''Get a dict of packet field bit offsets
        Returns a dict keyed by the field name and a pair (start, end) for each value.
          Start and end are the inclusive bit offsets for the start and end of a field
          relative to the end of SOP.
        '''
        fields = {'PID': (0, 7), 'Frame': (8, 18), 'CRC5': (19, 23)}
        if with_stuffing:
            fields = self._adjust_stuffing(fields)
        return fields
        
    def __repr__(self):
        return 'USBSOFPacket({}, {}, {}, {})'.format(hex(self.pid), hex(self.frame_num), \
            self.speed, self.delay)

    def __eq__(self, other):
        if self.pid != other.pid or self.speed != other.speed:
            match = False
        else:
            match = True
            
            try:
                if self.frame_num != other.frame_num: match = False
            except AttributeError:
                match = False
                
        return match

class USBSplitPacket(USBPacket):
    '''Split packet'''
    def __init__(self, pid, addr, sc, port, s, e, et, speed=USBSpeed.HighSpeed, delay=0.0):
        USBPacket.__init__(self, pid, speed, delay)
        self.addr = addr
        self.sc = sc
        self.port = port
        self.s = s
        self.e = e  # Snused for CSPLIT tokens. Should be 0
        self.et = et
        
    def get_bits(self):
        '''Generate split packet bits'''
        # Split packet format:
        #  sync, PID, Addr, SC, Port, S, E/U, ET, CRC5
        
        start_bits = self._init_bits() # sync and PID
            
        # generate address
        check_bits = []
        
        check_bits += reversed(split_bits(self.addr, 7))
        
        # SC field
        check_bits.append(self.sc & 0x01)
        
        # Port field
        check_bits += reversed(split_bits(self.port, 7))
        
        # S field
        check_bits.append(self.s & 0x01)
        
        # E field
        # unused for CSPLIT tokens
        check_bits.append(self.e & 0x01)
        
        # ET field
        check_bits += reversed(split_bits(self.et, 2))
        

        # generate CRC5
        crc_bits = usb_crc5(check_bits)
        #print('$$$$ Token CRC5', crc_bits)
        
        return start_bits + check_bits + crc_bits

    def field_offsets(self, with_stuffing=False):
        '''Get a dict of packet field bit offsets
        Returns a dict keyed by the field name and a pair (start, end) for each value.
          Start and end are the inclusive bit offsets for the start and end of a field
          relative to the end of SOP.
        '''
        fields = {'PID': (0, 7), 'Addr': (8, 14), 'SC': (15, 15), 'Port': (16, 22), \
            'S': (23, 23), 'E': (24, 24), 'ET': (25, 26), 'CRC5': (27, 31)}
        if with_stuffing:
            fields = self._adjust_stuffing(fields)
        return fields
        
    def __repr__(self):
        return 'USBSplitPacket({}, {}, {}, {}, {}, {}, {}, {}, {})'.format(hex(self.pid), hex(self.addr), \
            self.sc, hex(self.port), self.s, self.e, hex(self.et), self.speed, self.delay)

    def __eq__(self, other):
        if self.pid != other.pid or self.speed != other.speed:
            match = False
        else:
            match = True
            
            try:
                if self.addr != other.addr: match = False
                if self.sc != other.sc: match = False
                if self.port != other.port: match = False
                if self.s != other.s: match = False
                if self.e != other.e: match = False
                if self.et != other.et: match = False
            except AttributeError:
                match = False
                
        return match
            
class USBEXTPacket(USBPacket):
    '''Extended packet'''
    def __init__(self, pid, addr, endp, sub_pid, variable, speed=USBSpeed.FullSpeed, delay=0.0):
        USBPacket.__init__(self, pid, speed, delay)
        self.addr = addr
        self.endp = endp
        
        self.sub_pid = sub_pid
        self.variable = variable

    # Instead of defining the get_bits() method we "hack" the existing packet objects to
    # synthesize an extended packet from two token packets.
    # This is necessary since we need to add an interpacket gap between the two parts
    # of the extended token which needs to be done after conversion to NRZI.

    def _get_NRZI(self):
        # Make some dummy packet objects
        
        tok_packet = USBTokenPacket(self.pid, self.addr, self.endp, speed=self.speed)
        
        # split the 11-bit variable into 7-bit and 4-bit parts so they
        # can be stuffed into another USBTokenPacket()
        ext_addr = self.variable & 0x7F
        ext_endp = (self.variable >> 7) & 0x0F
        ext_packet = USBTokenPacket(self.sub_pid, ext_addr, ext_endp, speed=self.speed)
        
        tok_nrzi = tok_packet._get_NRZI()
        ext_nrzi = ext_packet._get_NRZI()
        
        # construct the interpacket gap
        if self.speed == USBSpeed.LowSpeed or self.speed == USBSpeed.FullSpeed:
            idle_state = USBState.J
            gap_bit_times = 4 # Minimum is 2 bit times
        else: # HighSpeed
            idle_state = USBState.SE0
            gap_bit_times = 40 # Minimum is 32 bit times

        # Add one cycle before switch to idle
        ig_start_time = tok_nrzi[-1][0] + USBClockPeriod[self.speed]
        tok_nrzi.append((ig_start_time, idle_state))
        
        # Adjust bit times in ext NRZI list
        ext_start_time = tok_nrzi[-1][0] + gap_bit_times * USBClockPeriod[self.speed]
        for i in xrange(len(ext_nrzi)):
            t, s = ext_nrzi[i]
            ext_nrzi[i] = (t + ext_start_time, s)
            
        tok_nrzi += ext_nrzi
        
        return tok_nrzi

    def field_offsets(self, with_stuffing=False):
        '''Get a dict of packet field bit offsets
        Returns a dict keyed by the field name and a pair (start, end) for each value.
          Start and end are the inclusive bit offsets for the start and end of a field
          relative to the end of SOP.
        '''
        tok_packet = USBTokenPacket(self.pid, self.addr, self.endp, speed=self.speed)
        
        # split the 11-bit variable into 7-bit and 4-bit parts so they
        # can be stuffed into another USBTokenPacket()
        ext_addr = self.variable & 0x7F
        ext_endp = (self.variable >> 7) & 0x0F
        ext_packet = USBTokenPacket(self.sub_pid, ext_addr, ext_endp, speed=self.speed)
        
        tok_fields = tok_packet.field_offsets(with_stuffing)
        ext_fields = ext_packet.field_offsets(with_stuffing)

        # These bit positions are relative to the start of second part of the EXT packet
        tok_fields['SubPID'] = ext_fields['PID']
        tok_fields['Variable'] = (ext_fields['Addr'][0], ext_fields['Endp'][1])
        tok_fields['CRC5_2'] = ext_fields['CRC5']

        return tok_fields
        
    def __repr__(self):
        return 'USBEXTPacket({}, {}, {}, {}, {}, {}, {})'.format(hex(self.pid), hex(self.addr), \
            hex(self.endp), hex(self.sub_pid), hex(self.variable), self.speed, self.delay)
            
    def __eq__(self, other):
        if self.pid != other.pid or self.speed != other.speed:
            match = False
        else:
            match = True
            
            try:
                if self.addr != other.addr: match = False
                if self.endp != other.endp: match = False
                if self.sub_pid != other.sub_pid: match = False
                if self.variable != other.variable: match = False
            except AttributeError:
                match = False
                
        return match


            
def usb_decode(dp, dm, stream_type=StreamType.Samples):
    '''Decode a USB data stream
    
    This is a generator function that can be used in a pipeline of waveform
    processing operations.
    
    This function decodes USB data captured from the two single-ended D+ and D- signals.
    For differential USB decode see the function usb_diff_decode().
    
    Low speed device keep-alive EOPs are not reported in the decoded results.
    
    
    The dp and dm parameters are edge or sample streams.
    Each is a stream of 2-tuples of (time, value) pairs. The type of stream is identified
    by the stream_type parameter. Either a series of real valued samples that will be
    analyzed to find edge transitions or a set of pre-processed edge transitions
    representing the 0 and 1 logic states of the waveforms. When this is a sample
    stream, an initial block of data on the dp stream is consumed to determine the most
    likely logic levels in the signal and the bus speed.
    
    dp
        USB D+ stream
    
    dm
        USB D- stream
    
    stream_type
        A StreamType value indicating that the dp, and dm parameters represent either Samples
        or Edges
        
    Yields a series of USBStreamPacket and USBStreamError objects
    
    Raises StreamError if stream_type = Samples and the logic levels cannot
      be determined. Also if the bus speed cannot be determined.
    '''
    
    if stream_type == StreamType.Samples:
        # tee off an iterator to determine logic thresholds
        s_dp_it, thresh_it = itertools.tee(dp)
        
        logic = find_logic_levels(thresh_it)
        if logic is None:
            raise StreamError('Unable to find avg. logic levels of waveform')
        del thresh_it
        
        hyst = 0.4
        dp_it = find_edges(s_dp_it, logic, hysteresis=hyst)
        dm_it = find_edges(dm, logic, hysteresis=hyst)
        
    else: # the streams are already lists of edges
        dp_it = dp
        dm_it = dm

    # tee off an iterator to determine speed class
    dp_it, speed_check_it = itertools.tee(dp_it)

    bus_speed = _get_bus_speed(speed_check_it)
    # delete the tee'd iterators so that the internal buffer will not grow
    # as the edges_it is advanced later on
    del speed_check_it
    
    #print('### symbol rate:', bus_speed, USBClockPeriod[bus_speed])

    edge_sets = {
        'dp': dp_it,
        'dm': dm_it
    }
    
    es = MultiEdgeSequence(edge_sets, 0.0)
    
    state_seq = EdgeSequence(_convert_single_ended_states(es, bus_speed), 0.0)
    
    records = _decode_usb_state(state_seq, bus_speed)
    
    return records

    
    
def usb_diff_decode(d_diff, stream_type=StreamType.Samples):
    '''Decode a differential USB data stream
    
    This is a generator function that can be used in a pipeline of waveform
    processing operations.
    
    This function decodes USB data captured from a differential (D+)-(D-) signal.
    For single-ended USB decode see the function usb_decode().

    Low speed device keep-alive EOPs are not reported in the decoded results.
    
    
    The d_diff parameter is a stream of 2-tuples of (time, value) pairs. The type of stream
    is identified by the stream_type parameter. Either a series of real valued samples that
    will be analyzed to find edge transitions or a set of pre-processed edge transitions
    representing the -1, 0, and 1 logic states of the differential waveform. When this is a
    sample stream, an initial block of data is consumed to determine the most
    likely logic levels in the signal and the bus speed.
    
    d_diff
        USB differential D stream
    
    stream_type
        A StreamType value indicating that the dp, and dm parameters represent either Samples
        or Edges
        
    Yields a series of USBStreamPacket and USBStreamError objects
    
    Raises StreamError if stream_type = Samples and the logic levels cannot
      be determined. Also if the bus speed cannot be determined.
    '''
    
    if stream_type == StreamType.Samples:
        # tee off an iterator to determine logic thresholds
        s_diff_it, thresh_it = itertools.tee(d_diff)
        
        logic = find_logic_levels(thresh_it)
        if logic is None:
            raise AutoLevelError
        del thresh_it

        hyst = 0.1
        d_diff_it = find_differential_edges(s_diff_it, logic, hysteresis=hyst)

    else: # The stream is already a list of edges
        d_diff_it = d_diff

    # Tee off an iterator to determine speed class
    d_diff_it, speed_check_it = itertools.tee(d_diff_it)

    bus_speed = _get_bus_speed(speed_check_it, remove_se0s = True)
    # Delete the tee'd iterators so that the internal buffer will not grow
    # as the d_diff_it is advanced later on
    del speed_check_it
    
    #print('### symbol rate:', bus_speed, USBClockPeriod[bus_speed])
    
    if stream_type == StreamType.Samples:
        # We needed the bus speed before we could properly strip just
        # the anomalous SE0s
        min_se0 = USBClockPeriod[bus_speed] * 0.75
        d_diff_it = remove_short_diff_0s(d_diff_it, min_se0)

    es = EdgeSequence(d_diff_it, 0.0)
    state_seq = EdgeSequence(_convert_differential_states(es, bus_speed), 0.0)
    
    records = _decode_usb_state(state_seq, bus_speed)
    
    return records


        
    
def _get_bus_speed(speed_check_it, remove_se0s=False):
    '''Determine bus speed of USB waveforms'''

    # An unfiltered differential edge list can contain unwanted
    # SE0 states between +1 <-> -1 transitions. These will interfere
    # with the symbol rate estimation code and need to be removed.
    # The legitimate SE0s lost from EOPs and High-speed idle will not
    # affect the result.
    if remove_se0s:
        speed_check_it = (edge for edge in speed_check_it if edge[1] != 0)

    buf_edges = 50
    min_edges = 8
    symbol_rate_edges = itertools.islice(speed_check_it, buf_edges)
    
    # We need to ensure that we can pull out enough edges from the iterator slice
    # Just consume them all for a count        
    sre_list = list(symbol_rate_edges)
    if len(sre_list) < min_edges:
        raise StreamError('Unable to determine bus speed (not enough edge transitions)')
        
    #print('## sym. rate edges len:', len(sre_list))
    
    raw_symbol_rate = find_symbol_rate(iter(sre_list), spectra=2)
    
    std_bus_speeds = ((USBSpeed.LowSpeed, 1.5e6), (USBSpeed.FullSpeed, 12.0e6), \
        (USBSpeed.HighSpeed, 480.0e6))
    # find the bus speed closest to the raw rate
    bus_speed = min(std_bus_speeds, key=lambda x: abs(x[1] - raw_symbol_rate))[0]
    
    del symbol_rate_edges

    
    return bus_speed


def _decode_usb_state(state_seq, bus_speed, allow_mixed_full_low=False):
    '''This routine does the bulk of the decode work. It is called by both the
    single-ended decoder and the differential decoder once they have completed
    the preliminary work of extracting the bus states (J, K, SE0) from their
    input streams.
    '''
    
    SE0 = USBState.SE0
    J = USBState.J
    K = USBState.K
    
    ext_packet_bits = None
    preamble_active = False

    cur_bus = state_seq.cur_state()
    while not state_seq.at_end():
        prev_bus = cur_bus
        state_seq.advance_to_edge()
        cur_bus = state_seq.cur_state()
        packet_start = state_seq.cur_time
        
        # The packet speed is reduced to low-speed if the previous packet
        # was a PREamble.
        if not preamble_active:
            pkt_speed = bus_speed
        else:
            pkt_speed = USBSpeed.LowSpeed
        
        # Look ahead to next edge to determine if this edge may be the start
        # of a full speed packet. This allows us to combine low and full
        # speed packets when PREamble packets are in use.
        if bus_speed == USBSpeed.FullSpeed and pkt_speed == USBSpeed.LowSpeed:
            next_edge_step = state_seq.next_states[0] - state_seq.cur_time
            if abs(next_edge_step - USBClockPeriod[USBSpeed.FullSpeed]) < \
                USBClockPeriod[USBSpeed.FullSpeed] * 0.05:
                pkt_speed = USBSpeed.FullSpeed
                preamble_active = False

        clock_period = USBClockPeriod[pkt_speed]

        # Look for a start of packet:
        #   Low and Full speed: Transition from J to K
        #   High speed: Transition from SE0 to K
        get_packet = False
        if pkt_speed == USBSpeed.LowSpeed or pkt_speed == USBSpeed.FullSpeed:
            if prev_bus == J and cur_bus == K:
                
                # move to middle of current K state
                state_seq.advance(clock_period / 2.0)
                if state_seq.cur_state() == K: # still valid
                    sync_pattern = [J, K, J, K, J, K, K]
                    get_packet = True
                    for p in sync_pattern:
                        # move to next sync bit
                        state_seq.advance(clock_period)
                        if state_seq.cur_state() != p: # pattern mismatch
                            get_packet = False
                            break

        else: # HighSpeed SOP
            if prev_bus == SE0 and cur_bus == K:
                # move to middle of current K state
                state_seq.advance(clock_period / 2.0)
                if state_seq.cur_state() == K: # still valid
                    # A chain of High-speed hubs can drop up to 20 sync bits
                    # We're only guaranteed 12. We may be on the first K and the
                    # last one is K so check for 5 JK's first
                    sync_pattern = [J, K] * 5
                    get_packet = True
                    for p in sync_pattern:
                        # move to next sync bit
                        state_seq.advance(clock_period)
                        if state_seq.cur_state() != p: # pattern mismatch
                            get_packet = False
                            break
                            
                    if get_packet: # first part matched, now look for end of sync
                        # now look for alternating JK's or KK for end of sync
                        cur_bus = state_seq.cur_state() # should be K
                        s_count = 0
                        while True:
                            prev_bus = cur_bus
                            state_seq.advance(clock_period)
                            s_count += 1
                            cur_bus = state_seq.cur_state()
                            
                            if not (cur_bus == J or cur_bus == K): # invalid sync state
                                get_packet = False
                                break
                            
                            if prev_bus == K and cur_bus == K: # found sync end
                                break
                                
                            elif (prev_bus == J and cur_bus == J) or s_count > 20: # invalid sync
                                get_packet = False
                                break

        if not get_packet: # we didn't find a valid sync
            cur_bus = state_seq.cur_state()
            continue

            
        # We have potentially found a sync field but this could be packet data that has the same
        # pattern. A bad PID, CRC, or premature SE0 will catch this
        
        sop_end = state_seq.cur_time + clock_period / 2.0
        
        # Get the remaining states in the packet.
        # We will adjust timings to keep ourselves positioned in the center of a bit
        packet_states = []
        state_seq.advance(clock_period)  # At middle of first PID bit
        time_adjustment = 0.0
        invalid_pid = False

        while state_seq.cur_state() != SE0:
            if state_seq.at_end():
                break
                
            packet_states.append(state_seq.cur_state())
            
            time_step = clock_period
            # only perform adjustment if it's magnitude is more than 1ps
            if abs(time_adjustment) >= 1.0e12:
                time_step += time_adjustment
                
            state_seq.advance(time_step)
            
            # Make timing adjustment if there is a state transition coming up within next bit period
            next_edge_time = state_seq.next_states[0]
            next_step = next_edge_time - state_seq.cur_time
            if next_step < clock_period:
                time_adjustment = next_step - (clock_period / 2.0)
                
            if len(packet_states) == 8:
                # Decode the PID
                packet_pid_bits = _decode_NRZI(packet_states)
                
                # Validate the PID
                packet_pid_check = packet_pid_bits[4:8]
                for i, b in enumerate(packet_pid_check): # invert the check bits
                    packet_pid_check[i] = 1 - b

                if packet_pid_bits[0:4] == packet_pid_check: # valid PID
                    pid = join_bits(reversed(packet_pid_bits[0:4]))
                    if bus_speed == USBSpeed.FullSpeed and pid == USBPID.PRE and ext_packet_bits is None:
                        # The PREamble packet is special and does not
                        # have an EOP. There will be no SE0 to break on.
                        preamble_active = True
                        break
                else:
                    invalid_pid = True

        cur_bus = state_seq.cur_state()
        packet_end = state_seq.cur_time
            
        # We need at least 8 states/bits to retrieve the PID
        if len(packet_states) < 8:
            status = USBStreamStatus.ShortPacketError
            yield USBStreamError((packet_start, packet_end), packet_bits, status=status)
            continue
            
        packet_bits = _decode_NRZI(packet_states)
        
        if invalid_pid:
            status = USBStreamStatus.InvalidPIDError
            yield USBStreamError((packet_start, packet_end), packet_bits, status=status)
            continue
            
        packet_kind = _get_packet_kind(pid)
        
        # A USB 2.0 hub chain can add up to 20 random bits to the end of a HighSpeed packet.
        # We need to strip the HighSpeed EOP fom the end of the packet_bits before unstuffing
        # so that we can determine the length of a data packet later. If we wait, the unstuffing
        # will mangle the EOP and make things less dependable.
        eop_bits = 0
        if pkt_speed == USBSpeed.HighSpeed and packet_kind == USBPacketKind.Data:
            # We need to find the *start* of the EOP
            trailing_data = list(reversed(packet_bits[-28:]))
            # look for reversed EOP pattern in trailing data
            eop_pat = [1,1,1,1,1,1,1,0]
            eop_bits = 0
            for i in xrange(len(trailing_data) - 8):
                sliding_window = trailing_data[i:i+8]
                if sliding_window == eop_pat: # found EOP
                    eop_bits = i + 8
                    
            if eop_bits == 0: # no EOP found
                #print('######### ERROR NO EOP found in data packet', trailing_data)
                
                status = USBStreamStatus.MissingEOPError
                yield USBStreamError((packet_start, packet_end), packet_bits, pid=pid, status=status)
                continue
        
        
        # Unstuff the bits
        # Technically the final 1 in the sync participates in the stuffing
        # but there is guaranteed to be a 0 in the PID field before 6 1's go by
        # so we don't bother including it in packet_bits.
        unstuffed_bits, stuffed_bits, stuffing_errors = _unstuff(packet_bits[0:len(packet_bits)-eop_bits])
        
        # Low and Full speed packets should have no stuffing errors
        # HighSpeed packets will have stuffing errors from their EOP.
        continue_decode = True
        #print('@@@@@@@@@@@@@ STUFFING ERRORS', stuffing_errors, len(packet_bits), len(stuffed_bits))
        if len(stuffing_errors) > 0:
            if (pkt_speed == USBSpeed.LowSpeed or pkt_speed == USBSpeed.FullSpeed):
                continue_decode = False # there was a stuffing error
            else: # HighSpeed
                # For all packets except SOF there should be one stuffing error
                # before the end.
                if pid != USBPID.SOF:
                    if stuffing_errors[0] != len(packet_bits)-1:
                        continue_decode = False # there was a stuffing error not in the EOP
                else: #SOF HighSpeed will have multiple stuffing errors in EOP
                    # The first stuffing error should have been on bit-31 in the
                    # unstuffed data
                    if stuffing_errors[0] - len(stuffed_bits) != 31:
                        continue_decode = False # there was a stuffing error not in the EOP
                
        if continue_decode:
            ####### Now we decode the different packet types based on kind and PID
            short_packet = False
            
            # Special case for the EXT token
            if ext_packet_bits is not None:
                # Change the PID so we fall through the next section and hit the EXT
                # parsing code
                sub_pid = pid
                pid = USBPID.EXT
                packet_kind = USBPacketKind.Special
                
        
            #print('@@@ UNSTUFFED:', unstuffed_bits, len(unstuffed_bits))
            if (packet_kind == USBPacketKind.Token and pid != USBPID.SOF) or pid == USBPID.PING:
                ### Token packet. We should have 8 + 16 bits of data
                if len(unstuffed_bits) < (8 + 16): # not enough bits for packet
                    short_packet = True
                else:
                    addr_bits = unstuffed_bits[8:8+7]
                    addr = join_bits(reversed(addr_bits))

                    endp_bits = unstuffed_bits[8+7:8+11]
                    endp = join_bits(reversed(endp_bits))
                    
                    crc5_bits = unstuffed_bits[8+11:8+11+5]
                    # check the CRC
                    crc_check = usb_crc5(addr_bits + endp_bits)
                    status = USBStreamStatus.CRCError if crc_check != crc5_bits else StreamStatus.Ok
                    
                    # Construct the stream record
                    raw_packet = USBTokenPacket(pid, addr, endp, pkt_speed)
                    packet = USBStreamPacket((packet_start, packet_end), sop_end, raw_packet, crc5_bits, status=status)
                    yield packet

                
            elif pid == USBPID.SOF:
                ### SOF packet. We should have 8 + 16 bits of data
                if len(unstuffed_bits) < (8 + 16): # not enough bits for packet
                    short_packet = True
                else:
                    frame_num_bits = unstuffed_bits[8:8+11]
                    frame_num = join_bits(reversed(frame_num_bits))
                    crc5_bits = unstuffed_bits[8+11:8+11+5]
                    # check the CRC
                    crc_check = usb_crc5(frame_num_bits)
                    status = USBStreamStatus.CRCError if crc_check != crc5_bits else StreamStatus.Ok
                    
                    # Construct the stream record
                    raw_packet = USBSOFPacket(pid, frame_num, pkt_speed)
                    packet = USBStreamPacket((packet_start, packet_end), sop_end, raw_packet, crc5_bits, status=status)
                    yield packet
                
            elif packet_kind == USBPacketKind.Data:
                ### Data packet. Unknown length

                # Determine number of bytes in packet
                data_bits = len(unstuffed_bits) - 8 - 16 # take away PID and CRC bits
                data_bytes = data_bits // 8
                
                # Check for non-multiple of 8
                if data_bytes * 8 != data_bits:
                    short_packet = True
                else:
                    data = []
                    for i in xrange(data_bytes):
                        byte = join_bits(reversed(unstuffed_bits[8 + i*8: 8 + i*8 + 8]))
                        data.append(byte)
                    
                    #print('DECODED DATA:', data)
                    
                    crc16_bits = unstuffed_bits[-16:]
                    
                    # check the CRC
                    crc_check = table_usb_crc16(data)
                    status = USBStreamStatus.CRCError if crc_check != crc16_bits else StreamStatus.Ok

                    # Construct the stream record
                    raw_packet = USBDataPacket(pid, data, pkt_speed)
                    packet = USBStreamPacket((packet_start, packet_end), sop_end, raw_packet, crc16_bits, status=status)
                    yield packet

                
            elif packet_kind == USBPacketKind.Handshake or pid == USBPID.ERR:
                ### Handshake packet. We should have 8-bits of data (already decoded into PID)
                # This also catches PREamble and ERR packets which use the same PID
                # PRE is only used in Low and Full speed USB
                # ERR is only used in High speed USB
                
                # Construct the stream record
                raw_packet = USBHandshakePacket(pid, pkt_speed)
                packet = USBStreamPacket((packet_start, packet_end), sop_end, raw_packet, status=StreamStatus.Ok)
                yield packet

            else: # One of the "special" packets
                if pid == USBPID.SPLIT:
                    ### Split packet. We should have 8 + 24 bits of data
                    if len(unstuffed_bits) < (8 + 24): # not enough bits for packet
                        short_packet = True
                    else:
                        addr_bits = unstuffed_bits[8:8+7]
                        addr = join_bits(reversed(addr_bits))
                        
                        sc = unstuffed_bits[8+7]
                        
                        port_bits = unstuffed_bits[8+8:8+8+7]
                        port = join_bits(reversed(port_bits))
                        
                        s = unstuffed_bits[8+7+1+7]
                        
                        e = unstuffed_bits[8+7+1+7+1]
                        
                        et_bits = unstuffed_bits[8+17:8+17+2]
                        et = join_bits(reversed(et_bits))
                        
                        crc5_bits = unstuffed_bits[8+17+2:8+17+2+5]
                        # check the CRC
                        crc_check = usb_crc5(addr_bits + [sc] + port_bits + [s, e] + et_bits)
                        status = USBStreamStatus.CRCError if crc_check != crc5_bits else StreamStatus.Ok
                        
                        # Construct the stream record
                        raw_packet = USBSplitPacket(pid, addr, sc, port, s, e, et, pkt_speed)
                        packet = USBStreamPacket((packet_start, packet_end), sop_end, raw_packet, crc5_bits, status=status)
                        yield packet  
                    
                elif pid == USBPID.EXT:
                    ### Extended packet used by Link Power Management
                    if ext_packet_bits is None:
                        ### EXT packet part 1. We should have 8 + 16 bits of data
                        if len(unstuffed_bits) < (8 + 16): # not enough bits for packet
                            short_packet = True
                        else:
                            # Save the first packet of the EXT token
                            ext_packet_bits = unstuffed_bits
                            sop_end1 = sop_end

                        # The whole EXT packet is decoded below once we get the next half

                    else:
                        ### EXT packet part 1. We should have 8 + 16 bits of data (already verified)
                        addr_bits = ext_packet_bits[8:8+7]
                        addr = join_bits(reversed(addr_bits))

                        endp_bits = ext_packet_bits[8+7:8+11]
                        endp = join_bits(reversed(endp_bits))
                        
                        crc5_1_bits = ext_packet_bits[8+11:8+11+5]
                        # check the CRC
                        crc_check = usb_crc5(addr_bits + endp_bits)
                        status1 = USBStreamStatus.CRCError if crc_check != crc5_1_bits else StreamStatus.Ok
                        
                        # EXT packet part 2. We should have 8 + 16 bits of data
                        if len(unstuffed_bits) < (8 + 16):
                            short_packet = True
                        else:
                            #sub_pid = pid
                            
                            variable_bits = unstuffed_bits[8:8+11]
                            variable = join_bits(reversed(variable_bits))
                            crc5_2_bits = unstuffed_bits[8+11:8+11+5]
                            # check the CRC
                            crc_check = usb_crc5(variable_bits)
                            status2 = USBStreamStatus.CRCError if crc_check != crc5_2_bits else StreamStatus.Ok
                            
                            # Construct the stream record
                            raw_packet = USBEXTPacket(USBPID.EXT, addr, endp, sub_pid, variable, pkt_speed)
                            status = max(status1, status2)
                            packet = USBStreamPacket((packet_start, packet_end), sop_end1, raw_packet, crc5_1_bits, status=status)
                            packet.crc2 = crc5_2_bits
                            packet.sop_end2 = sop_end
                            yield packet

                        ext_packet_bits = None # Revert to normal processing of packets

                    
            if short_packet:
                # We had a bad packet with insufficient bits
                status = USBStreamStatus.ShortPacketError
                yield USBStreamError((packet_start, packet_end), unstuffed_bits, pid=pid, status=status)
                ext_packet_bits = None # Revert to normal processing of packets
                
        else: # handle stuffing error
            status = USBStreamStatus.BitStuffingError
            yield USBStreamError((packet_start, packet_end), packet_bits, pid=pid, status=status)
            ext_packet_bits = None # Revert to normal processing of packets


        
def _unstuff(packet_bits):
    '''Remove stuffed bits from a list of bits representing a packet'''

    unstuffed = []
    ones = 0
    expect_stuffing = False
    stuffing_errors = []
    stuffed_bits = []
    for i, b in enumerate(packet_bits):
        if not expect_stuffing:
            unstuffed.append(b)
        else:
            # should have a stuffed 0
            if b != 0:
                stuffing_errors.append(i)
            else:
                stuffed_bits.append(i)

        expect_stuffing = False

        if b == 1:
            ones += 1
        else:
            ones = 0
            
        if ones == 6:
            # next bit should be a stuffed 0
            expect_stuffing = True
            ones = 0
            
    return (unstuffed, stuffed_bits, stuffing_errors)

    
def _decode_NRZI(packet_states):
    '''Convert NRZI states (J,K) to bits (0,1)'''
    # previous state was a K from end of sync
    prev_state = USBState.K
    bits = []
    for s in packet_states:
        if s == prev_state: # no toggle -> 1-bit
            bits.append(1)
        else: # toggle -> 0-bit
            bits.append(0)
            
        prev_state = s
            
    return bits


def _convert_single_ended_states(es, bus_speed):
    '''Convert a stream of single-ended states for D+ and D- to
    logical states (J, K, SE0). Small skews between D+ and D- are filtered
    out to eliminate spurious SE0's and SE1's in the stream.
    
    This is a generator function.
    
    Yields a 2-tuple (time, state) representing the state of D+ and D-
    '''
    # Establish J/K state values
    if bus_speed == USBSpeed.LowSpeed:
        J_DP = 0
        J_DM = 1
    else:
        J_DP = 1
        J_DM = 0
        
    K_DP = 1 - J_DP
    K_DM = 1 - J_DM
    
    def decode_state(cur_dp, cur_dm):
        cur_bus = USBState.SE1
        if cur_dp == 0    and cur_dm == 0:    cur_bus = USBState.SE0
        if cur_dp == J_DP and cur_dm == J_DM: cur_bus = USBState.J
        if cur_dp == K_DP and cur_dm == K_DM: cur_bus = USBState.K
            
        return cur_bus
            
    cur_bus = decode_state(es.cur_state('dp'), es.cur_state('dm'))
    yield (es.cur_time(), cur_bus)
    
    while not es.at_end():
        es.advance_to_edge()
        
        # Due to channel skew we can get erroneous SE0 and SE1 decodes
        # on the bus so skip ahead by a small amount to ensure that any
        # near simultaneous transition has passed.
        
        #The skew adjustment should be no more than half a bit period
        if bus_speed == USBSpeed.LowSpeed:
            skew_adjust = 200.0e-9
        elif bus_speed == USBSpeed.FullSpeed:
            skew_adjust = 20.0e-9
        else: #HighSpeed
            skew_adjust = 1.0e-9
        
        es.advance(skew_adjust)
        
        cur_bus = decode_state(es.cur_state('dp'), es.cur_state('dm'))
        yield (es.cur_time() - skew_adjust, cur_bus)
        

def _convert_differential_states(es, bus_speed):
    '''Convert a stream of differential states for D+ - D- to
    logical states (J, K, SE0).
    
    This is a generator function.
    
    Yields a 2-tuple (time, state) representing the state of the differential pair.
    '''
    # Establish J/K state values
    if bus_speed == USBSpeed.LowSpeed:
        DIFF_J = -1
        DIFF_K = 1
    else:
        DIFF_J = 1
        DIFF_K = -1

    DIFF_SE0 = 0
    
    def decode_state(cur_diff):
        cur_bus = USBState.SE1
        if cur_diff == DIFF_J: cur_bus = USBState.J
        if cur_diff == DIFF_K: cur_bus = USBState.K
        if cur_diff == DIFF_SE0: cur_bus = USBState.SE0
        
        return cur_bus
        
    
    cur_bus = decode_state(es.cur_state())
    yield (es.cur_time, cur_bus)
    
    while not es.at_end():
        #prev_bus = cur_bus
        es.advance_to_edge()
        
        cur_bus = decode_state(es.cur_state())
        yield (es.cur_time, cur_bus)

    

def usb_synth(packets, idle_start=0.0, idle_end=0.0):
    '''Generate synthesized USB waveforms
    
    This function simulates USB packet transmission on the D+ and D- signals.
    
    packets
        A sequence of USBPacket objects that are to be simulated
    
    idle_start
        The amount of idle time before the transmission of packets begins
    
    idle_end
        The amount of idle time after the last packet

    Returns a pair of iterators (dp, dm) for the D+ and D- channels. Each
      iterator is a 2-tuple (time, value) representing the time and the
      logic value (0 or 1) for each edge transition on D+ and D-. The first tuple
      yielded is the initial state of the waveform. All remaining tuples are
      edges where the state changes.
    '''
    # This is a wrapper around the actual synthesis code in _usb_synth()
    # It unzips the yielded tuple and removes unwanted artifact edges
    dp, dm = itertools.izip(*_usb_synth(packets, idle_start, idle_end))
    dp = remove_excess_edges(dp)
    dm = remove_excess_edges(dm)
    return dp, dm

def _usb_synth(packets, idle_start=0.0, idle_end=0.0):
    '''Core USB sunthesizer
    
    This is a generator function.
    '''
    
    t = 0.0
    dp = 0
    dm = 0
    
    yield ((t, dp), (t, dm)) # initial conditions
    t += idle_start

    for p in packets:
        edges_dp , edges_dm = p.get_edges(t)
        
        for dp, dm in zip(edges_dp, edges_dm):
            yield (dp, dm)
        
        # update time to end of edge sequence plus a clock period
        t = edges_dp[-1][0] + USBClockPeriod[p.speed]
 
    dp = 0
    dm = 0
    yield ((t, dp), (t, dm))
    t += idle_end
    yield ((t, dp), (t, dm)) # final state

    
    
def usb_diff_synth(packets, idle_start=0.0, idle_end=0.0):
    '''Generate synthesized differential USB waveforms
    
    This function simulates USB packet transmission on the differential D+ - D-
    signal.
    
    packets
        A sequence of USBPacket objects that are to be simulated
    
    idle_start
        The amount of idle time before the transmission of packets begins
    
    idle_end
        The amount of idle time after the last packet

    Returns an iterator of 2-tuples for the D+ - D- differential channel. Each
      2-tuple is a (time, value) pair representing the time and the
      logic value (-1, 0, or 1) for each edge transition. The first tuple
      yielded is the initial state of the waveform. All remaining tuples are
      edges where the state changes.
    ''' 
    # This is a wrapper around the actual synthesis code in _usb_diff_synth()
    diff_d = _usb_diff_synth(packets, idle_start, idle_end)
    diff_d = remove_excess_edges(diff_d)
    return diff_d

def _usb_diff_synth(packets, idle_start=0.0, idle_end=0.0):
    t = 0.0
    diff_d = 0
    
    yield (t, diff_d) # initial conditions
    t += idle_start

    for p in packets:
        diff_edges = p.get_diff_edges(t)
        
        for e in diff_edges:
            yield e
        
        # update time to end of edge sequence plus a clock period
        t = diff_edges[-1][0] + USBClockPeriod[p.speed]
 
    yield (t, diff_d)
    t += idle_end
    yield (t, diff_d) # final state



def usb_crc5(d):
    '''Calculate USB CRC-5 on data
    d
        Array of integers representing 0 or 1 bits in transmission order
        
    Returns array of integers for each bit in the CRC with LSB first
    '''
    poly = 0x5   # USB CRC-5 polynomial
    sreg = 0x1f  # prime register with 1's
    mask = 0x1f
    
    for b in d:
        leftbit = (sreg & 0x10) >> 4
        sreg = (sreg << 1) & mask
        if b != leftbit:
            sreg ^= poly

    crc = sreg ^ mask  # invert shift register contents
    # Note: crc is in LSB-first order    
    return split_bits(crc, 5)


def usb_crc16(d):
    '''Calculate USB CRC-16 on data
    d
        Array of integers representing 0 or 1 bits in transmission order
        
    Returns array of integers for each bit in the CRC with LSB first
    '''
    poly = 0x8005  # USB CRC-16 polynomial
    sreg = 0xffff  # prime register with 1's
    mask = 0xffff
    
    for b in d:
        leftbit = (sreg & 0x8000) >> 15
        sreg = (sreg << 1) & mask
        if b != leftbit:
            sreg ^= poly

    crc = sreg ^ mask  # invert shift register contents
    # Note: crc is in LSB-first order
    return split_bits(crc, 16)

    
    
def _crc16_table_gen():
    poly = 0x8005 # USB CRC-16 polynomial
    mask = 0xffff

    tbl = [0] * 256
        
    for i in xrange(len(tbl)):
        sreg = i
        sreg = int('{:08b}'.format(sreg)[::-1], base=2) # reverse the bits

        sreg <<= 8
        for j in xrange(8):
            if sreg & 0x8000 != 0:
                sreg = (sreg << 1) ^ poly
            else:
                sreg = sreg << 1
                
        sreg = sreg & mask # remove shifted out bits
        sreg = int('{:016b}'.format(sreg)[::-1], base=2) # reverse the bits
        tbl[i] = sreg & mask
        
    return tbl
    
_crc16_table = _crc16_table_gen()


def table_usb_crc16(d):
    '''Calculate USB CRC-16 on data
    
    This is a table-based byte-wise implementation
    
    d
        Array of integers representing bytes
        
    Returns array of integers for each bit in the CRC with LSB first
    '''
    
    sreg = 0xffff # prime register with 1's
    mask = 0xffff
    
    tbl = _crc16_table

    for byte in d:
        tidx = (sreg ^ byte) & 0xff
        sreg = ((sreg >> 8) ^ tbl[tidx]) & mask

    sreg = int('{:016b}'.format(sreg)[::-1], base=2) # reverse the bits
    
    crc = sreg ^ mask # invert shift register contents
    
    # Note: crc is in LSB-first order
    return split_bits(crc, 16)
