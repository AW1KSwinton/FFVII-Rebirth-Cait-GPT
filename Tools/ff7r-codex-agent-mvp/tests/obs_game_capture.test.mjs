import test from 'node:test';
import assert from 'node:assert/strict';
import { readGameFrame, obsConfiguration, safeCaptureError } from '../src/ff7r_agent/obs_game_capture.mjs';

test('OBS configuration rejects remote endpoints and URL-carried credentials', () => {
  for (const url of ['ws://remote.invalid:4455', 'ws://fixture:example@127.0.0.1:4455',
      'ws://127.0.0.1:4455/?credential=example', 'ws://localhost:4455/#example',
      'ws://localhost:4455/private']) {
    assert.throws(() => obsConfiguration({FF7R_OBS_URL: url}));
  }
  assert.equal(obsConfiguration({}).obsUrl, 'ws://127.0.0.1:4455');
  assert.equal(obsConfiguration({FF7R_OBS_URL: 'ws://[::1]:4455'}).obsUrl, 'ws://[::1]:4455');
});

test('OBS failures redact the configured secret even without a password label', () => {
  const secret = 'synthetic fixture/value';
  const result = safeCaptureError(new Error(`Rejected ${secret} / ${encodeURIComponent(secret)} token=example`),
    {FF7R_OBS_PASSWORD: secret});
  assert.ok(!result.includes(secret));
  assert.ok(!result.includes(encodeURIComponent(secret)));
  assert.ok(result.includes('token=[redacted]'));
});

const window = 'FINAL FANTASY VII REBIRTH:UnrealWindow:ff7rebirth_.exe';
function fixture({mode = 'window', target = window, ambiguous = false, changed = false, active = true} = {}) {
  const calls = [];
  let settingsReads = 0;
  const png = Buffer.alloc(24);
  Buffer.from([137,80,78,71,13,10,26,10]).copy(png);
  png.writeUInt32BE(1536,16); png.writeUInt32BE(864,20);
  return { calls, obs: {call: async (name, args) => {
    calls.push({name,args});
    if(name === 'GetInputList') return {inputs: [{inputName:'Desktop',inputKind:'monitor_capture'}, {inputName:'Game',inputKind:'game_capture'}, ...(ambiguous ? [{inputName:'Other Game',inputKind:'game_capture'}] : [])]};
    if(name === 'GetInputSettings') return {inputSettings:{capture_mode:mode,window:changed && ++settingsReads > 1 ? 'Other:Window:private.exe' : target}};
    if(name === 'GetSourceActive') return {videoActive:active};
    if(name === 'GetSourceScreenshot') return {imageData:'data:image/png;base64,' + png.toString('base64')};
    throw Error('Unexpected request');
  }}};
}
test('captures only the uniquely verified game source, never desktop', async () => {
  const f = fixture(), r = await readGameFrame(f.obs, 'ff7rebirth_.exe');
  assert.equal(r.source_name,'Game'); assert.equal(r.width,1536); assert.equal(r.height,864);
  assert.deepEqual(f.calls.filter(c=>c.name==='GetSourceScreenshot').map(c=>c.args.sourceName),['Game']);
  assert(!f.calls.some(c=>c.args?.inputName==='Desktop'));
});
for(const [name, options] of [['fullscreen',{mode:'any_fullscreen'}],['other executable',{target:'Other:Window:private.exe'}],['ambiguous',{ambiguous:true}],['inactive',{active:false}]]) {
  test('refuses ' + name + ' before taking a frame', async () => {
    const f = fixture(options);
    await assert.rejects(readGameFrame(f.obs,'ff7rebirth_.exe'));
    assert(!f.calls.some(c=>c.name==='GetSourceScreenshot'));
  });
}
test('rejects source rebinding during capture', async () => {
  const f=fixture({changed:true});
  await assert.rejects(readGameFrame(f.obs,'ff7rebirth_.exe'),/identity changed/);
});
