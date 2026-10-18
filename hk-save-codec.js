/*
 * Hollow Knight save-file codec — the container the game uses for
 * user1.dat … user4.dat, also what save editors hand back.
 *
 * A .dat file is laid out as:
 *
 *   [22-byte C# BinaryWriter header]               (constant, HK_HEADER)
 *   [base-128 varint: byte length of the base64]   ("length-prefixed string")
 *   [base64( AES-256-ECB( pkcs7( UTF-8( save JSON ) ) ) )]
 *   [0x0b]                                         (constant tail byte)
 *
 * Editors such as https://hollowedit.com/ export exactly these bytes; iPad
 * Safari merely renames the download to user1.dat.html because the payload
 * arrives with an HTML-ish MIME type. This module lets the page accept all
 * of those (.dat, .dat.html, plain save JSON, base64 text, HTML wrappers).
 *
 * Attached to window.HKSaveCodec (globalThis outside browsers):
 *   decodeDat(bytes)  -> save JSON string       (throws if it is not a save)
 *   encodeDat(json)   -> Uint8Array             (.dat container bytes)
 *   findSave(bytes)   -> save JSON string|null  (tolerant sniffing)
 */
(function (global) {
  'use strict';

  /* ------------------------------------------------------------------ *
   * AES-256-ECB (the key below is the game's hard-coded save key)
   * ------------------------------------------------------------------ */

  var AES_KEY = [
    85, 75, 117, 53, 50, 101, 80, 85, 66, 119, 101, 116, 90, 57, 119, 78,
    88, 56, 56, 111, 53, 52, 100, 110, 102, 75, 82, 117, 48, 84, 49, 108,
  ]; // 'UKu52ePUBwetZ9wNX88o54dnfKRu0T1l'

  var SBOX = new Uint8Array(256);
  var INV_SBOX = new Uint8Array(256);
  var RCON = new Uint32Array(15);
  var MUL2 = new Uint8Array(256), MUL3 = new Uint8Array(256);
  var MUL9 = new Uint8Array(256), MUL11 = new Uint8Array(256);
  var MUL13 = new Uint8Array(256), MUL14 = new Uint8Array(256);

  function xtime(x) {
    return ((x << 1) ^ (x & 0x80 ? 0x11b : 0)) & 0xff;
  }
  function rot8(b, n) {
    return ((b << n) | (b >>> (8 - n))) & 0xff;
  }

  (function buildTables() {
    var log = new Uint8Array(256), exp = new Uint8Array(256);
    var x = 1, i, j, b;
    for (i = 0; i < 255; i++) {          // log/exp over GF(2^8), generator 3
      exp[i] = x;
      log[x] = i;
      x = (x ^ xtime(x)) & 0xff;
    }
    exp[255] = 1;                        // 3^255 wraps to 3^0 (used by exp[255 - log[1]])
    SBOX[0] = 0x63;
    for (i = 1; i < 256; i++) {
      b = exp[255 - log[i]];             // multiplicative inverse
      b = (b ^ rot8(b, 1) ^ rot8(b, 2) ^ rot8(b, 3) ^ rot8(b, 4) ^ 0x63) & 0xff;
      SBOX[i] = b;
      INV_SBOX[b] = i;
    }
    for (i = 1; i < 15; i++) {           // RCON[0] unused
      RCON[i] = (RCON[i - 1] ? xtime(RCON[i - 1]) : 1);
    }
    for (i = 0; i < 256; i++) {          // MUL2/MUL3 must be complete first:
      MUL2[i] = xtime(i);                // the 9/11/13/14 formulas below read
      MUL3[i] = xtime(i) ^ i;            // MUL2 at arbitrary indices
    }
    for (i = 0; i < 256; i++) {
      var m8 = MUL2[MUL2[MUL2[i]]];
      var m4 = MUL2[MUL2[i]];
      MUL9[i] = m8 ^ i;                  //  9 = 8 ^ 1
      MUL11[i] = m8 ^ MUL2[i] ^ i;       // 11 = 8 ^ 2 ^ 1
      MUL13[i] = m8 ^ m4 ^ i;            // 13 = 8 ^ 4 ^ 1
      MUL14[i] = m8 ^ m4 ^ MUL2[i];      // 14 = 8 ^ 4 ^ 2
    }
  })();

  function subWord(x) {
    return ((SBOX[(x >>> 24) & 0xff] << 24) | (SBOX[(x >>> 16) & 0xff] << 16) |
            (SBOX[(x >>> 8) & 0xff] << 8) | SBOX[x & 0xff]) | 0;
  }

  /* 32-byte key -> 60 round-key words (AES-256, 14 rounds) */
  function expandKey(keyBytes) {
    var w = new Uint32Array(60), i, t;
    for (i = 0; i < 8; i++) {
      w[i] = ((keyBytes[4 * i] << 24) | (keyBytes[4 * i + 1] << 16) |
              (keyBytes[4 * i + 2] << 8) | keyBytes[4 * i + 3]) | 0;
    }
    for (i = 8; i < 60; i++) {
      t = w[i - 1];
      if (i % 8 === 0) t = (subWord(((t << 8) | (t >>> 24)) | 0) ^ (RCON[i / 8] << 24)) | 0;
      else if (i % 8 === 4) t = subWord(t);
      w[i] = (w[i - 8] ^ t) | 0;
    }
    return w;
  }

  var ROUND_KEYS = expandKey(AES_KEY); // the key is fixed, expand once

  function keyByte(w, round, i) {
    return (w[round * 4 + (i >> 2)] >>> ((3 - (i & 3)) * 8)) & 0xff;
  }

  function shiftRowsSub(s, box, dir) {
    var t = new Uint8Array(16), r, c;
    for (r = 0; r < 4; r++) {
      for (c = 0; c < 4; c++) {
        t[4 * c + r] = box[s[4 * ((c + (dir ? (4 - r) : r)) & 3) + r]];
      }
    }
    s.set(t);
  }

  function mixColumns(s) {
    var c, i, a0, a1, a2, a3;
    for (c = 0; c < 4; c++) {
      i = 4 * c;
      a0 = s[i]; a1 = s[i + 1]; a2 = s[i + 2]; a3 = s[i + 3];
      s[i]     = MUL2[a0] ^ MUL3[a1] ^ a2 ^ a3;
      s[i + 1] = a0 ^ MUL2[a1] ^ MUL3[a2] ^ a3;
      s[i + 2] = a0 ^ a1 ^ MUL2[a2] ^ MUL3[a3];
      s[i + 3] = MUL3[a0] ^ a1 ^ a2 ^ MUL2[a3];
    }
  }

  function invMixColumns(s) {
    var c, i, a0, a1, a2, a3;
    for (c = 0; c < 4; c++) {
      i = 4 * c;
      a0 = s[i]; a1 = s[i + 1]; a2 = s[i + 2]; a3 = s[i + 3];
      s[i]     = MUL14[a0] ^ MUL11[a1] ^ MUL13[a2] ^ MUL9[a3];
      s[i + 1] = MUL9[a0] ^ MUL14[a1] ^ MUL11[a2] ^ MUL13[a3];
      s[i + 2] = MUL13[a0] ^ MUL9[a1] ^ MUL14[a2] ^ MUL11[a3];
      s[i + 3] = MUL11[a0] ^ MUL13[a1] ^ MUL9[a2] ^ MUL14[a3];
    }
  }

  function addRoundKey(s, round) {
    for (var i = 0; i < 16; i++) s[i] ^= keyByte(ROUND_KEYS, round, i);
  }

  function encryptBlock(input, output, offset) {
    var s = new Uint8Array(16), r, i;
    for (i = 0; i < 16; i++) s[i] = input[offset + i];
    addRoundKey(s, 0);
    for (r = 1; r < 14; r++) {
      shiftRowsSub(s, SBOX, false);
      mixColumns(s);
      addRoundKey(s, r);
    }
    shiftRowsSub(s, SBOX, false);
    addRoundKey(s, 14);
    output.set(s, offset);
  }

  function decryptBlock(input, output, offset) {
    var s = new Uint8Array(16), r, i;
    for (i = 0; i < 16; i++) s[i] = input[offset + i];
    addRoundKey(s, 14);
    for (r = 13; r >= 1; r--) {
      shiftRowsSub(s, INV_SBOX, true);
      addRoundKey(s, r);
      invMixColumns(s);
    }
    shiftRowsSub(s, INV_SBOX, true);
    addRoundKey(s, 0);
    output.set(s, offset);
  }

  function ecbEncrypt(bytes) {
    var out = new Uint8Array(bytes.length);
    for (var off = 0; off < bytes.length; off += 16) encryptBlock(bytes, out, off);
    return out;
  }

  function ecbDecrypt(bytes) {
    var out = new Uint8Array(bytes.length);
    for (var off = 0; off < bytes.length; off += 16) decryptBlock(bytes, out, off);
    return out;
  }

  function pkcs7Pad(bytes) {
    var pad = 16 - (bytes.length % 16);
    var out = new Uint8Array(bytes.length + pad);
    out.set(bytes);
    for (var i = bytes.length; i < out.length; i++) out[i] = pad;
    return out;
  }

  function pkcs7Unpad(bytes) {
    var pad = bytes[bytes.length - 1];
    if (pad < 1 || pad > 16 || bytes.length < pad) throw new Error('bad pkcs7 padding');
    return bytes.subarray(0, bytes.length - pad);
  }

  /* ------------------------------------------------------------------ *
   * base64 / UTF-8 helpers
   * ------------------------------------------------------------------ */

  function b64ToBytes(text) {
    text = String(text).replace(/[\s\r\n]+/g, '');
    var bin = atob(text);
    var out = new Uint8Array(bin.length);
    for (var i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  }

  function bytesToB64(bytes) {
    var bin = '';
    for (var i = 0; i < bytes.length; i += 0x8000) {
      bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    }
    return btoa(bin);
  }

  function utf8Encode(text) {
    return new TextEncoder().encode(text);
  }

  function utf8Decode(bytes) {
    return new TextDecoder('utf-8', { fatal: false }).decode(bytes);
  }

  /* ------------------------------------------------------------------ *
   * .dat container
   * ------------------------------------------------------------------ */

  var HK_HEADER = [0, 1, 0, 0, 0, 255, 255, 255, 255, 1, 0, 0, 0, 0, 0, 0, 0, 6, 1, 0, 0, 0];

  function looksLikeDat(bytes) {
    if (!bytes || bytes.length < HK_HEADER.length + 20) return false;
    for (var i = 0; i < HK_HEADER.length; i++) {
      if (bytes[i] !== HK_HEADER[i]) return false;
    }
    return true;
  }

  /* Parse the container, AES-decrypt and JSON-validate. Throws on anything
   * that is not a Hollow Knight save. */
  function decodeDat(bytes) {
    if (!bytes || bytes.length < 64) throw new Error('save file is too short');
    var p = HK_HEADER.length;                 // skip the header without requiring an exact match
    var len = 0, shift = 0, count = 0, b;
    while (p < bytes.length - 1) {            // keep the trailing byte out of the varint
      b = bytes[p++];
      count++;
      len |= (b & 0x7f) << shift;
      if ((b & 0x80) === 0) break;
      shift += 7;
      if (count > 5) throw new Error('bad length-prefixed string');
    }
    if (p + len > bytes.length) throw new Error('truncated base64 payload');
    var b64 = '';
    for (var q = p; q < p + len; q += 0x8000) {   // chunked: apply() has arg limits
      b64 += String.fromCharCode.apply(null, bytes.subarray(q, Math.min(q + 0x8000, p + len)));
    }
    var cipher = b64ToBytes(b64);
    if (!cipher.length || (cipher.length & 15) !== 0) throw new Error('payload is not AES-sized');
    var plain = ecbDecrypt(cipher);
    var json;
    try {
      json = utf8Decode(pkcs7Unpad(plain));
    } catch (e) {
      json = utf8Decode(plain);               // editors with buggy padding:
    }
    try {                                     // the real "is this a save" check
      JSON.parse(json);
      return json;
    } catch (e) { }
    var end = json.lastIndexOf('}');          // recover trailing garbage from
    if (end > 0) {                            // a bad pad, then re-validate
      var trimmed = json.slice(0, end + 1);
      JSON.parse(trimmed);
      return trimmed;
    }
    throw new Error('decrypted payload is not a save');
  }

  function varint(n) {
    var out = [];
    while (n > 0x7f) {
      out.push((n & 0x7f) | 0x80);
      n >>= 7;
    }
    out.push(n);
    return out;
  }

  function encodeDat(json) {
    JSON.parse(json);                         // fail early on malformed saves
    var cipher = ecbEncrypt(pkcs7Pad(utf8Encode(json)));
    var b64 = bytesToB64(cipher);
    var lps = varint(b64.length);
    var out = new Uint8Array(HK_HEADER.length + lps.length + b64.length + 1);
    var p = 0, i;
    for (i = 0; i < HK_HEADER.length; i++) out[p++] = HK_HEADER[i];
    for (i = 0; i < lps.length; i++) out[p++] = lps[i];
    for (i = 0; i < b64.length; i++) out[p++] = b64.charCodeAt(i);
    out[p] = 0x0b;
    return out;
  }

  /* Try to decrypt a bare base64 payload (no container). */
  function decodePayloadB64(b64) {
    var cipher = b64ToBytes(b64);
    if (!cipher.length || (cipher.length & 15) !== 0) throw new Error('payload is not AES-sized');
    var json = utf8Decode(pkcs7Unpad(ecbDecrypt(cipher)));
    JSON.parse(json);
    return json;
  }

  /* Tolerant sniffing: find the save JSON inside .dat bytes, plain JSON,
   * bare base64 text, or an HTML page wrapping any of those. */
  function findSave(bytes) {
    var err = null;
    try {                                     // 1) the normal container
      return decodeDat(bytes);
    } catch (e) { err = e; }

    var text = utf8Decode(bytes);             // 2) text-ish inputs
    var trimmed = text.replace(/^[\ufeff\s]+/, '');
    if (trimmed.charAt(0) === '{') {          //    a plain, unencrypted save
      JSON.parse(trimmed);
      return trimmed;
    }
    try {                                     // 3) bare base64 ciphertext
      return decodePayloadB64(trimmed);
    } catch (e) { /* keep scanning below */ }

    var runs = text.match(/[A-Za-z0-9+/]{64,}={0,2}/g) || [];  // 4) inside HTML/text
    for (var i = 0; i < runs.length && i < 25; i++) {
      try {
        return decodePayloadB64(runs[i]);
      } catch (e) { /* next candidate */ }
    }
    throw err || new Error('not a Hollow Knight save file');
  }

  var codec = {
    decodeDat: decodeDat,
    encodeDat: encodeDat,
    findSave: findSave,
    looksLikeDat: looksLikeDat,
    /* exposed for the test suite (tools/test-save-import.mjs) */
    _internals: {
      ecbEncrypt: ecbEncrypt,
      ecbDecrypt: ecbDecrypt,
      pkcs7Pad: pkcs7Pad,
      pkcs7Unpad: pkcs7Unpad,
      shiftRowsSub: shiftRowsSub,
      SBOX: SBOX,
      INV_SBOX: INV_SBOX,
      mixColumns: mixColumns,
      invMixColumns: invMixColumns,
      addRoundKey: addRoundKey,
    },
  };

  global.HKSaveCodec = codec;
})(typeof window !== 'undefined' ? window : globalThis);
