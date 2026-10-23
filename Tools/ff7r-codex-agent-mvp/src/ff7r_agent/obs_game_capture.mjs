// Read only the OBS source explicitly bound to the allow-listed game window.
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

export function verifiedGameSource(input, settings, gameProcess) {
  const kind = input.inputKind;
  if (!['game_capture', 'window_capture'].includes(kind)) return null;
  const window = settings.window;
  if (typeof window !== 'string' || window.split(':').at(-1)?.toLowerCase() !== gameProcess.toLowerCase()) return null;
  if (kind === 'game_capture' && settings.capture_mode !== 'window') return null;
  return { source_name: input.inputName, input_kind: kind, window, capture_mode: settings.capture_mode ?? null };
}

export async function readGameFrame(obs, gameProcess, describeOnly = false) {
  const inputs = (await obs.call('GetInputList')).inputs;
  const candidates = [];
  for (const input of inputs) {
    if (!['game_capture', 'window_capture'].includes(input.inputKind)) continue;
    const settings = (await obs.call('GetInputSettings', { inputName: input.inputName })).inputSettings;
    const verified = verifiedGameSource(input, settings, gameProcess);
    if (verified) candidates.push(verified);
  }
  if (describeOnly) return { ok: true, inspected_inputs: inputs.length, verified_candidates: candidates };
  if (candidates.length !== 1) throw new Error('OBS requires exactly one source explicitly bound to the game window');
  const source = candidates[0];
  const active = await obs.call('GetSourceActive', { sourceName: source.source_name });
  if (!active.videoActive) throw new Error('Verified OBS game source is inactive');
  const shot = await obs.call('GetSourceScreenshot', { sourceName: source.source_name, imageFormat: 'png', imageWidth: 1536, imageHeight: 864 });
  const after = (await obs.call('GetInputSettings', { inputName: source.source_name })).inputSettings;
  if (JSON.stringify(verifiedGameSource({ inputName: source.source_name, inputKind: source.input_kind }, after, gameProcess)) !== JSON.stringify(source)) {
    throw new Error('OBS game source identity changed during capture');
  }
  if (typeof shot.imageData !== 'string' || !shot.imageData.startsWith('data:image/png;base64,') || shot.imageData.length > 32000000) throw new Error('Invalid OBS game image');
  const pixels = Buffer.from(shot.imageData.slice('data:image/png;base64,'.length), 'base64');
  if (pixels.length < 24 || !pixels.subarray(0, 8).equals(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]))) throw new Error('OBS game image is not PNG');
  return { ok: true, ...source, width: pixels.readUInt32BE(16), height: pixels.readUInt32BE(20), pixels };
}

async function main() {
  // Optional OBS capture uses explicit environment configuration only.
  const { default: OBSWebSocket } = await import('obs-websocket-js');
  const config = { obsUrl: process.env.FF7R_OBS_URL || 'ws://127.0.0.1:4455', obsPassword: process.env.FF7R_OBS_PASSWORD || '' };
  const obsUrl = new URL(config.obsUrl);
  if (obsUrl.protocol !== 'ws:' || !['127.0.0.1', 'localhost', '[::1]'].includes(obsUrl.hostname)) throw new Error('OBS must be a loopback websocket');
  const obs = new OBSWebSocket();
  try {
    await obs.connect(config.obsUrl, config.obsPassword);
    const result = await readGameFrame(obs, 'ff7rebirth_.exe', process.argv[2] === '--describe');
    if (result.pixels) {
      const output = process.argv[2];
      if (!output || !path.isAbsolute(output) || path.extname(output) !== '.png') throw new Error('Absolute PNG output required');
      await fs.writeFile(output, result.pixels, { flag: 'wx' });
      delete result.pixels;
      result.image_path = output;
    }
    process.stdout.write(JSON.stringify(result) + '\n');
  } finally { await obs.disconnect(); }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch(error => { process.stderr.write(String(error.message).replace(/(password|token)\s*[=:]\s*\S+/gi, '$1=[redacted]') + '\n'); process.exitCode = 1; });
}
