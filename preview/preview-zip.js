// Small uncompressed ZIP writer for downloadable workbooks and project documents.
const encoder = new TextEncoder();
const crcTable = Uint32Array.from({ length: 256 }, (_, n) => {
  let value = n;
  for (let bit = 0; bit < 8; bit++) value = (value >>> 1) ^ (value & 1 ? 0xedb88320 : 0);
  return value >>> 0;
});

function crc32(bytes) {
  let crc = 0xffffffff;
  for (const byte of bytes) crc = crcTable[(crc ^ byte) & 0xff] ^ (crc >>> 8);
  return (crc ^ 0xffffffff) >>> 0;
}

function header(values) {
  const buffer = new ArrayBuffer(values.reduce((sum, [width]) => sum + width, 0));
  const data = new DataView(buffer);
  let offset = 0;
  for (const [width, value] of values) {
    if (width === 4) data.setUint32(offset, value, true);
    else data.setUint16(offset, value, true);
    offset += width;
  }
  return new Uint8Array(buffer);
}

export function previewZip(entries) {
  const local = [], central = [];
  let offset = 0;
  for (const [path, content] of entries) {
    const name = encoder.encode(path);
    const bytes = typeof content === 'string' ? encoder.encode(content) : content;
    const info = [[2, 20], [2, 0x0800], [2, 0], [2, 0], [2, 33], [4, crc32(bytes)],
      [4, bytes.byteLength], [4, bytes.byteLength], [2, name.length], [2, 0]];
    const start = header([[4, 0x04034b50], ...info]);
    const directory = header([[4, 0x02014b50], [2, 20], ...info, [2, 0], [2, 0], [2, 0], [4, 0], [4, offset]]);
    local.push(start, name, bytes);
    central.push(directory, name);
    offset += start.length + name.length + bytes.length;
  }
  const size = central.reduce((sum, block) => sum + block.length, 0);
  const count = central.length / 2;
  const end = header([[4, 0x06054b50], [2, 0], [2, 0], [2, count], [2, count], [4, size], [4, offset], [2, 0]]);
  const output = new Uint8Array(offset + size + end.length);
  let cursor = 0;
  for (const part of [...local, ...central, end]) {
    output.set(part, cursor);
    cursor += part.length;
  }
  return output;
}

// Preview exports use stored entries; only read the independently identified manifest.
export function readPreviewEntry(bytes, path) {
  const decoder = new TextDecoder();
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  for (let offset = 0; offset + 30 <= bytes.length && view.getUint32(offset, true) === 0x04034b50;) {
    const size = view.getUint32(offset + 18, true);
    const nameSize = view.getUint16(offset + 26, true);
    const start = offset + 30 + nameSize + view.getUint16(offset + 28, true);
    const name = decoder.decode(bytes.subarray(offset + 30, offset + 30 + nameSize));
    if (start + size > bytes.length || view.getUint16(offset + 6, true) & 8) break;
    if (name === path && view.getUint16(offset + 8, true) === 0) {
      const entry = bytes.subarray(start, start + size);
      if (view.getUint32(offset + 14, true) !== crc32(entry)) throw new Error('分享包文件校验失败');
      return entry;
    }
    offset = start + size;
  }
  throw new Error('请选择从此预览导出的项目分享 ZIP');
}
