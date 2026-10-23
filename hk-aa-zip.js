/* ============================================================================
 * hk-aa-zip.js — zero-copy OPFS ZIP mount & streaming bundle server
 * ----------------------------------------------------------------------------
 * Why this exists:
 *   Previously, index.html extracted StreamingAssets/aa/WebGL.zip (~1.24 GB
 *   compressed / ~1.58 GB uncompressed across 21 Unity bundles) into Cache API
 *   at startup via:
 *
 *       var arrayBuf = await blob.arrayBuffer();   // ~1.24 GB contiguous RAM
 *       var zip = await JSZip.loadAsync(arrayBuf); // pins + slices ~1.24 GB
 *       Promise.all(10 x file.async("blob"));      // ~2.0+ GB parallel inflate
 *
 *   Peak WebContent RAM exceeded 3.5-4.5 GB right after chunk 63 finished
 *   (while localStorage["hk.stage"] was still "prepare aa"), causing iPadOS
 *   Jetsam to kill and refresh the Safari tab with:
 *       Previous attempt stopped during "prepare aa"
 *
 * What this module does instead:
 *   1. Reads ONLY the ZIP End-of-Central-Directory + Central Directory tail
 *      (~64 KB) + 30-byte local headers from the OPFS File via Blob.slice(),
 *      mounting all 21 bundles in ~2 ms using < 0.1 MB of RAM and 0 extra disk.
 *   2. Serves each requested .bundle on demand as a streaming HTTP Response
 *      with an exact Content-Length header, reading 256 KB slices from disk
 *      with pull backpressure through native DecompressionStream("deflate-raw")
 *      (Safari 16.4+, C++ zlib off the JS heap) with an automatic pure-JS
 *      streaming RFC 1951 raw-inflate fallback for older WebKit builds.
 * ==========================================================================*/

(function (root, factory) {
  if (typeof module === 'object' && module.exports) {
    module.exports = factory();
  } else {
    root.HKAaZip = factory();
  }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var EOCD_SIG = 0x06054b50;
  var ZIP64_LOC_SIG = 0x07064b50;
  var ZIP64_EOCD_SIG = 0x06064b50;
  var CD_SIG = 0x02014b50;
  var LFH_SIG = 0x04034b50;

  var READ_CHUNK_BYTES = 256 * 1024; // 256 KB disk slice per pull
  var OUT_CHUNK_BYTES = 64 * 1024;   // 64 KB inflated output chunks

  var utf8Decoder = typeof TextDecoder === 'function' ? new TextDecoder('utf-8') : null;

  function decodeUtf8(bytes) {
    if (utf8Decoder) return utf8Decoder.decode(bytes);
    var out = '';
    for (var i = 0; i < bytes.length; i++) out += String.fromCharCode(bytes[i]);
    return out;
  }

  function abortError() {
    var err = new Error('aborted');
    err.name = 'AbortError';
    return err;
  }

  function readUint64LE(view, offset) {
    var lo = view.getUint32(offset, true);
    var hi = view.getUint32(offset + 4, true);
    return hi * 4294967296 + lo;
  }

  /* ------------------------------------------------------------------ *
   * Runtime probe for DecompressionStream('deflate-raw')
   *   Safari 16.0-16.3 exposed DecompressionStream('gzip'/'deflate')
   *   WITHOUT 'deflate-raw' (added in Safari 16.4). Always self-test!
   * ------------------------------------------------------------------ */

  var nativeDeflateRawPromise = null;

  function supportsNativeDeflateRaw() {
    if (nativeDeflateRawPromise) return nativeDeflateRawPromise;
    nativeDeflateRawPromise = (async function () {
      try {
        if (typeof DecompressionStream !== 'function' || typeof ReadableStream !== 'function') {
          return false;
        }
        // Raw DEFLATE stream for ASCII "hello"
        var sample = new Uint8Array([0xcb, 0x48, 0xcd, 0xc9, 0xc9, 0x07, 0x00]);
        var src = new ReadableStream({
          start: function (c) {
            c.enqueue(sample);
            c.close();
          },
        });
        var reader = src.pipeThrough(new DecompressionStream('deflate-raw')).getReader();
        var out = [];
        var total = 0;
        for (;;) {
          var r = await reader.read();
          if (r.done) break;
          if (r.value && r.value.byteLength) {
            out.push(r.value);
            total += r.value.byteLength;
          }
        }
        if (total !== 5) return false;
        var merged = new Uint8Array(5);
        var pos = 0;
        for (var i = 0; i < out.length; i++) {
          merged.set(out[i], pos);
          pos += out[i].byteLength;
        }
        return decodeUtf8(merged) === 'hello';
      } catch (e) {
        return false;
      }
    })();
    return nativeDeflateRawPromise;
  }

  /* ------------------------------------------------------------------ *
   * Pure-JS streaming RFC 1951 raw DEFLATE decompressor (fallback)
   *   Total working state: 32 KB window + 64 KB output chunk + tables
   *   (~110 KB constant memory regardless of uncompressed bundle size).
   * ------------------------------------------------------------------ */

  var LEN_BASE = new Uint16Array([
    3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 15, 17, 19, 23, 27, 31,
    35, 43, 51, 59, 67, 83, 99, 115, 131, 163, 195, 227, 258,
  ]);
  var LEN_EXTRA = new Uint8Array([
    0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 2, 2, 2, 2,
    3, 3, 3, 3, 4, 4, 4, 4, 5, 5, 5, 5, 0,
  ]);
  var DIST_BASE = new Uint16Array([
    1, 2, 3, 4, 5, 7, 9, 13, 17, 25, 33, 49, 65, 97, 129, 193,
    257, 385, 513, 769, 1025, 1537, 2049, 3073, 4097, 6145, 8193, 12289, 16385, 24577,
  ]);
  var DIST_EXTRA = new Uint8Array([
    0, 0, 0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6,
    7, 7, 8, 8, 9, 9, 10, 10, 11, 11, 12, 12, 13, 13,
  ]);
  var CL_ORDER = new Uint8Array([
    16, 17, 18, 0, 8, 7, 9, 6, 10, 5, 11, 4, 12, 3, 13, 2, 14, 1, 15,
  ]);

  function reverseBits(code, len) {
    var res = 0;
    for (var i = 0; i < len; i++) {
      res = (res << 1) | (code & 1);
      code >>>= 1;
    }
    return res;
  }

  function buildHuffmanTable(lengths, count) {
    var maxBits = 0;
    var blCount = new Int32Array(16);
    var i;
    for (i = 0; i < count; i++) {
      var l = lengths[i];
      if (l > 0) {
        blCount[l]++;
        if (l > maxBits) maxBits = l;
      }
    }
    if (maxBits === 0) {
      return { table: new Uint32Array([0]), maxBits: 1 };
    }
    var nextCode = new Int32Array(16);
    var code = 0;
    for (var bits = 1; bits <= 15; bits++) {
      code = (code + blCount[bits - 1]) << 1;
      nextCode[bits] = code;
    }
    var size = 1 << maxBits;
    var table = new Uint32Array(size);
    for (var sym = 0; sym < count; sym++) {
      var len = lengths[sym];
      if (len === 0) continue;
      var c = nextCode[len]++;
      var rev = reverseBits(c, len);
      var entry = (len << 16) | sym;
      var step = 1 << len;
      for (var idx = rev; idx < size; idx += step) {
        table[idx] = entry;
      }
    }
    return { table: table, maxBits: maxBits };
  }

  var FIXED_LIT_TABLE = null;
  var FIXED_DIST_TABLE = null;

  function getFixedTables() {
    if (!FIXED_LIT_TABLE) {
      var litLens = new Uint8Array(288);
      var i;
      for (i = 0; i <= 143; i++) litLens[i] = 8;
      for (i = 144; i <= 255; i++) litLens[i] = 9;
      for (i = 256; i <= 279; i++) litLens[i] = 7;
      for (i = 280; i <= 287; i++) litLens[i] = 8;
      FIXED_LIT_TABLE = buildHuffmanTable(litLens, 288);

      var distLens = new Uint8Array(32);
      for (i = 0; i < 32; i++) distLens[i] = 5;
      FIXED_DIST_TABLE = buildHuffmanTable(distLens, 32);
    }
    return { lit: FIXED_LIT_TABLE, dist: FIXED_DIST_TABLE };
  }

  function createJsRawInflateStream(sourceStream) {
    var reader = sourceStream.getReader();
    var inBuf = null;
    var inPos = 0;
    var inLen = 0;
    var eof = false;

    var bitBuf = 0;
    var bitCnt = 0;

    var windowBuf = new Uint8Array(32768);
    var winPos = 0;
    var outBuf = new Uint8Array(OUT_CHUNK_BYTES);
    var outPos = 0;

    var pendingChunks = [];
    var finished = false;
    var streamErr = null;
    var pumping = null;
    var resumePump = null;

    function pauseIfFull() {
      if (pendingChunks.length < 2) return null;
      return new Promise(function (resolve) {
        resumePump = resolve;
      });
    }

    function pushOutputByte(b) {
      windowBuf[winPos] = b;
      winPos = (winPos + 1) & 32767;
      outBuf[outPos++] = b;
      if (outPos === OUT_CHUNK_BYTES) {
        pendingChunks.push(outBuf.slice(0, outPos));
        outPos = 0;
      }
    }

    function flushRemaining() {
      if (outPos > 0) {
        pendingChunks.push(outBuf.slice(0, outPos));
        outPos = 0;
      }
    }

    async function loadNextChunk() {
      while (inPos >= inLen) {
        var r = await reader.read();
        if (r.done) {
          eof = true;
          return false;
        }
        if (r.value && r.value.byteLength > 0) {
          inBuf = r.value;
          inPos = 0;
          inLen = r.value.byteLength;
          return true;
        }
      }
      return true;
    }

    async function ensureBits(n) {
      while (bitCnt < n) {
        if (inPos >= inLen) {
          var ok = await loadNextChunk();
          if (!ok) {
            throw new Error('Unexpected end of DEFLATE stream');
          }
        }
        bitBuf |= inBuf[inPos++] << bitCnt;
        bitCnt += 8;
      }
    }

    async function readBits(n) {
      if (n === 0) return 0;
      if (bitCnt < n) await ensureBits(n);
      var val = bitBuf & ((1 << n) - 1);
      bitBuf >>>= n;
      bitCnt -= n;
      return val;
    }

    async function decodeSymbol(huff) {
      var maxB = huff.maxBits;
      if (bitCnt < maxB) {
        // Pull available bytes without failing if near actual stream end
        while (bitCnt < maxB) {
          if (inPos >= inLen) {
            var ok = await loadNextChunk();
            if (!ok) break;
          }
          bitBuf |= inBuf[inPos++] << bitCnt;
          bitCnt += 8;
        }
      }
      var entry = huff.table[bitBuf & ((1 << maxB) - 1)];
      var len = entry >>> 16;
      if (len === 0 || len > bitCnt) {
        throw new Error('Invalid Huffman code in DEFLATE stream');
      }
      bitBuf >>>= len;
      bitCnt -= len;
      return entry & 0xffff;
    }

    async function runInflate() {
      try {
        var isFinal = 0;
        while (!isFinal) {
          isFinal = await readBits(1);
          var btype = await readBits(2);

          if (btype === 0) {
            // Uncompressed stored block: align to next byte boundary
            bitBuf = 0;
            bitCnt = 0;
            var len = await readBits(16);
            var nlen = await readBits(16);
            if ((len ^ 0xffff) !== nlen) {
              throw new Error('Corrupt stored block length in DEFLATE stream');
            }
            var remaining = len;
            while (remaining > 0) {
              if (inPos >= inLen) {
                var hasMore = await loadNextChunk();
                if (!hasMore) throw new Error('Truncated stored block in DEFLATE stream');
              }
              var avail = Math.min(remaining, inLen - inPos);
              for (var i = 0; i < avail; i++) {
                pushOutputByte(inBuf[inPos++]);
              }
              remaining -= avail;
              var waitStored = pauseIfFull();
              if (waitStored) await waitStored;
            }
          } else if (btype === 1 || btype === 2) {
            var litTable, distTable;
            if (btype === 1) {
              var fixed = getFixedTables();
              litTable = fixed.lit;
              distTable = fixed.dist;
            } else {
              var hlit = (await readBits(5)) + 257;
              var hdist = (await readBits(5)) + 1;
              var hclen = (await readBits(4)) + 4;
              var clLens = new Uint8Array(19);
              var cIdx;
              for (cIdx = 0; cIdx < hclen; cIdx++) {
                clLens[CL_ORDER[cIdx]] = await readBits(3);
              }
              var clTable = buildHuffmanTable(clLens, 19);
              var totalCodes = hlit + hdist;
              var codeLens = new Uint8Array(totalCodes);
              var n = 0;
              while (n < totalCodes) {
                var cSym = await decodeSymbol(clTable);
                if (cSym < 16) {
                  codeLens[n++] = cSym;
                } else if (cSym === 16) {
                  if (n === 0) throw new Error('Invalid repeat code 16 at start');
                  var rep16 = (await readBits(2)) + 3;
                  var prev = codeLens[n - 1];
                  while (rep16-- > 0) codeLens[n++] = prev;
                } else if (cSym === 17) {
                  var rep17 = (await readBits(3)) + 3;
                  while (rep17-- > 0) codeLens[n++] = 0;
                } else if (cSym === 18) {
                  var rep18 = (await readBits(7)) + 11;
                  while (rep18-- > 0) codeLens[n++] = 0;
                } else {
                  throw new Error('Invalid code length symbol: ' + cSym);
                }
              }
              litTable = buildHuffmanTable(codeLens.subarray(0, hlit), hlit);
              distTable = buildHuffmanTable(codeLens.subarray(hlit, totalCodes), hdist);
            }

            // Tight fast path whenever input has >= 32 bytes & output has room
            var litArr = litTable.table;
            var litMask = (1 << litTable.maxBits) - 1;
            var litMax = litTable.maxBits;
            var distArr = distTable.table;
            var distMask = (1 << distTable.maxBits) - 1;
            var distMax = distTable.maxBits;

            for (;;) {
              var sym;
              if (inLen - inPos >= 16) {
                while (bitCnt < litMax) {
                  bitBuf |= inBuf[inPos++] << bitCnt;
                  bitCnt += 8;
                }
                var lEntry = litArr[bitBuf & litMask];
                var lBits = lEntry >>> 16;
                if (lBits === 0) throw new Error('Invalid literal/length code');
                bitBuf >>>= lBits;
                bitCnt -= lBits;
                sym = lEntry & 0xffff;
              } else {
                sym = await decodeSymbol(litTable);
              }

              if (sym < 256) {
                pushOutputByte(sym);
                if (pendingChunks.length >= 2) {
                  var w1 = pauseIfFull();
                  if (w1) await w1;
                }
              } else if (sym === 256) {
                break;
              } else {
                var lIdx = sym - 257;
                if (lIdx >= 29) throw new Error('Invalid length symbol: ' + sym);
                var copyLen = LEN_BASE[lIdx];
                var lExtra = LEN_EXTRA[lIdx];
                if (lExtra > 0) {
                  if (bitCnt < lExtra && inPos < inLen) {
                    bitBuf |= inBuf[inPos++] << bitCnt;
                    bitCnt += 8;
                  }
                  copyLen += bitCnt >= lExtra
                    ? (function () {
                        var v = bitBuf & ((1 << lExtra) - 1);
                        bitBuf >>>= lExtra;
                        bitCnt -= lExtra;
                        return v;
                      })()
                    : await readBits(lExtra);
                }

                var dSym;
                if (inLen - inPos >= 8) {
                  while (bitCnt < distMax) {
                    bitBuf |= inBuf[inPos++] << bitCnt;
                    bitCnt += 8;
                  }
                  var dEntry = distArr[bitBuf & distMask];
                  var dBits = dEntry >>> 16;
                  if (dBits === 0) throw new Error('Invalid distance code');
                  bitBuf >>>= dBits;
                  bitCnt -= dBits;
                  dSym = dEntry & 0xffff;
                } else {
                  dSym = await decodeSymbol(distTable);
                }

                if (dSym >= 30) throw new Error('Invalid distance symbol: ' + dSym);
                var copyDist = DIST_BASE[dSym];
                var dExtra = DIST_EXTRA[dSym];
                if (dExtra > 0) {
                  copyDist += await readBits(dExtra);
                }

                var srcIdx = (winPos - copyDist) & 32767;
                for (var k = 0; k < copyLen; k++) {
                  var byteVal = windowBuf[srcIdx];
                  srcIdx = (srcIdx + 1) & 32767;
                  pushOutputByte(byteVal);
                }
                if (pendingChunks.length >= 2) {
                  var w2 = pauseIfFull();
                  if (w2) await w2;
                }
              }
            }
          } else {
            throw new Error('Invalid DEFLATE block type: ' + btype);
          }
        }
        flushRemaining();
        finished = true;
      } catch (err) {
        streamErr = err;
        finished = true;
      } finally {
        try {
          if (!eof && reader.cancel) await reader.cancel();
        } catch (e) {
          /* ignore */
        }
      }
    }

    return new ReadableStream({
      start: function () {
        pumping = runInflate();
      },
      pull: async function (controller) {
        while (pendingChunks.length === 0 && !finished) {
          if (resumePump) {
            var wake = resumePump;
            resumePump = null;
            wake();
          }
          await new Promise(function (r) {
            setTimeout(r, 0);
          });
        }
        if (streamErr && pendingChunks.length === 0) {
          throw streamErr;
        }
        if (pendingChunks.length > 0) {
          controller.enqueue(pendingChunks.shift());
          if (resumePump && pendingChunks.length < 2) {
            var wakeNext = resumePump;
            resumePump = null;
            wakeNext();
          }
          return;
        }
        if (pumping) await pumping;
        if (streamErr) throw streamErr;
        controller.close();
      },
      cancel: async function () {
        finished = true;
        if (resumePump) {
          var wake = resumePump;
          resumePump = null;
          wake();
        }
        try {
          await reader.cancel();
        } catch (e) {
          /* ignore */
        }
      },
    });
  }

  /* ------------------------------------------------------------------ *
   * Pull-based disk slice stream over OPFS File / Blob
   * ------------------------------------------------------------------ */

  function readSliceWithRefresh(state, start, end) {
    return state.blob.slice(start, end).arrayBuffer().catch(async function (err) {
      if (!state.reopenBlob) throw err;
      var refreshed = await state.reopenBlob();
      if (!refreshed) throw err;
      state.blob = refreshed;
      return state.blob.slice(start, end).arrayBuffer();
    });
  }

  function createSliceStream(state, start, length, signal) {
    var cursor = start;
    var end = start + length;
    var cancelled = false;

    return new ReadableStream(
      {
        pull: async function (controller) {
          if (cancelled) return;
          if (signal && signal.aborted) {
            cancelled = true;
            throw abortError();
          }
          if (cursor >= end) {
            controller.close();
            return;
          }
          var next = Math.min(end, cursor + READ_CHUNK_BYTES);
          var ab = await readSliceWithRefresh(state, cursor, next);
          cursor = next;
          if (signal && signal.aborted) {
            cancelled = true;
            throw abortError();
          }
          if (ab.byteLength === 0 && cursor < end) {
            throw new Error('Unexpected EOF reading ZIP slice at ' + cursor + '/' + end);
          }
          controller.enqueue(new Uint8Array(ab));
        },
        cancel: function () {
          cancelled = true;
        },
      },
      { highWaterMark: 1 }
    );
  }

  function wrapVerifiedStream(innerStream, expectedSize, signal, label) {
    var reader = innerStream.getReader();
    var emitted = 0;

    return new ReadableStream(
      {
        pull: async function (controller) {
          if (signal && signal.aborted) {
            try { await reader.cancel(); } catch (e) {}
            throw abortError();
          }
          var r = await reader.read();
          if (signal && signal.aborted) {
            try { await reader.cancel(); } catch (e2) {}
            throw abortError();
          }
          if (r.done) {
            if (typeof expectedSize === 'number' && emitted !== expectedSize) {
              throw new Error(
                'Truncated bundle stream for ' + (label || 'entry') + ': ' + emitted + ' != ' + expectedSize
              );
            }
            controller.close();
            return;
          }
          if (r.value && r.value.byteLength > 0) {
            emitted += r.value.byteLength;
            controller.enqueue(r.value);
          }
        },
        cancel: async function (reason) {
          try { await reader.cancel(reason); } catch (e) {}
        },
      },
      { highWaterMark: 1 }
    );
  }

  /* ------------------------------------------------------------------ *
   * ZIP Central Directory parser + archive handle
   * ------------------------------------------------------------------ */

  /**
   * Mount a ZIP File/Blob in place without reading payload bytes into RAM.
   *
   * @param {Blob} blob               OPFS File or Blob holding WebGL.zip
   * @param {object} [opts]
   * @param {function} [opts.reopenBlob] Optional async () => File refresher
   * @param {boolean} [opts.forceJsInflate] Force pure-JS streaming inflate (tests)
   */
  async function open(blob, opts) {
    opts = opts || {};
    if (!blob || typeof blob.slice !== 'function' || typeof blob.size !== 'number') {
      throw new Error('HKAaZip.open requires a File or Blob');
    }
    var fileSize = blob.size;
    if (fileSize < 22) {
      throw new Error('ZIP archive is truncated (' + fileSize + ' bytes)');
    }

    var state = {
      blob: blob,
      reopenBlob: typeof opts.reopenBlob === 'function' ? opts.reopenBlob : null,
      forceJsInflate: !!opts.forceJsInflate,
    };

    // Read at most the final ~65.5 KB to locate End of Central Directory record
    var tailSize = Math.min(fileSize, 65558 + 64);
    var tailStart = fileSize - tailSize;
    var tailAb = await readSliceWithRefresh(state, tailStart, fileSize);
    var tailView = new DataView(tailAb);

    var eocdPos = -1;
    for (var pos = tailAb.byteLength - 22; pos >= 0; pos--) {
      if (tailView.getUint32(pos, true) === EOCD_SIG) {
        var commentLen = tailView.getUint16(pos + 20, true);
        if (pos + 22 + commentLen <= tailAb.byteLength) {
          eocdPos = pos;
          break;
        }
      }
    }
    if (eocdPos < 0) {
      throw new Error('Invalid ZIP archive: End of Central Directory not found');
    }

    var entryCount = tailView.getUint16(eocdPos + 10, true);
    var cdSize = tailView.getUint32(eocdPos + 12, true);
    var cdOffset = tailView.getUint32(eocdPos + 16, true);

    // Support ZIP64 EOCD if archive ever exceeds 4 GB / 65535 entries
    if (
      (entryCount === 0xffff || cdSize === 0xffffffff || cdOffset === 0xffffffff) &&
      eocdPos >= 20 &&
      tailView.getUint32(eocdPos - 20, true) === ZIP64_LOC_SIG
    ) {
      var z64EocdOffset = readUint64LE(tailView, eocdPos - 12);
      var z64Ab = await readSliceWithRefresh(state, z64EocdOffset, z64EocdOffset + 56);
      var z64View = new DataView(z64Ab);
      if (z64View.getUint32(0, true) !== ZIP64_EOCD_SIG) {
        throw new Error('Invalid ZIP64 End of Central Directory');
      }
      entryCount = readUint64LE(z64View, 32);
      cdSize = readUint64LE(z64View, 40);
      cdOffset = readUint64LE(z64View, 48);
    }

    if (cdOffset + cdSize > fileSize) {
      throw new Error('Corrupt ZIP Central Directory bounds');
    }

    var cdAb = await readSliceWithRefresh(state, cdOffset, cdOffset + cdSize);
    var cdBytes = new Uint8Array(cdAb);
    var cdView = new DataView(cdAb);

    var entriesByName = new Map();
    var entriesList = [];
    var totalCompressed = 0;
    var totalUncompressed = 0;
    var p = 0;

    for (var i = 0; i < entryCount; i++) {
      if (p + 46 > cdBytes.byteLength || cdView.getUint32(p, true) !== CD_SIG) {
        throw new Error('Corrupt ZIP Central Directory entry #' + i);
      }
      var flags = cdView.getUint16(p + 8, true);
      var method = cdView.getUint16(p + 10, true);
      var crc = cdView.getUint32(p + 16, true);
      var csize = cdView.getUint32(p + 20, true);
      var usize = cdView.getUint32(p + 24, true);
      var nlen = cdView.getUint16(p + 28, true);
      var elen = cdView.getUint16(p + 30, true);
      var clen = cdView.getUint16(p + 32, true);
      var lho = cdView.getUint32(p + 42, true);

      var nameStart = p + 46;
      var extraStart = nameStart + nlen;
      var nextEntry = extraStart + elen + clen;
      if (nextEntry > cdBytes.byteLength) {
        throw new Error('Truncated ZIP Central Directory entry #' + i);
      }

      // Parse ZIP64 extra field (0x0001) if present
      if (usize === 0xffffffff || csize === 0xffffffff || lho === 0xffffffff) {
        var ep = extraStart;
        var extraEnd = extraStart + elen;
        while (ep + 4 <= extraEnd) {
          var headerId = cdView.getUint16(ep, true);
          var dataSize = cdView.getUint16(ep + 2, true);
          var fp = ep + 4;
          if (headerId === 0x0001) {
            if (usize === 0xffffffff && fp + 8 <= ep + 4 + dataSize) {
              usize = readUint64LE(cdView, fp);
              fp += 8;
            }
            if (csize === 0xffffffff && fp + 8 <= ep + 4 + dataSize) {
              csize = readUint64LE(cdView, fp);
              fp += 8;
            }
            if (lho === 0xffffffff && fp + 8 <= ep + 4 + dataSize) {
              lho = readUint64LE(cdView, fp);
              fp += 8;
            }
            break;
          }
          ep += 4 + dataSize;
        }
      }

      var rawName = decodeUtf8(cdBytes.subarray(nameStart, extraStart));
      p = nextEntry;

      // Skip directory entries
      if (!rawName || rawName.charAt(rawName.length - 1) === '/') continue;
      if (method !== 0 && method !== 8) {
        throw new Error('Unsupported ZIP compression method ' + method + ' for ' + rawName);
      }

      // Eagerly verify the 30-byte Local File Header so dataStart is known in O(1)
      // and any corrupt OPFS file is caught immediately during mount.
      var lfhAb = await readSliceWithRefresh(state, lho, lho + 30);
      if (lfhAb.byteLength < 30) {
        throw new Error('Truncated Local File Header for ' + rawName);
      }
      var lfhView = new DataView(lfhAb);
      if (lfhView.getUint32(0, true) !== LFH_SIG) {
        throw new Error('Bad Local File Header signature for ' + rawName);
      }
      var lfhNameLen = lfhView.getUint16(26, true);
      var lfhExtraLen = lfhView.getUint16(28, true);
      var dataStart = lho + 30 + lfhNameLen + lfhExtraLen;
      if (dataStart + csize > fileSize) {
        throw new Error('Truncated compressed payload for ' + rawName);
      }

      var baseName = rawName.split('/').pop();
      var entry = {
        name: rawName,
        baseName: baseName,
        flags: flags,
        method: method,
        crc: crc,
        csize: csize,
        usize: usize,
        lho: lho,
        dataStart: dataStart,
      };
      entriesList.push(entry);
      entriesByName.set(rawName, entry);
      if (baseName !== rawName) entriesByName.set(baseName, entry);
      totalCompressed += csize;
      totalUncompressed += usize;
    }

    var useNativeDeflateRaw = state.forceJsInflate ? false : await supportsNativeDeflateRaw();

    function lookup(nameOrUrl) {
      if (!nameOrUrl) return null;
      var direct = entriesByName.get(nameOrUrl);
      if (direct) return direct;
      var clean = String(nameOrUrl).split('?')[0].split('#')[0];
      var last = clean.split('/').pop();
      if (entriesByName.has(last)) return entriesByName.get(last);
      try {
        var decoded = decodeURIComponent(last);
        if (entriesByName.has(decoded)) return entriesByName.get(decoded);
      } catch (e) {
        /* ignore */
      }
      return null;
    }

    function openEntryStream(nameOrUrl, signal) {
      var entry = lookup(nameOrUrl);
      if (!entry) return null;
      var rawStream = createSliceStream(state, entry.dataStart, entry.csize, signal);
      var outStream;
      if (entry.method === 0) {
        outStream = rawStream;
      } else if (useNativeDeflateRaw) {
        outStream = rawStream.pipeThrough(new DecompressionStream('deflate-raw'));
      } else {
        outStream = createJsRawInflateStream(rawStream);
      }
      return wrapVerifiedStream(outStream, entry.usize, signal, entry.baseName);
    }

    function openResponse(nameOrUrl, optsOrMethod) {
      var entry = lookup(nameOrUrl);
      if (!entry) return null;
      var method = 'GET';
      var signal = undefined;
      if (typeof optsOrMethod === 'string') {
        method = optsOrMethod.toUpperCase();
      } else if (optsOrMethod && typeof optsOrMethod === 'object') {
        if (optsOrMethod.method) method = String(optsOrMethod.method).toUpperCase();
        signal = optsOrMethod.signal;
      }
      var headers = {
        'Content-Type': 'application/octet-stream',
        'Content-Length': String(entry.usize),
      };
      if (method === 'HEAD') {
        return new Response(null, { status: 200, statusText: 'OK', headers: headers });
      }
      var stream = openEntryStream(entry.baseName, signal);
      return new Response(stream, { status: 200, statusText: 'OK', headers: headers });
    }

    return {
      size: fileSize,
      count: entriesList.length,
      totalCompressed: totalCompressed,
      totalUncompressed: totalUncompressed,
      engine: useNativeDeflateRaw ? 'DecompressionStream(deflate-raw)' : 'js-streaming-inflate',
      entries: entriesList,
      has: function (nameOrUrl) {
        return !!lookup(nameOrUrl);
      },
      get: lookup,
      openEntryStream: openEntryStream,
      openResponse: openResponse,
    };
  }

  return {
    VERSION: '1.0.0',
    open: open,
    supportsNativeDeflateRaw: supportsNativeDeflateRaw,
    _internals: {
      createJsRawInflateStream: createJsRawInflateStream,
    },
  };
});
