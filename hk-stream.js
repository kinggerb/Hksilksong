/* ============================================================================
 * hk-stream.js — low-memory asset loader for the Hollow Knight web port
 * ----------------------------------------------------------------------------
 * The problem
 *   The game ships as 47 chunk files (Build/hktruffled.data.part1..45 and
 *   Build/hktruffled.wasm.part1..2 = ~930 MB). The original page glued the
 *   chunks together in RAM:
 *
 *       45 x new Blob(chunks)      ~ 930 MB
 *       fetch(blobUrl).blob()      ~ 930 MB more
 *       new Blob([...blobs])       ~ 930 MB more
 *       Unity's dataUrl fetch      ~ 930 MB more   ==> ~3.7 GB peak
 *
 *   iPadOS gives a Safari tab roughly 1.5-2.5 GB before the WebContent process
 *   is jetsam-killed, so the tab died exactly when the progress bar reached
 *   ~100 % ("loads most of the MBs then crashes").
 *
 * What this module does instead
 *   1. stream every part straight to disk (OPFS) through a worker, so the
 *      download phase only ever uses a few MB of RAM;
 *   2. keep the merged file between sessions, so a reload (or a crash + retry)
 *      skips the ~930 MB download completely;
 *   3. hand Unity ONE file-backed blob URL, so its ~930 MB ArrayBuffer is the
 *      only big allocation left in the tab.
 *
 * Network + storage are injectable so this file is unit tested in Node
 * (see tools/test-hk-stream.mjs).
 * ==========================================================================*/

(function (root, factory) {
  if (typeof module === 'object' && module.exports) {
    module.exports = factory();
  } else {
    root.HKStream = factory();
  }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var DEFAULT_DIR = 'hk-webgl-cache';
  var FLUSH_EVERY = 32 * 1024 * 1024; // flush OPFS about every 32 MB
  var MAX_RETRIES = 4; // per part (positional sinks only)
  var SEQ_RETRIES = 3; // whole-pass retries for non-positional sinks
  var MAX_WRITES_IN_FLIGHT = 8;
  var WRITE_TIMEOUT = 180000; // ms per worker write
  var OPEN_TIMEOUT = 60000; // ms for open/stat/marker calls
  var PROGRESS_THROTTLE = 120; // ms

  function noop() {}
  function now() {
    return typeof performance !== 'undefined' && performance.now ? performance.now() : Date.now();
  }
  function defaultFetch(url, opts) {
    return fetch(url, opts);
  }
  function mb(bytes) {
    return (bytes / 1048576).toFixed(1) + ' MB';
  }
  function makeLogger(log) {
    return function () {
      if (!log) return;
      try {
        log.apply(null, arguments);
      } catch (e) {
        /* logging must never break the loader */
      }
    };
  }
  function abortError() {
    var e = new Error('aborted');
    e.name = 'AbortError';
    return e;
  }
  function sleep(ms, signal) {
    return new Promise(function (resolve, reject) {
      if (signal && signal.aborted) return reject(abortError());
      var timer = setTimeout(done, ms);
      function done() {
        if (signal) signal.removeEventListener('abort', onAbort);
        resolve();
      }
      function onAbort() {
        clearTimeout(timer);
        reject(abortError());
      }
      if (signal) signal.addEventListener('abort', onAbort, { once: true });
    });
  }

  /* ------------------------------------------------------------------ *
   * Capability detection
   * ------------------------------------------------------------------ */

  var capabilityPromise = null;

  function detect() {
    if (capabilityPromise) return capabilityPromise;
    capabilityPromise = (async function () {
      var caps = {
        opfs: false,
        opfsError: null,
        quota: 0,
        usage: 0,
        available: 0,
        secure: false,
        workers: typeof Worker !== 'undefined',
      };
      try {
        caps.secure = typeof isSecureContext === 'undefined' ? location.protocol === 'https:' : isSecureContext;
      } catch (e) {
        caps.secure = false;
      }
      try {
        if (typeof navigator !== 'undefined' && navigator.storage && navigator.storage.getDirectory) {
          caps.opfs = !!(await navigator.storage.getDirectory());
        }
      } catch (e) {
        caps.opfs = false;
        caps.opfsError = String((e && e.message) || e);
      }
      try {
        if (typeof navigator !== 'undefined' && navigator.storage && navigator.storage.estimate) {
          var est = (await navigator.storage.estimate()) || null;
          caps.quota = (est && est.quota) || 0;
          caps.usage = (est && est.usage) || 0;
          caps.available = caps.quota ? Math.max(0, caps.quota - caps.usage) : 0;
        }
      } catch (e) {
        /* estimate() is only a hint */
      }
      return caps;
    })();
    return capabilityPromise;
  }

  function requestPersistence() {
    try {
      if (typeof navigator !== 'undefined' && navigator.storage && navigator.storage.persist) {
        return Promise.resolve(navigator.storage.persist()).catch(function () {
          return false;
        });
      }
    } catch (e) {
      /* ignore */
    }
    return Promise.resolve(false);
  }

  /* ------------------------------------------------------------------ *
   * Sinks
   *   sink.open()                -> {size, canResume}
   *   sink.write(u8, at, flush)  -> void          (positional if !sequentialOnly)
   *   sink.finish()              -> {size, url?}
   *   sink.abort()               -> void
   * ------------------------------------------------------------------ */

  /* ---- OPFS through a worker using FileSystemSyncAccessHandle -------- */

  function WorkerSink(opts) {
    this.dir = opts.dirName || DEFAULT_DIR;
    this.name = opts.name;
    this.expected = opts.expectedSize || 0;
    this.workerUrl = opts.workerUrl;
    this.log = makeLogger(opts.log);
    this.seq = 0;
    this.pending = new Map();
    this.worker = null;
    this.sequentialOnly = false; // sync access handles support random access
  }

  WorkerSink.prototype.connect = function () {
    var self = this;
    if (this.worker) return Promise.resolve(this);
    return new Promise(function (resolve, reject) {
      var worker;
      try {
        worker = new Worker(self.workerUrl);
      } catch (e) {
        return reject(e);
      }
      self.worker = worker;
      worker.onmessage = function (ev) {
        var msg = ev.data || {};
        var entry = self.pending.get(msg.id);
        if (!entry) return;
        self.pending.delete(msg.id);
        clearTimeout(entry.timer);
        if (msg.error) {
          var err = new Error(msg.error.message || 'writer worker error');
          err.name = msg.error.name || 'WorkerError';
          err.op = msg.error.op;
          entry.reject(err);
        } else {
          entry.resolve(msg);
        }
      };
      worker.onerror = function (ev) {
        self.failAll(new Error('writer worker failed: ' + ((ev && ev.message) || 'unknown error')));
      };
      worker.onmessageerror = function () {
        self.failAll(new Error('writer worker could not decode a message'));
      };
      resolve(self);
    });
  };

  WorkerSink.prototype.failAll = function (err) {
    this.pending.forEach(function (entry) {
      clearTimeout(entry.timer);
      entry.reject(err);
    });
    this.pending.clear();
  };

  WorkerSink.prototype.call = function (msg, timeout, transfer) {
    var self = this;
    if (!this.worker) return Promise.reject(new Error('writer worker is not connected'));
    return new Promise(function (resolve, reject) {
      var id = ++self.seq;
      var timer = setTimeout(function () {
        self.pending.delete(id);
        reject(new Error('writer worker timed out on "' + msg.type + '"'));
      }, timeout || WRITE_TIMEOUT);
      self.pending.set(id, { resolve: resolve, reject: reject, timer: timer });
      msg.id = id;
      try {
        self.worker.postMessage(msg, transfer || []);
      } catch (e) {
        clearTimeout(timer);
        self.pending.delete(id);
        reject(e);
      }
    });
  };

  WorkerSink.prototype.open = async function (resume) {
    await this.connect();
    var res = await this.call(
      { type: 'open', dir: this.dir, name: this.name, expected: this.expected, resume: !!resume },
      OPEN_TIMEOUT
    );
    this.size = res.size || 0;
    return { size: this.size, canResume: !!res.canResume };
  };

  WorkerSink.prototype.write = async function (u8, at, flush) {
    // Transfer the buffer so the chunk is moved (not copied) to the worker.
    var buf = u8.byteOffset === 0 && u8.byteLength === u8.buffer.byteLength ? u8 : u8.slice();
    await this.call({ type: 'write', data: buf.buffer, at: at, flush: !!flush }, WRITE_TIMEOUT, [buf.buffer]);
  };

  WorkerSink.prototype.finish = async function () {
    var res = await this.call({ type: 'finish' }, OPEN_TIMEOUT);
    return { size: res.size };
  };

  WorkerSink.prototype.abort = async function () {
    try {
      if (this.worker) await this.call({ type: 'quit' }, 5000);
    } catch (e) {
      /* ignore */
    }
    this.destroy();
  };

  WorkerSink.prototype.destroy = function () {
    this.failAll(new Error('writer worker closed'));
    if (this.worker) {
      try {
        this.worker.terminate();
      } catch (e) {
        /* ignore */
      }
      this.worker = null;
    }
  };

  WorkerSink.prototype.stat = async function (name) {
    await this.connect();
    var res = await this.call({ type: 'stat', dir: this.dir, name: name }, OPEN_TIMEOUT);
    return typeof res.size === 'number' ? res.size : -1;
  };
  WorkerSink.prototype.putText = async function (name, text) {
    await this.connect();
    return this.call({ type: 'putText', dir: this.dir, name: name, text: text }, OPEN_TIMEOUT);
  };
  WorkerSink.prototype.getText = async function (name) {
    await this.connect();
    var res = await this.call({ type: 'getText', dir: this.dir, name: name }, OPEN_TIMEOUT);
    return res.text === undefined ? null : res.text;
  };
  WorkerSink.prototype.remove = async function (name) {
    await this.connect();
    return this.call({ type: 'remove', dir: this.dir, name: name }, OPEN_TIMEOUT);
  };

  /* ---- OPFS on the main thread via createWritable (Safari 26+) ------- */

  function DirectOpfsSink(opts) {
    this.dirName = opts.dirName || DEFAULT_DIR;
    this.name = opts.name;
    this.handle = null;
    this.writable = null;
    this.sequentialOnly = true; // no seeking: writes must arrive in order
  }

  DirectOpfsSink.prototype.open = async function () {
    var root = await navigator.storage.getDirectory();
    var dir = await root.getDirectoryHandle(this.dirName, { create: true });
    this.handle = await dir.getFileHandle(this.name, { create: true });
    this.writable = await this.handle.createWritable(); // truncates to zero
    return { size: 0, canResume: false };
  };

  DirectOpfsSink.prototype.write = async function (u8) {
    await this.writable.write(u8);
  };

  DirectOpfsSink.prototype.finish = async function () {
    await this.writable.close();
    this.writable = null;
    var f = await this.handle.getFile();
    return { size: f.size };
  };

  DirectOpfsSink.prototype.abort = async function () {
    try {
      if (this.writable) await this.writable.abort();
    } catch (e) {
      /* ignore */
    }
    this.writable = null;
  };

  /* ---- Plain RAM (last resort) -------------------------------------- */

  function MemorySink(opts) {
    this.expected = opts.expectedSize || 0;
    this.mime = opts.mime || 'application/octet-stream';
    this.buffer = null;
    this.url = null;
    this.sequentialOnly = true;
  }

  MemorySink.prototype.open = function () {
    if (!this.buffer && this.expected > 0) this.buffer = new Uint8Array(this.expected);
    return Promise.resolve({ size: 0, canResume: false });
  };

  MemorySink.prototype.write = function (u8, at) {
    if (!this.buffer) this.buffer = new Uint8Array(at + u8.byteLength);
    if (at + u8.byteLength > this.buffer.length) {
      var bigger = new Uint8Array(at + u8.byteLength);
      bigger.set(this.buffer.subarray(0, Math.min(at, this.buffer.length)));
      this.buffer = bigger;
    }
    this.buffer.set(u8, at);
    return Promise.resolve();
  };

  MemorySink.prototype.finish = function () {
    var blob = new Blob([this.buffer], { type: this.mime });
    this.url = URL.createObjectURL(blob);
    return Promise.resolve({ size: this.buffer.byteLength, url: this.url });
  };

  MemorySink.prototype.abort = function () {
    if (this.url) {
      try {
        URL.revokeObjectURL(this.url);
      } catch (e) {
        /* ignore */
      }
      this.url = null;
    }
    this.buffer = null;
    return Promise.resolve();
  };

  /* ------------------------------------------------------------------ *
   * Download driver
   * ------------------------------------------------------------------ */

  /**
   * One attempt at streaming a part into the sink.
   * p = {url, bytes, index, sink, partOffset, skip, signal, fetchImpl, progress, onAdvance}
   */
  async function fetchPartOnce(p) {
    var res = await p.fetchImpl(p.url, { cache: 'default', signal: p.signal });
    if (!res.ok) throw new Error('HTTP ' + res.status + ' for ' + p.url);
    if (!res.body) throw new Error('no readable body for ' + p.url);

    var reader = res.body.getReader();
    var received = 0; // bytes pulled off the wire for this part (incl. skipped prefix)
    var written = 0; // bytes this attempt produced for the file
    var inFlight = [];
    var lastFlushAt = 0;
    var failed = null;

    p.progress.written = 0;
    p.onAdvance();

    try {
      for (;;) {
        var chunk = await reader.read();
        if (chunk.done) break;
        var value = chunk.value;
        if (!value || !value.byteLength) continue;

        var start = received; // offset of this chunk inside the part
        received += value.byteLength;

        if (received <= p.skip) {
          p.onAdvance();
          continue;
        }

        var usable = start >= p.skip ? value : value.subarray(p.skip - start);
        var at = p.partOffset + written; // `written` = bytes before this chunk
        written += usable.byteLength;
        var flush = written - lastFlushAt >= FLUSH_EVERY;
        if (flush) lastFlushAt = written;

        p.progress.written = written;
        p.onAdvance();

        inFlight.push(p.sink.write(usable, at, flush));
        if (inFlight.length >= MAX_WRITES_IN_FLIGHT) {
          await Promise.all(inFlight);
          inFlight = [];
        }
      }
      await Promise.all(inFlight);
    } catch (err) {
      failed = err;
    } finally {
      if (reader.cancel) {
        try {
          await reader.cancel();
        } catch (e) {
          /* ignore */
        }
      }
    }

    if (failed) {
      // Roll the progress back so a retry does not double count.
      p.progress.written = 0;
      p.onAdvance();
      throw failed;
    }

    var want = p.bytes - p.skip;
    if (written !== want) {
      p.progress.written = 0;
      p.onAdvance();
      throw new Error('part ' + p.index + ' ended early: ' + written + '/' + want + ' bytes');
    }
  }

  async function fetchPartTo(p) {
    var log = makeLogger(p.log);
    var attempts = p.sink.sequentialOnly ? 0 : MAX_RETRIES;

    for (var attempt = 0; ; attempt++) {
      if (p.signal && p.signal.aborted) throw abortError();
      try {
        await fetchPartOnce(p);
        return;
      } catch (err) {
        if (err && err.name === 'AbortError') throw err;
        if (attempt >= attempts) throw err;
        var wait = Math.min(8000, 400 * Math.pow(2, attempt));
        log(
          'part ' +
            p.index +
            ' failed (' +
            ((err && err.message) || err) +
            '), retrying in ' +
            wait +
            'ms (attempt ' +
            (attempt + 2) +
            '/' +
            (attempts + 1) +
            ')'
        );
        await sleep(wait, p.signal);
      }
    }
  }

  /**
   * Stream every part into `sink`; `startOffset` is what is already on disk.
   */
  async function streamAll(opts) {
    var parts = opts.parts;
    var sink = opts.sink;
    var fetchImpl = opts.fetchImpl || defaultFetch;
    var log = makeLogger(opts.log);

    var total = 0;
    var i;
    for (i = 0; i < parts.length; i++) {
      parts[i].bytes = parts[i].bytes || 0;
      parts[i].offset = total;
      total += parts[i].bytes;
    }

    var passes = sink.sequentialOnly ? SEQ_RETRIES : 1;
    var lastErr = null;

    for (var pass = 0; pass < passes; pass++) {
      var startOffset = pass === 0 ? opts.startOffset || 0 : 0;
      try {
        if (pass > 0) {
          log('restarting the download from the beginning (attempt ' + (pass + 1) + '/' + passes + ')');
          var reopened = await sink.open(false);
          startOffset = reopened.canResume ? reopened.size : 0;
        }

        var accounted = 0;
        var lastReport = 0;
        var currentPart = 0;

        function report(force) {
          if (!opts.onProgress) return;
          var t = now();
          if (!force && t - lastReport < PROGRESS_THROTTLE) return;
          lastReport = t;
          opts.onProgress({
            loaded: Math.min(accounted, total),
            total: total,
            part: currentPart,
            parts: parts.length,
            pass: pass + 1,
          });
        }

        function makeAdvance(prog, base) {
          return function () {
            accounted = base + prog.written;
            report(false);
          };
        }

        for (i = 0; i < parts.length; i++) {
          var p = parts[i];
          currentPart = i + 1;

          if (p.offset + p.bytes <= startOffset) {
            accounted = p.offset + p.bytes;
            report(true);
            continue;
          }

          var skip = Math.max(0, startOffset - p.offset);
          var progress = { written: 0 };
          var baseForPart = p.offset + skip;
          var onAdvance = makeAdvance(progress, baseForPart);

          await fetchPartTo({
            url: p.url,
            bytes: p.bytes,
            index: currentPart,
            sink: sink,
            partOffset: p.offset,
            skip: skip,
            signal: opts.signal,
            fetchImpl: fetchImpl,
            log: opts.log,
            progress: progress,
            onAdvance: onAdvance,
          });

          accounted = p.offset + p.bytes;
          report(true);
        }

        report(true);
        return { total: total };
      } catch (err) {
        if (err && err.name === 'AbortError') throw err;
        lastErr = err;
        if (pass + 1 >= passes) break;
        await sleep(1000, opts.signal);
      }
    }

    throw lastErr || new Error('download failed');
  }

  /* ------------------------------------------------------------------ *
   * High level: make sure a merged, disk-backed dataset exists
   * ------------------------------------------------------------------ */

  function wrapBlob(file, name, mime) {
    // Re-wrapping keeps the disk-backed reference (no copy) and gives the blob
    // a content type: Unity needs application/wasm to *stream* compile it.
    try {
      return new File([file], name, { type: mime });
    } catch (e) {
      return new Blob([file], { type: mime });
    }
  }

  async function safeCall(fn) {
    try {
      return await fn();
    } catch (e) {
      return null;
    }
  }

  async function getFileFromOpfs(dirName, name) {
    try {
      var root = await navigator.storage.getDirectory();
      var dir = await root.getDirectoryHandle(dirName, { create: true });
      var fh = await dir.getFileHandle(name, { create: false });
      return await fh.getFile();
    } catch (e) {
      return null;
    }
  }

  /**
   * @param {object} opts
   *   parts, expectedSize, cacheKey, buildId, dirName, mime,
   *   onProgress, onStatus, onLog, signal, fetchImpl, workerUrl,
   *   writerFactory (tests), allowMemoryFallback (default true)
   * @returns {Promise<{url,size,kind,file,dispose}>}
   */
  async function ensureDataset(opts) {
    var log = makeLogger(opts.onLog);
    var dirName = opts.dirName || DEFAULT_DIR;
    var expectedSize = opts.expectedSize || 0;
    var caps = opts.caps || (await detect());
    var sink = null;

    if (opts.preferDisk !== false && caps.opfs) {
      try {
        sink = opts.writerFactory
          ? await opts.writerFactory({ dirName: dirName, name: opts.cacheKey, expectedSize: expectedSize })
          : await openOpfsSink(opts, caps, dirName, expectedSize);
      } catch (err) {
        log('could not open OPFS writer: ' + ((err && err.message) || err));
        sink = null;
      }
    }

    if (sink) {
      try {
        return await runDiskSession(opts, sink, dirName, expectedSize, log);
      } catch (err) {
        if (err && err.name === 'AbortError') throw err;
        // Setup problems (no sync handle / blocked OPFS) can fall back to RAM;
        // a failed download must not silently retry in RAM.
        if (err && err.setupFailure && opts.allowMemoryFallback !== false) {
          log('disk cache unavailable: ' + err.message + ' — falling back to memory');
          if (opts.onStatus) opts.onStatus('disk cache unavailable, loading into memory');
          try {
            await sink.abort();
          } catch (e) {
            /* ignore */
          }
        } else {
          throw err;
        }
      }
    } else if (opts.allowMemoryFallback === false) {
      throw new Error('no writable storage available for the game data');
    }

    /* ---- RAM fallback ------------------------------------------------ */
    var memSink = new MemorySink({
      expectedSize: expectedSize,
      mime: opts.mime,
    });
    await memSink.open();
    if (opts.onStatus) opts.onStatus('loading into memory (no disk cache)');
    await streamAll({
      parts: opts.parts,
      sink: memSink,
      startOffset: 0,
      onProgress: opts.onProgress,
      fetchImpl: opts.fetchImpl,
      signal: opts.signal,
      log: opts.onLog,
    });
    var memDone = await memSink.finish();
    if (expectedSize && memDone.size !== expectedSize) {
      throw new Error('assembled size mismatch: ' + memDone.size + ' != ' + expectedSize);
    }
    return finish({
      kind: 'memory',
      url: memDone.url,
      size: memDone.size,
      file: null,
      sink: memSink,
    });
  }

  async function openOpfsSink(opts, caps, dirName, expectedSize) {
    var base = {
      dirName: dirName,
      name: opts.cacheKey,
      expectedSize: expectedSize,
      log: opts.onLog,
    };
    if (caps.workers && typeof Worker !== 'undefined' && opts.workerUrl) {
      // Probe with a throw-away file so a partially downloaded cache is never
      // touched, and so we know OPFS + sync access handles really work here.
      var probeName = opts.cacheKey + '.probe';
      var probe = new WorkerSink(
        Object.assign({ workerUrl: opts.workerUrl, name: probeName, expectedSize: 0 }, base)
      );
      try {
        await probe.open(false);
        await probe.remove(probeName);
        await probe.abort();
        return new WorkerSink(Object.assign({ workerUrl: opts.workerUrl }, base));
      } catch (e) {
        probe.destroy();
        opts.onLog && opts.onLog('writer worker unavailable: ' + ((e && e.message) || e));
      }
    }
    var root = await navigator.storage.getDirectory();
    var dir = await root.getDirectoryHandle(dirName, { create: true });
    var handle = await dir.getFileHandle(opts.cacheKey, { create: true });
    if (typeof handle.createWritable !== 'function') {
      throw new Error('cannot write to the origin private file system on this browser');
    }
    return new DirectOpfsSink(base);
  }

  async function runDiskSession(opts, sink, dirName, expectedSize, log) {
    function setup(msg) {
      var e = new Error(msg);
      e.setupFailure = true;
      return e;
    }

    if (opts.onStatus) opts.onStatus('checking local cache');
    var known = await safeCall(function () {
      return sink.stat(opts.cacheKey);
    });
    if (typeof known !== 'number') known = -1;

    // `.ok`       = written after the whole file was verified  (complete)
    // `.building` = written before the first byte of a download (partial)
    var done = await safeCall(function () {
      return sink.getText(opts.cacheKey + '.ok');
    });
    var building = await safeCall(function () {
      return sink.getText(opts.cacheKey + '.building');
    });

    var doneMatchesBuild = typeof done === 'string' && done.indexOf(opts.buildId) === 0;
    var partialMatchesBuild = typeof building === 'string' && building.indexOf(opts.buildId) === 0;

    var cacheValid = expectedSize > 0 && known === expectedSize && doneMatchesBuild;

    // A file of exactly the expected size can only exist if every byte was
    // written (writes are issued strictly in order), so it is usable even when
    // the tab died between the final write and the completion marker — but
    // only if no *other* build left a marker behind.
    var completeButUnmarked = expectedSize > 0 && known === expectedSize && done === null && !cacheValid;
    var staleBuild = done !== null && !doneMatchesBuild;

    if (cacheValid || completeButUnmarked) {
      var cachedFile = await getFileFromOpfs(dirName, opts.cacheKey);
      if (cachedFile && cachedFile.size === expectedSize) {
        if (completeButUnmarked) {
          log('found a complete file without a completion marker — repairing it');
          await safeCall(function () {
            return sink.putText(opts.cacheKey + '.ok', opts.buildId + ' ' + new Date().toISOString());
          });
        }
        log('cache hit: ' + opts.cacheKey + ' (' + mb(expectedSize) + ' already on disk)');
        return finish({
          kind: 'opfs-cached',
          url: URL.createObjectURL(wrapBlob(cachedFile, opts.cacheKey, opts.mime)),
          size: cachedFile.size,
          file: cachedFile,
          sink: sink,
        });
      }
      log('cache looked complete but the file could not be read back, rebuilding');
    }

    if (staleBuild) log('cached data belongs to another build id — starting over');

    var resumeAt = 0;
    if (
      expectedSize > 0 &&
      known > 0 &&
      known < expectedSize &&
      partialMatchesBuild &&
      !staleBuild
    ) {
      resumeAt = known; // same build, partially written
    }

    if (!resumeAt) {
      // Remember which build this partial file belongs to, so a killed session
      // can be resumed instead of re-downloaded.
      await safeCall(function () {
        return sink.putText(opts.cacheKey + '.building', opts.buildId);
      });
      if (staleBuild) {
        await safeCall(function () {
          return sink.remove(opts.cacheKey + '.ok');
        });
      }
    }

    var opened;
    try {
      opened = await sink.open(resumeAt > 0);
    } catch (err) {
      throw setup('could not open the cache file: ' + ((err && err.message) || err));
    }

    var startOffset = opened.canResume ? opened.size : 0;
    if (startOffset > 0) {
      log('resuming download at ' + mb(startOffset));
      if (opts.onStatus) opts.onStatus('resuming download — ' + mb(startOffset) + ' already saved');
    } else {
      if (opts.onStatus) opts.onStatus('downloading game data');
    }

    await streamAll({
      parts: opts.parts,
      sink: sink,
      startOffset: startOffset,
      onProgress: opts.onProgress,
      fetchImpl: opts.fetchImpl,
      signal: opts.signal,
      log: opts.onLog,
    });

    var done = await sink.finish();
    if (expectedSize && done.size !== expectedSize) {
      // Something is wrong with the file on disk: throw it away so the next
      // attempt starts from zero instead of failing the same way.
      await safeCall(function () {
        return sink.remove(opts.cacheKey + '.ok');
      });
      await safeCall(function () {
        return sink.remove(opts.cacheKey + '.building');
      });
      if (resumeAt) {
        // Only delete the data file when we did not simply resume into it.
        await safeCall(function () {
          return sink.remove(opts.cacheKey);
        });
      }
      throw new Error(
        'saved file is ' + done.size + ' bytes, expected ' + expectedSize + ' — the cache was cleared, try again'
      );
    }
    try {
      await sink.putText(opts.cacheKey + '.ok', opts.buildId + ' ' + new Date().toISOString());
    } catch (e) {
      log('could not write the cache marker: ' + ((e && e.message) || e));
    }

    var file = await getFileFromOpfs(dirName, opts.cacheKey);
    if (!file) throw new Error('assembled file could not be re-opened');
    log('saved ' + mb(done.size) + ' to OPFS as ' + opts.cacheKey);
    return finish({
      kind: 'opfs',
      url: URL.createObjectURL(wrapBlob(file, opts.cacheKey, opts.mime)),
      size: file.size,
      file: file,
      sink: sink,
    });
  }

  function finish(dataset) {
    dataset.dispose = function () {
      if (dataset.url) {
        try {
          URL.revokeObjectURL(dataset.url);
        } catch (e) {
          /* ignore */
        }
      }
      if (dataset.sink && dataset.sink.destroy) dataset.sink.destroy();
    };
    return dataset;
  }

  async function clearCache(dirName) {
    try {
      var root = await navigator.storage.getDirectory();
      await root.removeEntry(dirName || DEFAULT_DIR, { recursive: true });
      return true;
    } catch (e) {
      return false;
    }
  }

  async function cacheInfo(dirName) {
    var out = { entries: [], bytes: 0 };
    try {
      var root = await navigator.storage.getDirectory();
      var dir = await root.getDirectoryHandle(dirName || DEFAULT_DIR, { create: false });
      if (dir.entries) {
        for await (var pair of dir.entries()) {
          var size = 0;
          if (pair[1].kind === 'file') {
            try {
              size = (await pair[1].getFile()).size;
            } catch (e) {
              size = 0;
            }
          }
          out.entries.push({ name: pair[0], size: size });
          out.bytes += size;
        }
      }
    } catch (e) {
      out.error = String((e && e.message) || e);
    }
    return out;
  }

  return {
    VERSION: '1.1.0',
    DEFAULT_DIR: DEFAULT_DIR,
    detect: detect,
    requestPersistence: requestPersistence,
    ensureDataset: ensureDataset,
    clearCache: clearCache,
    cacheInfo: cacheInfo,
    mb: mb,
    _internals: {
      WorkerSink: WorkerSink,
      DirectOpfsSink: DirectOpfsSink,
      MemorySink: MemorySink,
      streamAll: streamAll,
      fetchPartOnce: fetchPartOnce,
      fetchPartTo: fetchPartTo,
      sleep: sleep,
    },
  };
});
