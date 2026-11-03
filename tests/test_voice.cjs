const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');
const source = fs.readFileSync(path.join(root, 'app.js'), 'utf8');
const logic = source.slice(0, source.indexOf("\ndocument.querySelectorAll('[data-add-row]')"));

function harness() {
  const fields = [], utterances = [], recognizers = [];
  let busy = null, speaking = null;
  const timers = new Map();
  let timerId = 0;
  class Element {
    constructor() { this.dataset = {}; this.children = []; this.events = {}; this.attrs = {}; this.classes = new Set(); this.textContent = ''; }
    classList = { toggle: (name, on) => on ? this.classes.add(name) : this.classes.delete(name), add: (name) => this.classes.add(name), remove: (name) => this.classes.delete(name) };
    setAttribute(key, val) { this.attrs[key] = val; }
    append(child) { this.children.push(child); child.parent = this; }
    addEventListener(event, callback) { this.events[event] = callback; }
    remove() { if (this.parent) this.parent.children = this.parent.children.filter((child) => child !== this); }
    closest(selector) { return selector === '.input-wrap' ? this.wrap || this.parent : this.field; }
    querySelector(selector) {
      if (selector === 'input') return this.input;
      if (selector === '.field-label') return { cloneNode: () => ({ textContent: this.label, querySelectorAll: () => [] }) };
      if (selector === '.voice-field-status') return this.children.find((child) => child.className === 'voice-field-status');
      return null;
    }
  }
  const elements = new Map();
  const document = {
    getElementById: (id) => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); },
    createElement: () => new Element(),
    querySelectorAll(selector) {
      if (selector.includes('input')) return fields.map((field) => field.input).filter((input) => !input.disabled);
      if (selector === '.field-voice') return fields.flatMap((field) => field.wrap.children);
      if (selector === '.voice-field-status') return fields.flatMap((field) => field.children);
      if (selector === '.voice-listening') return fields.filter((field) => field.classes.has('voice-listening'));
      return [];
    },
  };
  class Recognition {
    constructor() { recognizers.push(this); }
    start() { if (busy) throw Error('Recognizer still busy'); busy = this; this.onstart?.(); }
    stop() { this.stopped = true; }
    abort() { if (busy === this) busy = null; this.aborted = true; this.onend?.(); }
    result(text) { this.onresult?.({ resultIndex: 0, results: [Object.assign([{ transcript: text }], { isFinal: true })] }); }
    end() { if (busy === this) busy = null; this.onend?.(); }
  }
  class Utterance { constructor(text) { this.text = text; } }
  const window = {
    SpeechRecognition: Recognition, SpeechSynthesisUtterance: Utterance,
    speechSynthesis: {
      getVoices: () => [{ lang: 'hi-IN' }],
      speak: (utterance) => { utterances.push(utterance); speaking = utterance; },
      cancel: () => { const old = speaking; speaking = null; old?.onerror?.(); },
    },
    setTimeout: (callback) => { timers.set(++timerId, callback); return timerId; },
    clearTimeout: (id) => timers.delete(id),
  };
  const context = vm.createContext({ window, document, Intl, Event, SpeechSynthesisUtterance: Utterance, console });
  vm.runInContext(logic, context, { filename: 'app.js' });
  function input(label, type = 'number', disabled = false) {
    const field = new Element(); field.label = label;
    const wrap = new Element(); wrap.field = field;
    const control = new Element();
    Object.assign(control, { field, wrap, type, disabled, value: '', isConnected: true, dispatched: [], focus() {}, scrollIntoView() {}, dispatchEvent(event) { this.dispatched.push(event.type); } });
    field.input = control; field.wrap = wrap; wrap.input = control;
    fields.push(field);
    return control;
  }
  return { context, input, fields, utterances, recognizers, timers,
    finishSpeech() { const last = utterances.at(-1); speaking = null; last.onend?.(); },
    active: () => vm.runInContext('voiceActive', context),
  };
}

test('money, Hindi numbers, decimals and phone digits are preserved', () => {
  const { context: c } = harness();
  for (const [spoken, expected] of [
    ['₹6,378', 6378], ['६,३७८ रुपये', 6378], ['1,22,000', 122000],
    ['पैंतीस', 35], ['सैंतालीस', 47], ['निन्यानवे', 99], ['एक सौ पचास रुपये', 150],
    ['दो हजार पांच सौ', 2500], ['3 हजार 500', 3500], ['forty five', 45],
    ['तेईस दशमलव आठ', 23.8], ['23 दशमलव 8', 23.8], ['23.8 किलो', 23.8],
    ['शून्य', 0], ['डेढ़ सौ', 150], ['ढाई हजार', 2500],
    ['minus five', null], ['-10', null], ['पांच अस्पष्ट', null], ['कुछ नहीं सुना', null],
  ]) assert.equal(c.parseSpokenNumber(spoken), expected, spoken);
  assert.equal(c.parseSpokenPhone('नौ आठ सात छह पांच चार तीन दो एक शून्य।'), '9876543210');
  assert.equal(c.parseSpokenPhone('९८७६५४३२१०'), '9876543210');
});

test('every editable field microphone fills its own field; disabled opening fields are excluded', () => {
  const h = harness();
  const html = fs.readFileSync(path.join(root, 'index.html'), 'utf8');
  for (const match of html.matchAll(/<label class="field">([\s\S]*?)<\/label>/g)) {
    const content = match[1];
    const label = content.match(/<span class="field-label">([\s\S]*?)<\/span>/)?.[1].replace(/<[^>]*>/g, '').trim();
    h.input(label, /type="tel"/.test(content) ? 'tel' : 'number', /\bdisabled\b/.test(content));
  }
  h.context.setupVoiceButtons();
  let count = 0;
  for (const field of h.fields) {
    if (field.input.disabled) { assert.equal(field.wrap.children.length, 0); continue; }
    const button = field.wrap.children[0];
    assert.ok(button.events.click, field.label);
    button.events.click({ preventDefault() {} });
    h.finishSpeech();
    const recognition = h.recognizers.at(-1);
    assert.equal(recognition.lang, 'hi-IN');
    recognition.result(field.input.type === 'tel' ? '9876543210' : '6,378 रुपये');
    recognition.end();
    assert.equal(field.input.value, field.input.type === 'tel' ? '9876543210' : '6378', field.label);
    assert.deepEqual(field.input.dispatched, ['input', 'change']);
    h.context.stopVoice(); // Isolate the next button; normal use continues automatically.
    assert.equal(h.active(), false);
    count++;
  }
  assert.ok(count >= 30, `Only ${count} fields were covered`);
  const added = h.input('Supplier phone number', 'tel');
  h.context.setupVoiceButtons();
  added.wrap.children[0].events.click({ preventDefault() {} });
  h.finishSpeech(); h.recognizers.at(-1).result('9123456789'); h.recognizers.at(-1).end();
  assert.equal(added.value, '9123456789');
});

test('switching fields ignores old speech and recognition callbacks', () => {
  const h = harness(), first = h.input('Cash received'), second = h.input('Total sales');
  h.context.startVoiceFor(first, false);
  const oldPrompt = h.utterances.at(-1);
  h.context.startVoiceFor(second, false);
  oldPrompt.onend();
  assert.equal(h.recognizers.length, 0);
  h.finishSpeech();
  const oldRecognition = h.recognizers.at(-1);
  h.context.startVoiceFor(first, false); h.finishSpeech();
  oldRecognition.result('99'); oldRecognition.onend();
  assert.equal(second.value, '');
  h.recognizers.at(-1).result('500'); h.recognizers.at(-1).end();
  assert.equal(first.value, '500');
});

test('guided entry waits for microphone end before advancing and stop cancels pending speech', () => {
  const h = harness(), first = h.input('Cash received'), second = h.input('Total sales');
  h.context.startVoiceFor(null, true);
  h.finishSpeech(); h.finishSpeech();
  const recognition = h.recognizers.at(-1);
  recognition.result('150');
  assert.equal(h.recognizers.length, 1);
  recognition.end(); h.finishSpeech(); h.finishSpeech();
  assert.equal(h.recognizers.length, 2);
  assert.equal(first.value, '150');
  h.context.stopVoice();
  h.recognizers.at(-1).result('700');
  assert.equal(second.value, '');
  assert.equal(h.active(), false);
});

test('speech error/end is handled once; empty recognition and permission errors allow retry', () => {
  const h = harness(), field = h.input('Closing Boiler stock');
  h.context.startVoiceFor(field, false);
  const speech = h.utterances.at(-1);
  speech.onerror(); speech.onend();
  assert.equal(h.recognizers.length, 1);
  h.recognizers.at(-1).end();
  assert.equal(h.active(), false);
  h.context.startVoiceFor(field, false); h.finishSpeech();
  h.recognizers.at(-1).onerror({ error: 'not-allowed' });
  assert.equal(h.active(), false);
  h.context.startVoiceFor(field, false); h.finishSpeech();
  h.recognizers.at(-1).result('पैंतीस'); h.recognizers.at(-1).end();
  assert.equal(field.value, '35');
});

test('one microphone click completes every following field without further clicks', () => {
  const h = harness();
  const html = fs.readFileSync(path.join(root, 'index.html'), 'utf8');
  for (const match of html.matchAll(/<label class="field">([\s\S]*?)<\/label>/g)) {
    const body = match[1];
    h.input(body.match(/<span class="field-label">([\s\S]*?)<\/span>/)[1].replace(/<[^>]*>/g, '').trim(), /type="tel"/.test(body) ? 'tel' : 'number', /\bdisabled\b/.test(body));
  }
  h.context.setupVoiceButtons();
  const eligible = h.fields.filter((field) => !field.input.disabled);
  eligible[0].wrap.children[0].events.click({ preventDefault() {} });
  h.finishSpeech();
  for (const [index, field] of eligible.entries()) {
    assert.equal(h.active(), true, `Stopped before field ${index}: ${field.label}`);
    const recognition = h.recognizers.at(-1);
    recognition.result(field.input.type === 'tel' ? '9876543210' : String(index));
    recognition.end();
    h.finishSpeech(); // Acknowledgment automatically asks the next question.
    if (index < eligible.length - 1) h.finishSpeech(); // Question automatically starts listening.
    assert.equal(field.input.value, field.input.type === 'tel' ? '9876543210' : String(index));
  }
  assert.equal(h.recognizers.length, eligible.length);
  assert.equal(h.active(), false);
});

test('clicking a middle field continues from that field, skipping disabled openings', () => {
  const h = harness();
  const previous = h.input('Total sales'), first = h.input('Cash received');
  const opening = h.input('Opening counter cash', 'number', true), next = h.input('Closing counter cash');
  h.context.setupVoiceButtons();
  first.wrap.children[0].events.click({ preventDefault() {} }); h.finishSpeech();
  h.recognizers.at(-1).result('500'); h.recognizers.at(-1).end();
  h.finishSpeech(); h.finishSpeech();
  h.recognizers.at(-1).result('900'); h.recognizers.at(-1).end(); h.finishSpeech();
  assert.equal(previous.value, ''); assert.equal(opening.value, '');
  assert.equal(first.value, '500'); assert.equal(next.value, '900'); assert.equal(h.active(), false);
});

test('guided mode automatically retries silence, invalid answers and empty endings', () => {
  const h = harness(), field = h.input('Total sales');
  h.context.startVoiceFor(field); h.finishSpeech();
  h.recognizers.at(-1).onerror({ error: 'no-speech' }); h.recognizers.at(-1).end();
  assert.equal(h.active(), true); h.finishSpeech();
  h.recognizers.at(-1).result('समझ नहीं आया'); h.recognizers.at(-1).end();
  assert.equal(field.value, ''); assert.equal(h.active(), true); h.finishSpeech();
  h.recognizers.at(-1).end(); h.finishSpeech();
  h.recognizers.at(-1).result('शून्य'); h.recognizers.at(-1).end(); h.finishSpeech();
  assert.equal(field.value, '0'); assert.equal(h.active(), false);
});

test('stop during automatic retry prevents any further microphone start', () => {
  const h = harness(), field = h.input('Cash received');
  h.context.startVoiceFor(field); h.finishSpeech();
  h.recognizers.at(-1).end();
  const retry = h.utterances.at(-1);
  h.context.stopVoice(); retry.onend();
  assert.equal(h.recognizers.length, 1); assert.equal(h.active(), false);
});

test('guided microphone timeout retries without a click', () => {
  const h = harness(), field = h.input('Cash received');
  h.context.startVoiceFor(field); h.finishSpeech();
  [...h.timers.values()].at(-1)(); h.finishSpeech();
  assert.equal(h.recognizers.length, 2); assert.equal(h.active(), true);
  h.recognizers.at(-1).result('20'); h.recognizers.at(-1).end(); h.finishSpeech();
  assert.equal(field.value, '20'); assert.equal(h.active(), false);
});
