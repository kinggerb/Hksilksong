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
      self.postMessage({ id: job.id, ok: true, size: out.size, cached: out.cached }, out.buf ? [out.buf] : []);
    },
    function (err) {
      self.postMessage({ id: job.id, ok: false, error: String((err && err.message) || err) });
    }
  );
};

async function handle(job) {
  if (!XzReadableStream) throw new Error('xz decoder failed to load');

  var blob = job.file.slice(job.off, job.off + job.comp);
  var source = blob.stream ? blob.stream() : new Response(blob).body;
  var buf = await new Response(new XzReadableStream(source)).arrayBuffer();

  if (job.raw && buf.byteLength !== job.raw) {
    throw new Error('decoded ' + buf.byteLength + ' bytes, expected ' + job.raw);
  }

  try {
    var cache = await caches.open(job.cacheName);
    await cache.put(
      job.name,
      new Response(buf, {
        headers: {
          'Content-Type': 'application/octet-stream',
          'X-HK-Bundle': job.name,
        },
      })
    );
    return { size: buf.byteLength, cached: true };
  } catch (e) {
    return { size: buf.byteLength, cached: false, buf: buf };
  }
}
