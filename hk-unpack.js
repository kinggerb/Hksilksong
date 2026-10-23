/* hk-unpack.js — turn one compressed bundle from the .hkar container into a
 * usable response.
 *
 * The container stores each bundle as a single xz stream holding a UnityFS
 * bundle whose blocks are stored uncompressed (see tools/build_hkar.py).  This
 * worker slices those bytes straight out of the on-disk container - nothing is
 * read on the main thread - and decodes them with liblzma compiled to wasm.
 *
 * Runs off the main thread so a 360 MB bundle never freezes the page.
 *
 * in : {id, file, off, comp, raw, name, cacheName}
 * out: {id, ok:true, size, cached:true}
 *    | {id, ok:true, size, cached:false, buf}      (buf transferred)
 *    | {id, ok:false, error}
 *
 * If the Cache API refuses the entry (quota) the decoded bytes are handed
 * back instead, so a full disk degrades to "slower next time", not "broken".
 */
/* global XzReadableStream */
importScripts('xz-decompress.min.js');

var Xz = self['xz-decompress'] || {};
var XzReadableStream = Xz.XzReadableStream;

self.onmessage = function (e) {
  var job = e.data || {};
  handle(job).then(
    function (out) {
      var msg = { id: job.id, ok: true, size: out.size, cached: out.cached };
      if (out.buf) msg.buf = out.buf; // transferred, not copied
      self.postMessage(msg, out.buf ? [out.buf] : []);
    },
    function (err) {
      self.postMessage({ id: job.id, ok: false, error: String((err && err.message) || err) });
    }
  );
};

async function handle(job) {
  if (!XzReadableStream) throw new Error('xz decoder failed to load');

  var headers = {
    'Content-Type': 'application/octet-stream',
    'X-HK-Bundle': job.name,
  };

  // First choice: stream the decoded bundle straight into the Cache API.  On
  // iOS the big bundles are 200-500 MB, and this never holds one in the JS
  // heap - liblzma fills the stream and the cache pulls it a chunk at a time.
  try {
    var cache = await caches.open(job.cacheName);
    await cache.put(job.name, new Response(decodeStream(job), { headers: headers }));
    return { size: job.raw, cached: true };
  } catch (e) {
    /* no Cache API in this worker, or the stream died - fall through */
  }

  // Fallback: materialise the bundle and hand it back to the page.
  var buf = await new Response(decodeStream(job)).arrayBuffer();
  if (job.raw && buf.byteLength !== job.raw) {
    throw new Error('decoded ' + buf.byteLength + ' bytes, expected ' + job.raw);
  }
  return { size: buf.byteLength, cached: false, buf: buf };
}

function decodeStream(job) {
  var blob = job.file.slice(job.off, job.off + job.comp);
  var source = blob.stream ? blob.stream() : new Response(blob).body;
  return new XzReadableStream(source);
}
