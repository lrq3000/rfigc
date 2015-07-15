#!/usr/bin/env python
#
# ECC manager facade api
# Allows to easily use different kinds of ECC algorithms and libraries under one single class.
# Copyright (C) 2015 Larroque Stephen
#
# Licensed under the MIT License (MIT)
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.

# Compatibility with Python 3
from _compat import _str

# ECC libraries
try:
    import lib.reedsolomon.creedsolo as reedsolo
    import lib.brownanrs.rs as brownanrs
    #import lib.brownanrs.crs as brownanrs
except ImportError:
    import lib.brownanrs.rs as brownanrs # Pure python implementation of Reed-Solomon with configurable max_block_size and automatic error detection (you don't have to specify where they are). This is a base 3 implementation that is formally correct and with unit tests.
    import lib.reedsolomon.reedsolo as reedsolo # Faster pure python implementation of Reed-Solomon, with a base 3 compatible encoder (but not yet decoder! But you can use brownanrs to decode).

rs_encode_msg = reedsolo.rs_encode_msg # local reference for small speed boost
#rs_encode_msg_precomp = reedsolo.rs_encode_msg_precomp

def compute_ecc_params(max_block_size, rate, hasher):
    '''Compute the ecc parameters (size of the message, size of the hash, size of the ecc). This is an helper function to easily compute the parameters from a resilience rate to instanciate an ECCMan object.'''
    #message_size = max_block_size - int(round(max_block_size * rate * 2, 0)) # old way to compute, wasn't really correct because we applied the rate on the total message+ecc size, when we should apply the rate to the message size only (that is not known beforehand, but we want the ecc size (k) = 2*rate*message_size or in other words that k + k * 2 * rate = n)
    message_size = int(round(float(max_block_size) / (1 + 2*rate), 0))
    ecc_size = max_block_size - message_size
    hash_size = len(hasher) # 32 when we use MD5
    return {"message_size": message_size, "ecc_size": ecc_size, "hash_size": hash_size}

class ECCMan(object):
    '''Error correction code manager, which provides a facade API to use different kinds of ecc algorithms or libraries.'''

    def __init__(self, n, k, algo=1):
        self.c_exp = 8 # we stay in GF(2^8) for this software
        self.field_charac = int((2**self.c_exp) - 1)

        if algo == 1 or algo == 2: # brownanrs library implementations: fully correct base 3 implementation, and mode 2 is for fast encoding
            self.gen_nb = 3
            self.prim = 0x11b
            self.fcr = 1

            self.ecc_manager = brownanrs.RSCoder(n, k, generator=self.gen_nb, prim=self.prim, fcr=self.fcr)
        elif algo == 3: # reedsolo fast implementation, compatible with brownanrs in base 3
            self.gen_nb = 3
            self.prim = 0x11b
            self.fcr = 1

            reedsolo.init_tables(generator=self.gen_nb, prim=self.prim)
            self.g = reedsolo.rs_generator_poly_all(n, fcr=self.fcr, generator=self.gen_nb)
            #self.gf_mul_arr, self.gf_add_arr = reedsolo.gf_precomp_tables()
        elif algo == 4: # reedsolo fast implementation, incompatible with any other implementation
            self.gen_nb = 2
            self.prim = 0x187
            self.fcr = 120

            reedsolo.init_tables(self.prim) # parameters for US FAA ADSB UAT RS FEC
            self.g = reedsolo.rs_generator_poly_all(n, fcr=self.fcr, generator=self.gen_nb)

        self.algo = algo
        self.n = n
        self.k = k

    def encode(self, message, k=None):
        '''Encode one message block (up to 255) into an ecc'''
        if not k: k = self.k
        message, _ = self.pad(message, k=k)
        if self.algo == 1:
            mesecc = self.ecc_manager.encode(message, k=k)
        elif self.algo == 2:
            mesecc = self.ecc_manager.encode_fast(message, k=k)
        elif self.algo == 3 or self.algo == 4:
            mesecc = rs_encode_msg(message, self.n-k, fcr=self.fcr, gen=self.g[self.n-k])
            #mesecc = rs_encode_msg_precomp(message, self.n-k, fcr=self.fcr, gen=self.g[self.n-k])

        ecc = mesecc[len(message):]
        return ecc

    def decode(self, message, ecc, k=None, enable_erasures=False, erasures_char="\x00", only_erasures=False):
        '''Repair a message and its ecc also, given the message and its ecc (both can be corrupted, we will still try to fix both of them)'''
        if not k: k = self.k

        # Optimization, use bytearray
        if isinstance(message, _str):
            message = bytearray([ord(x) for x in message])
            ecc = bytearray([ord(x) for x in ecc])

        # Detect erasures positions and replace with null bytes (replacing erasures with null bytes is necessary for correct syndrome computation)
        # Note that this must be done before padding, else we risk counting the padded null bytes as erasures!
        erasures_pos = None
        if enable_erasures:
            # Concatenate to find erasures in the whole codeword
            mesecc = message + ecc
            # Convert char to a int (because we use a bytearray)
            if isinstance(erasures_char, _str): erasures_char = ord(erasures_char)
            # Find the positions of the erased characters
            erasures_pos = [i for i in xrange(len(mesecc)) if mesecc[i] == erasures_char]
            # Failing case: no erasures could be found and we want to only correct erasures, then we return the message as-is
            if only_erasures and not erasures_pos: return message, ecc

        # Pad with null bytes if necessary
        message, pad = self.pad(message, k=k)
        ecc, _ = self.rpad(ecc, k=k) # fill ecc with null bytes if too small (maybe the field delimiters were misdetected and this truncated the ecc? But we maybe still can correct if the truncation is less than the resilience rate)
        # If the message was left padded, then we need to update the positions of the erasures
        if erasures_pos and pad:
            len_pad = len(pad)
            erasures_pos = [x+len_pad for x in erasures_pos]

        # Decoding
        if self.algo == 1:
            msg_repaired, ecc_repaired = self.ecc_manager.decode(message + ecc, nostrip=True, k=k, erasures_pos=erasures_pos, only_erasures=only_erasures) # Avoid automatic stripping because we are working with binary streams, thus we should manually strip padding only when we know we padded
        elif self.algo == 2:
            msg_repaired, ecc_repaired = self.ecc_manager.decode_fast(message + ecc, nostrip=True, k=k, erasures_pos=erasures_pos, only_erasures=only_erasures)
        elif self.algo == 3:
            #msg_repaired, ecc_repaired = self.ecc_manager.decode_fast(message + ecc, nostrip=True, k=k, erasures_pos=erasures_pos, only_erasures=only_erasures)
            msg_repaired, ecc_repaired = reedsolo.rs_correct_msg_nofsynd(bytearray(message + ecc), self.n-k, fcr=self.fcr, generator=self.gen_nb, erase_pos=erasures_pos, only_erasures=only_erasures)
            msg_repaired = bytearray(msg_repaired)
            ecc_repaired = bytearray(ecc_repaired)
        elif self.algo == 4:
            msg_repaired, ecc_repaired = reedsolo.rs_correct_msg(bytearray(message + ecc), self.n-k, fcr=self.fcr, generator=self.gen_nb, erase_pos=erasures_pos, only_erasures=only_erasures)
            msg_repaired = bytearray(msg_repaired)
            ecc_repaired = bytearray(ecc_repaired)

        if pad: # Strip the null bytes if we padded the message before decoding
            msg_repaired = msg_repaired[len(pad):len(msg_repaired)]
        return msg_repaired, ecc_repaired

    def pad(self, message, k=None):
        '''Automatically left pad with null bytes a message if too small, or leave unchanged if not necessary. This allows to keep track of padding and strip the null bytes after decoding reliably with binary data. Equivalent to shortening (shortened reed-solomon code).'''
        if not k: k = self.k
        pad = None
        if len(message) < k:
            #pad = "\x00" * (k-len(message))
            pad = bytearray(k-len(message))
            message = pad + message
        return [message, pad]

    def rpad(self, ecc, k=None):
        '''Automatically right pad with null bytes an ecc to fill for missing bytes if too small, or leave unchanged if not necessary. This can be used as a workaround for field delimiter misdetection. Equivalent to puncturing (punctured reed-solomon code).'''
        if not k: k = self.k
        pad = None
        if len(ecc) < self.n-k:
            print("Warning: the ecc field may have been truncated (entrymarker or field_delim misdetection?).")
            #pad = "\x00" * (self.n-k-len(ecc))
            pad = bytearray(self.n-k-len(ecc))
            ecc = ecc + pad
        return [ecc, pad]

    def check(self, message, ecc, k=None):
        '''Check if there's any error in a message+ecc. Can be used before decoding, in addition to hashes to detect if the message was tampered, or after decoding to check that the message was fully recovered.'''
        if not k: k = self.k
        message, _ = self.pad(message, k=k)
        ecc, _ = self.rpad(ecc, k=k)
        if self.algo == 1 or self.algo == 2:
            return self.ecc_manager.check_fast(message + ecc, k=k)
        elif self.algo == 3 or self.algo == 4:
            return reedsolo.rs_check(bytearray(message + ecc), self.n-k, fcr=self.fcr, generator=self.gen_nb)

    def description(self):
        '''Provide a description for each algorithm available, useful to print in ecc file'''
        if self.algo <= 3:
            return "Reed-Solomon with polynomials in Galois field of characteristic %i (2^%i) with generator=%s, prime poly=%s and first consecutive root=%s." % (self.field_charac, self.c_exp, self.gen_nb, hex(self.prim), self.fcr)
        elif self.algo == 4:
            return "Reed-Solomon with polynomials in Galois field of characteristic %i (2^%i) under US FAA ADSB UAT RS FEC standard with generator=%s, prime poly=%s and first consecutive root=%s." % (self.field_charac, self.c_exp, self.gen_nb, hex(self.prim), self.fcr)
        else:
            return "No description for this ECC algorithm."