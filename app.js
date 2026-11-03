const API = '/api';
const entryDate = document.getElementById('entry-date');
const summaryDate = document.getElementById('summary-date');
const entryForm = document.getElementById('entry-form');
const saveButton = document.getElementById('save-entry');
const saveStatus = document.getElementById('save-status');
const moneyFormat = new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 });
let isDirty = false;
let isSaving = false;
let activeScreen = 'entry';
let loadedDate = '';
let voiceActive = false;
let guidedVoice = false;
let voiceQueue = [];
let voiceIndex = 0;
let voiceTarget = null;
let voiceRun = 0;
let voiceRecognition = null;
let voiceTimer = null;
const SpeechRecognitionAPI = window.SpeechRecognition || window.webkitSpeechRecognition;

function localDateString(date = new Date()) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  return `${year}-${month}-${day}`;
}

function fieldKey(field) {
  const label = field.querySelector('.field-label').cloneNode(true);
  label.querySelectorAll('.auto-tag,.hint-icon,.unconfirmed').forEach((el) => el.remove());
  const key = label.textContent.trim().toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_|_$/g, '');
  return ({ closing_counter_cash: 'closing_cash', opening_counter_cash: 'opening_cash' })[key] || key;
}

function allSingleFields() {
  return [...entryForm.querySelectorAll('.field-grid > .field')];
}

function setStatus(message, kind = '') {
  saveStatus.textContent = message;
  saveStatus.className = kind ? `status-${kind}` : '';
}

function markDirty() {
  isDirty = true;
  updateSaveButton();
  setStatus('Unsaved changes. Save this entry to keep them.');
}

function updateSaveButton() {
  const canSave = isDirty && !isSaving;
  saveButton.disabled = !canSave;
  saveButton.classList.toggle('is-saving', isSaving);
  saveButton.innerHTML = isSaving ? '<span>…</span> Saving' : '<span>✓</span> Save entry';
}

function money(value) {
  return `₹${moneyFormat.format(Number(String(value ?? '').replace(/,/g, '')) || 0)}`;
}

function addRepeatRow(group, values = {}) {
  const list = document.querySelector(`[data-entry-list="${group}"]`);
  const sample = list.querySelector('.repeat-row');
  const row = sample.cloneNode(true);
  row.classList.add('with-remove');
  row.querySelectorAll('.field-voice,.voice-field-status').forEach((button) => button.remove());
  row.querySelectorAll('input').forEach((input) => { input.value = ''; delete input.dataset.voiceButton; });
  const inputs = row.querySelectorAll('input');
  inputs[0].value = values.phone || '';
  inputs[1].value = values.amount || '';
  const remove = document.createElement('button');
  remove.type = 'button';
  remove.className = 'remove-row';
  remove.setAttribute('aria-label', `Remove ${group === 'udhar' ? 'Udhar' : 'supplier payment'} row`);
  remove.textContent = '×';
  remove.addEventListener('click', () => { row.remove(); markDirty(); });
  row.append(remove);
  list.append(row);
  setupVoiceButtons(row);
  row.querySelector('input')?.focus();
  markDirty();
  return row;
}

function setRepeated(group, values) {
  const list = document.querySelector(`[data-entry-list="${group}"]`);
  const template = list.querySelector('.repeat-row').cloneNode(true);
  list.replaceChildren();
  const rows = Array.isArray(values) && values.length ? values : [{}];
  rows.forEach((value, index) => {
    const row = template.cloneNode(true);
    row.querySelectorAll('.field-voice,.voice-field-status').forEach((button) => button.remove());
    row.querySelectorAll('input').forEach((input) => { input.value = ''; delete input.dataset.voiceButton; });
    const inputs = row.querySelectorAll('input');
    if (inputs[0]) inputs[0].value = value.phone || '';
    if (inputs[1]) inputs[1].value = value.amount || '';
    if (index > 0) {
      row.classList.add('with-remove');
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.className = 'remove-row';
      remove.setAttribute('aria-label', `Remove ${group === 'udhar' ? 'Udhar' : 'supplier payment'} row`);
      remove.textContent = '×';
      remove.addEventListener('click', () => { row.remove(); markDirty(); });
      row.append(remove);
    }
    list.append(row);
  });
  setupVoiceButtons(list);
}

function fillEntry(entry) {
  isDirty = false;
  loadedDate = entry.date;
  const fields = entry.fields || {};
  allSingleFields().forEach((field) => {
    const input = field.querySelector('input');
    const key = fieldKey(field);
    input.value = fields[key] ?? '';
  });
  // Opening values are supplied by the server and remain disabled in the form.
  document.querySelector('[aria-label^="Opening Boiler stock"]').value = fields.opening_boiler_stock || '—';
  document.querySelector('[aria-label^="Opening Desi stock"]').value = fields.opening_desi_stock || '—';
  document.querySelector('[aria-label^="Opening counter cash"]').value = fields.opening_cash || '—';
  setRepeated('udhar', entry.udhar);
  setRepeated('supplier', entry.suppliers);
  document.getElementById('entry-status').value = entry.status || 'draft';
  document.querySelector('.draft-tag').textContent = entry.exists ? (entry.status === 'complete' ? 'COMPLETE' : 'SAVED DRAFT') : 'NEW ENTRY';
  setStatus(entry.exists ? 'Saved entry loaded. Changes will update this date.' : 'New entry. Opening values come from the previous saved day.');
  updateQuickLook();
  if (!isSaving) updateSaveButton();
}

async function loadEntry() {
  if (!entryDate.value) return;
  if (voiceActive) stopVoice('नई तारीख खुलने के कारण वॉइस एंट्री रोक दी गई।');
  setStatus('Loading this date…');
  try {
    const response = await fetch(`${API}/entry?date=${encodeURIComponent(entryDate.value)}`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Could not load this date.');
    fillEntry(data);
  } catch (error) {
    setStatus(error.message, 'error');
  }
}

function collectEntry() {
  const fields = {};
  allSingleFields().forEach((field) => {
    const input = field.querySelector('input');
    if (!input.disabled) fields[fieldKey(field)] = input.value.trim();
  });
  const repeatValues = (group) => [...document.querySelectorAll(`[data-entry-list="${group}"] .repeat-row`)]
    .map((row) => {
      const inputs = row.querySelectorAll('input');
      return { phone: inputs[0].value.trim(), amount: inputs[1].value.trim() };
    }).filter((item) => item.phone || item.amount);
  return { date: entryDate.value, fields, udhar: repeatValues('udhar'), suppliers: repeatValues('supplier'), status: document.getElementById('entry-status').value };
}

async function saveEntry(event, { draft = false, autoSave = false } = {}) {
  event?.preventDefault();
  if (isSaving || (!isDirty && !autoSave)) return;
  if (voiceActive) stopVoice('सेव करने के लिए वॉइस एंट्री रोक दी गई।');
  if (!entryDate.value) return setStatus('Choose an entry date first.', 'error');
  isSaving = true;
  updateSaveButton();
  setStatus(autoSave ? 'Saving a draft…' : 'Saving your entry…');
  try {
    const entry = collectEntry();
    if (draft) entry.status = 'draft';
    const response = await fetch(`${API}/entry`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(entry),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Could not save this entry.');
    fillEntry(data.entry);
    isDirty = false;
    setStatus(autoSave ? 'Draft saved automatically.' : 'Entry saved successfully on this device.', 'success');
  } catch (error) {
    setStatus(error.message, 'error');
  } finally {
    isSaving = false;
    updateSaveButton();
  }
}

function updateQuickLook() {
  const fields = collectEntry().fields;
  const rows = document.querySelectorAll('.summary-row strong');
  if (rows.length >= 4) {
    rows[0].textContent = money(fields.total_sales);
    rows[1].textContent = money(fields.cash_received);
    const numberValue = (value) => Number(String(value ?? '').replace(/,/g, '')) || 0;
    rows[2].textContent = money(numberValue(fields.paytm_received) + numberValue(fields.phonepe_received));
    rows[3].textContent = `${numberValue(fields.new_customers) + numberValue(fields.returning_customers)} people`;
  }
  const note = document.querySelector('.summary-note');
  if (note) note.textContent = 'Totals update as you fill in the form.';
}

function formatDate(isoDate) {
  if (!isoDate) return '';
  return new Date(`${isoDate}T12:00:00`).toLocaleDateString(undefined, { year: 'numeric', month: 'long', day: 'numeric' });
}

async function showHistory() {
  const list = document.getElementById('history-list');
  const search = document.getElementById('history-search');
  try {
    const response = await fetch(`${API}/entries`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Could not load entries.');
    const entries = data.entries || [];
    const draw = () => {
      const query = search.value.trim().toLowerCase();
      const filtered = entries.filter((entry) => formatDate(entry.date).toLowerCase().includes(query) || entry.date.includes(query));
      document.getElementById('history-count').textContent = `${filtered.length} ${filtered.length === 1 ? 'entry' : 'entries'}`;
      list.replaceChildren();
      if (!filtered.length) {
        const empty = document.createElement('div');
        empty.className = 'empty-state';
        empty.innerHTML = '<div>▤</div><strong>No entries yet</strong><p>Saved daily entries will appear here. Choose Daily entry to record your first day.</p>';
        list.append(empty);
        return;
      }
      filtered.forEach(({ date, summary, status }) => {
        const card = document.createElement('article');
        card.className = 'history-card';
        const info = document.createElement('div');
        info.className = 'history-date';
        const strong = document.createElement('strong');
        strong.textContent = formatDate(date);
        const caption = document.createElement('span');
        caption.textContent = `${summary.customers} customers · ${summary.weight_sold} kg sold`;
        info.append(strong, caption);
        const total = document.createElement('strong');
        total.className = 'history-total';
        total.textContent = money(summary.total_sales);
        const open = document.createElement('button');
        open.className = 'open-entry';
        open.type = 'button';
        open.textContent = 'Open';
        open.addEventListener('click', () => {
          if (!showScreen('entry')) return;
          entryDate.value = date;
          loadEntry();
        });
        const badge = document.createElement('span');
        badge.className = `history-status ${status === 'complete' ? 'complete' : ''}`;
        badge.textContent = status === 'complete' ? 'Complete' : 'Draft';
        card.append(info, badge, total, open);
        list.append(card);
      });
    };
    search.oninput = draw;
    draw();
  } catch (error) {
    list.textContent = error.message;
  }
}

async function showSummary() {
  const targetDate = summaryDate.value || entryDate.value || localDateString();
  summaryDate.value = targetDate;
  try {
    const response = await fetch(`${API}/summary?date=${encodeURIComponent(targetDate)}`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Could not load summary.');
    const summary = data.summary;
    const values = {
      'sum-sales': money(summary.total_sales), 'sum-cash': money(summary.cash_received),
      'sum-online': money(summary.online_received), 'sum-customers': summary.customers,
      'sum-paytm': money(summary.paytm_received), 'sum-phonepe': money(summary.phonepe_received),
      'sum-expenses': money(summary.expenses), 'sum-udhar': money(summary.udhar_total),
      'sum-new-customers': summary.new_customers, 'sum-returning-customers': summary.returning_customers,
      'sum-weight': `${summary.weight_sold} kg`, 'sum-boiler-stock': summary.closing_boiler_stock || '—',
      'sum-desi-stock': summary.closing_desi_stock || '—', 'sum-closing-cash': money(summary.closing_cash),
    };
    Object.entries(values).forEach(([id, value]) => { document.getElementById(id).textContent = value; });
    const check = document.getElementById('payment-check');
    if (!summary.total_sales && !summary.cash_received && !summary.online_received) {
      check.textContent = 'Save an entry for this date to see a payment check.';
      check.className = 'check-message';
    } else if (summary.payment_check) {
      check.textContent = '✓ Sales match cash and online payments.';
      check.className = 'check-message check-ok';
    } else {
      check.textContent = `Check amounts: sales are ${money(summary.total_sales)}; cash + online is ${money(summary.cash_received + summary.online_received)}.`;
      check.className = 'check-message check-alert';
    }
  } catch (error) {
    document.getElementById('payment-check').textContent = error.message;
  }
}

function showScreen(screen) {
  if (screen !== activeScreen && isDirty) {
    if (!window.confirm('You have unsaved changes. Leave this entry without saving?')) return false;
    isDirty = false;
  }
  activeScreen = screen;
  if (screen !== 'entry' && voiceActive) stopVoice('दूसरी स्क्रीन खुलने के कारण वॉइस एंट्री रोक दी गई।');
  const names = { entry: 'Daily entry', history: 'Past entries', summary: 'Shop summary' };
  document.querySelectorAll('.screen').forEach((el) => { el.hidden = el.id !== `${screen}-screen`; });
  document.getElementById('current-screen-title').textContent = names[screen] || names.entry;
  document.querySelectorAll('[data-screen-link]').forEach((link) => {
    const active = link.dataset.screenLink === screen;
    link.classList.toggle('active', active);
    link.classList.toggle('mobile-active', active);
  });
  history.replaceState(null, '', `#${screen}`);
  if (screen === 'history') showHistory();
  if (screen === 'summary') showSummary();
  return true;
}

const hindiPrompts = {
  boiler_chicken_bought: 'आज कितने ब्रॉयलर चिकन खरीदे? संख्या बोलें।',
  desi_chicken_bought: 'आज कितने देसी चिकन खरीदे? संख्या बोलें।',
  boiler_purchase_cost: 'ब्रॉयलर चिकन खरीदने में कितने रुपये लगे?',
  desi_purchase_cost: 'देसी चिकन खरीदने में कितने रुपये लगे?',
  boiler_chicken_chopped: 'कितने ब्रॉयलर चिकन काटे?',
  desi_chicken_chopped: 'कितने देसी चिकन काटे?',
  closing_boiler_stock: 'दिन के अंत में कितने ब्रॉयलर चिकन बचे? संख्या बोलें।',
  closing_desi_stock: 'दिन के अंत में कितने देसी चिकन बचे? संख्या बोलें।',
  total_weight_sold: 'आज कुल कितने किलो वजन बेचा?',
  chicken_wasted: 'आज कितना चिकन बर्बाद हुआ? ग्राम में बताएं।',
  dead_chicken: 'आज कितने मुर्गे मरे? संख्या बोलें।',
  total_sales: 'आज की कुल बिक्री कितने रुपये हुई?',
  cash_received: 'नकद बिक्री कितने रुपये की हुई?',
  paytm_received: 'पेटीएम से कितने रुपये मिले?',
  phonepe_received: 'फोन पे से कितने रुपये मिले?',
  closing_counter_cash: 'दुकान बंद करते समय काउंटर में कितने रुपये हैं?',
  cash_taken_out_by_bahadur: 'बहादुर ने कितने रुपये निकाले?',
  new_customers: 'आज कितने नए ग्राहक आए?',
  returning_customers: 'आज कितने पुराने ग्राहक वापस आए?',
  miscellaneous_expense: 'दूसरे खर्च कितने रुपये के हुए?',
  dalia: 'डालिया में कितने रुपये लगे?',
  dead_chicken_cost: 'मरे हुए चिकन का खर्च कितने रुपये है?',
  police: 'पुलिस के लिए कितने रुपये दिए?',
  customer_phone_number: 'उधार लेने वाले ग्राहक का फोन नंबर एक-एक अंक करके बोलें।',
  udhar_amount: 'इस ग्राहक को कितने रुपये उधार दिए?',
  supplier_phone_number: 'सप्लायर का फोन नंबर एक-एक अंक करके बोलें।',
  payment_amount: 'सप्लायर को कितने रुपये दिए?',
  bahadur_salary: 'बहादुर की तनख्वाह कितने रुपये है?',
  shop_rent: 'दुकान का किराया कितने रुपये है?',
  electricity_bill: 'बिजली का बिल कितने रुपये का है?',
  ap: 'ए पी के लिए कितने रुपये हैं?',
  ag: 'ए जी के लिए कितने रुपये हैं?',
};

function inputLabel(input) {
  const field = input.closest('.field');
  const label = field?.querySelector('.field-label')?.cloneNode(true);
  label?.querySelectorAll('.auto-tag,.hint-icon,.unconfirmed').forEach((el) => el.remove());
  return label?.textContent.trim() || 'इस खाने';
}

function inputPrompt(input) {
  const key = inputLabel(input).toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_|_$/g, '');
  const translated = hindiPrompts[key] || `${inputLabel(input)} के लिए संख्या बोलें।`;
  if (input.type === 'tel') return translated;
  const unit = input.closest('.input-wrap')?.querySelector('.unit,.currency')?.textContent.trim();
  const unitHint = unit === '₹' ? 'रुपये में बोलें।' : unit === 'pieces' ? 'पीस में बोलें।' : unit === 'kg' ? 'किलो में बोलें।' : unit === 'grams' ? 'ग्राम में बोलें।' : '';
  return `${translated} ${unitHint} अगला छोड़ने के लिए “छोड़ें” कहें।`;
}

function setVoiceStatus(message, listening = false) {
  const status = document.getElementById('voice-status');
  status.textContent = message;
  status.classList.toggle('is-listening', listening);
  const field = voiceTarget?.closest('.field');
  if (field) {
    let hint = field.querySelector('.voice-field-status');
    if (!hint) {
      hint = document.createElement('span');
      hint.className = 'voice-field-status';
      hint.setAttribute('role', 'status');
      field.append(hint);
    }
    hint.textContent = message;
    field.classList.toggle('voice-listening', listening);
  }
}

function speakHindi(message, onEnd) {
  const run = voiceRun;
  let finished = false;
  const finish = () => {
    if (finished || run !== voiceRun) return;
    finished = true;
    onEnd?.();
  };
  if (!window.speechSynthesis || !window.SpeechSynthesisUtterance) {
    finish();
    return;
  }
  window.speechSynthesis.cancel();
  const utterance = new SpeechSynthesisUtterance(message);
  utterance.lang = 'hi-IN';
  utterance.rate = 0.88;
  const voice = window.speechSynthesis.getVoices().find((item) => /^hi([-_]|$)/i.test(item.lang));
  if (voice) utterance.voice = voice;
  utterance.onend = finish;
  utterance.onerror = finish;
  window.speechSynthesis.speak(utterance);
}

function hindiDigits(text) {
  const digits = '०१२३४५६७८९';
  return String(text).replace(/[०-९]/g, (digit) => String(digits.indexOf(digit)));
}

const spokenNumbers = {
  zero: 0, oh: 0, शून्य: 0, सुन्न: 0,
  one: 1, एक: 1, इक्क: 1,
  two: 2, दो: 2,
  three: 3, तीन: 3,
  four: 4, चार: 4,
  five: 5, पांच: 5, पाँच: 5,
  six: 6, छह: 6, छः: 6,
  seven: 7, सात: 7,
  eight: 8, आठ: 8,
  nine: 9, नौ: 9,
  ten: 10, दस: 10, ग्यारह: 11, eleven: 11, बारह: 12, twelve: 12,
  तेरह: 13, thirteen: 13, चौदह: 14, fourteen: 14, पंद्रह: 15, fifteen: 15,
  सोलह: 16, sixteen: 16, सत्रह: 17, seventeen: 17, अठारह: 18, eighteen: 18,
  उन्नीस: 19, nineteen: 19, बीस: 20, twenty: 20, इक्कीस: 21, बाईस: 22,
  तेईस: 23, चौबीस: 24, पच्चीस: 25, छब्बीस: 26, सत्ताईस: 27, अट्ठाईस: 28,
  उनतीस: 29, तीस: 30, चालीस: 40, पचास: 50, साठ: 60, सत्तर: 70,
  अस्सी: 80, नब्बे: 90,
};
// Hindi numbers are irregular: each value through 99 needs its own spelling.
const hindiNumberNames = 'शून्य एक दो तीन चार पांच छह सात आठ नौ दस ग्यारह बारह तेरह चौदह पंद्रह सोलह सत्रह अठारह उन्नीस बीस इक्कीस बाईस तेईस चौबीस पच्चीस छब्बीस सत्ताईस अट्ठाईस उनतीस तीस इकतीस बत्तीस तैंतीस चौंतीस पैंतीस छत्तीस सैंतीस अड़तीस उनतालीस चालीस इकतालीस बयालीस तैंतालीस चवालीस पैंतालीस छियालीस सैंतालीस अड़तालीस उनचास पचास इक्यावन बावन तिरपन चौवन पचपन छप्पन सत्तावन अट्ठावन उनसठ साठ इकसठ बासठ तिरसठ चौंसठ पैंसठ छियासठ सड़सठ अड़सठ उनहत्तर सत्तर इकहत्तर बहत्तर तिहत्तर चौहत्तर पचहत्तर छिहत्तर सतहत्तर अठहत्तर उनासी अस्सी इक्यासी बयासी तिरासी चौरासी पचासी छियासी सत्तासी अट्ठासी नवासी नब्बे इक्यानवे बानवे तिरानवे चौरानवे पचानवे छियानवे सत्तानवे अट्ठानवे निन्यानवे'.split(' ');
hindiNumberNames.forEach((word, value) => { spokenNumbers[word.normalize('NFC')] = value; });
Object.assign(spokenNumbers, { thirty: 30, forty: 40, fifty: 50, sixty: 60, seventy: 70, eighty: 80, ninety: 90, जीरो: 0, ज़ीरो: 0, पन्द्रह: 15, अट्ठाइस: 28, चौंतिस: 34, चौवालीस: 44, छियालीस: 46, डेढ़: 1.5, ढाई: 2.5 });
const spokenScales = { hundred: 100, सौ: 100, thousand: 1000, हजार: 1000, हज़ार: 1000, lakh: 100000, लाख: 100000 };
const ignoredNumberWords = new Set(['rupees', 'rupee', 'rs', 'रुपये', 'रुपए', 'रुपया', 'रुपे', 'पीस', 'टुकड़े', 'टुकड़ा', 'किलो', 'किलोग्राम', 'ग्राम', 'people', 'लोग', 'ग्राहक', 'हैं', 'का', 'की', 'हुआ', 'हुई']);

function parseHindiInteger(text) {
  const tokens = String(text).normalize('NFC').toLowerCase().split(/\s+/).filter(Boolean);
  let total = 0;
  let current = 0;
  let found = false;
  for (const token of tokens) {
    if (ignoredNumberWords.has(token) || ['and', 'और', 'kg', 'grams', 'gram', 'pieces', 'किग्रा', 'रुपयों', 'रूपये', 'रुपए', 'रूपए'].includes(token)) continue;
    if (Object.hasOwn(spokenScales, token)) {
      found = true;
      const scale = spokenScales[token];
      if (scale === 100) current = (current || 1) * scale;
      else { total += (current || 1) * scale; current = 0; }
    } else if (Object.hasOwn(spokenNumbers, token)) {
      current += spokenNumbers[token];
      found = true;
    } else if (/^\d+(?:\.\d+)?$/.test(token)) {
      current += Number(token);
      found = true;
    } else {
      return null;
    }
  }
  return found ? total + current : null;
}

function parseSpokenNumber(raw) {
  const text = hindiDigits(raw).normalize('NFC').toLowerCase().replace(/,/g, '').replace(/₹/g, ' ').replace(/[।.!?]+$/g, '').trim();
  if (!text || /[-−]|माइनस|ऋण|minus|negative/.test(text)) return null;
  const decimalAt = text.split(/दशमलव्?|decimal|point|पॉइंट|प्वाइंट/);
  if (decimalAt.length > 1) {
    if (decimalAt.length !== 2) return null;
    const whole = parseHindiInteger(decimalAt[0]);
    const fractionTokens = decimalAt[1].trim().split(/\s+/).filter((word) => !ignoredNumberWords.has(word) && !['kg', 'grams'].includes(word));
    const parts = fractionTokens.map((word) => /^\d+$/.test(word) ? word : spokenNumbers[word]);
    if (parts.some((value) => value === undefined || !/^\d+$/.test(String(value)))) return null;
    const fraction = parts.join('');
    return whole === null || !fraction ? null : Number(`${whole}.${fraction}`);
  }
  const value = parseHindiInteger(text);
  return value !== null && Number.isFinite(value) && value >= 0 ? value : null;
}

function parseSpokenPhone(raw) {
  const text = hindiDigits(raw).normalize('NFC').toLowerCase().replace(/[।.!?]+$/g, '').trim();
  const digitRun = text.replace(/\D/g, '');
  if (digitRun.length >= 10) return digitRun;
  const tokens = text.split(/\s+/).filter(Boolean);
  const spoken = tokens.filter((word) => Object.hasOwn(spokenNumbers, word) && spokenNumbers[word] >= 0 && spokenNumbers[word] <= 9);
  return spoken.length === tokens.length && spoken.length ? spoken.map((word) => spokenNumbers[word]).join('') : digitRun;
}

function spokenCommand(text) {
  const value = text.toLowerCase();
  if (/रुको|बंद करो|stop|cancel/.test(value)) return 'stop';
  if (/छोड़ो|छोड़ो|छोड़ें|अगला|skip|next/.test(value)) return 'skip';
  if (/दोहर|फिर से|repeat/.test(value)) return 'repeat';
  return '';
}

function voiceFields() {
  return [...document.querySelectorAll('#entry-form .input-wrap input:not(:disabled)')];
}

function refreshVoiceButton() {
  const button = document.getElementById('start-voice');
  button.innerHTML = voiceActive ? '<span>■</span> आवाज़ रोकें' : '<span>🎙️</span> हिन्दी में लगातार भरें';
  button.classList.toggle('voice-running', voiceActive);
  document.querySelectorAll('.field-voice').forEach((mic) => {
    const selected = voiceActive && mic.closest('.input-wrap').querySelector('input') === voiceTarget;
    mic.setAttribute('aria-pressed', String(selected));
  });
}

function stopVoice(message = 'वॉइस एंट्री रोक दी गई।') {
  voiceActive = false;
  voiceRun += 1;
  window.clearTimeout(voiceTimer);
  const previous = voiceRecognition;
  voiceRecognition = null;
  try { previous?.abort(); } catch { /* recognition may already be stopped */ }
  window.speechSynthesis?.cancel();
  document.querySelectorAll('.voice-listening').forEach((field) => field.classList.remove('voice-listening'));
  refreshVoiceButton();
  setVoiceStatus(message);
}

function nextVoiceField() {
  if (!voiceActive) return;
  voiceIndex += 1;
  while (voiceIndex < voiceQueue.length && (!voiceQueue[voiceIndex].isConnected || voiceQueue[voiceIndex].disabled)) voiceIndex += 1;
  if (voiceIndex >= voiceQueue.length) {
    voiceActive = false;
    refreshVoiceButton();
    setVoiceStatus('सभी बोलने वाले खाने पूरे हुए। जानकारी देख लें और फिर सेव करें।');
    speakHindi('सभी खाने पूरे हो गए। जानकारी देख लें और सेव करें।');
    return;
  }
  askVoiceField(voiceQueue[voiceIndex]);
}

function askVoiceField(input) {
  if (!voiceActive || !input?.isConnected || input.disabled) return;
  voiceRun += 1;
  window.clearTimeout(voiceTimer);
  const previous = voiceRecognition;
  voiceRecognition = null;
  try { previous?.abort(); } catch { /* previous question has ended */ }
  document.querySelectorAll('.voice-field-status').forEach((hint) => hint.remove());
  document.querySelectorAll('.voice-listening').forEach((field) => field.classList.remove('voice-listening'));
  voiceTarget = input;
  input.focus({ preventScroll: true });
  input.scrollIntoView?.({ block: 'center', behavior: 'smooth' });
  refreshVoiceButton();
  setVoiceStatus(`सवाल सुनें: ${inputLabel(input)}। “सुन रहा है” दिखने पर बोलें।`);
  const prompt = inputPrompt(input);
  const run = voiceRun;
  speakHindi(prompt, () => {
    if (voiceActive && run === voiceRun) listenForField(input, run);
  });
}

function retryVoiceField(input, run, message) {
  if (!voiceActive || run !== voiceRun || !input.isConnected) return;
  if (!guidedVoice) return stopVoice(message);
  setVoiceStatus(`${message} फिर से सुन रहा हूँ।`, false);
  speakHindi('कृपया इसी सवाल का जवाब फिर से बोलें।', () => {
    if (voiceActive && run === voiceRun) listenForField(input, run);
  });
}

function listenForField(input, run) {
  if (!voiceActive || run !== voiceRun || !input.isConnected || input.disabled) return;
  const recognition = new SpeechRecognitionAPI();
  voiceRecognition = recognition;
  recognition.lang = 'hi-IN';
  recognition.continuous = false;
  recognition.interimResults = false;
  recognition.maxAlternatives = 1;
  let receivedResult = false;
  let afterEnd = null;
  const current = () => voiceActive && run === voiceRun && voiceRecognition === recognition && input.isConnected;
  recognition.onstart = () => {
    if (!current()) return;
    setVoiceStatus('सुन रहा है… अब बोलें।', true);
  };
  recognition.onresult = (event) => {
    if (!current() || receivedResult) return;
    const result = event.results?.[event.resultIndex || 0];
    if (result?.isFinal === false) return;
    receivedResult = true;
    const transcript = result?.[0]?.transcript?.trim() || '';
    const command = spokenCommand(transcript);
    if (command === 'stop') return stopVoice();
    if (command === 'skip') {
      afterEnd = () => guidedVoice ? nextVoiceField() : stopVoice('यह खाना छोड़ दिया गया।');
    } else if (command === 'repeat') {
      afterEnd = () => askVoiceField(input);
    } else {
      const value = input.type === 'tel' ? parseSpokenPhone(transcript) : parseSpokenNumber(transcript);
      if (value === null || value === '' || (input.type === 'tel' && !/^\+?\d{10,15}$/.test(value))) {
        afterEnd = () => retryVoiceField(input, run, `सुना: “${transcript}”। संख्या समझ नहीं आई।`);
      } else {
        input.value = String(value);
        input.dispatchEvent(new Event('input', { bubbles: true }));
        input.dispatchEvent(new Event('change', { bubbles: true }));
        setVoiceStatus(`दर्ज किया: ${value}। सुना: “${transcript}”`);
        afterEnd = guidedVoice
          ? () => speakHindi('दर्ज हो गया।', () => { if (voiceActive && run === voiceRun) nextVoiceField(); })
          : () => stopVoice(`दर्ज किया: ${value}। सही हो तो सेव करें।`);
      }
    }
    try { recognition.stop(); } catch { /* onend will finish this question */ }
  };
  recognition.onerror = (event) => {
    if (!current()) return;
    if (event.error === 'no-speech' && guidedVoice) {
      afterEnd = () => retryVoiceField(input, run, 'आवाज़ नहीं मिली।');
      return;
    }
    const messages = {
      'no-speech': 'आवाज़ नहीं मिली। माइक दबाकर फिर बोलें।',
      'not-allowed': 'माइक्रोफ़ोन की अनुमति दें, फिर माइक दबाएँ।',
      'service-not-allowed': 'ब्राउज़र ने वॉइस सेवा रोक दी है। अनुमति जाँचें।',
      'audio-capture': 'माइक्रोफ़ोन नहीं मिला। उसका कनेक्शन जाँचें।',
      network: 'वॉइस सेवा से कनेक्शन नहीं हुआ। इंटरनेट जाँचकर फिर कोशिश करें।',
      'language-not-supported': 'इस ब्राउज़र की वॉइस सेवा में हिन्दी उपलब्ध नहीं है।',
    };
    stopVoice(messages[event.error] || 'वॉइस पहचान रुक गई। माइक दबाकर फिर कोशिश करें।');
  };
  recognition.onend = () => {
    if (!current()) return;
    window.clearTimeout(voiceTimer);
    voiceRecognition = null;
    if (afterEnd) afterEnd();
    else retryVoiceField(input, run, 'कोई जवाब नहीं मिला।');
  };
  try {
    recognition.start();
    voiceTimer = window.setTimeout(() => {
      if (!current()) return;
      voiceRecognition = null;
      try { recognition.abort(); } catch { /* service already stopped */ }
      if (afterEnd) afterEnd();
      else retryVoiceField(input, run, 'जवाब का समय पूरा हुआ।');
    }, 20000);
  } catch {
    stopVoice('माइक्रोफ़ोन शुरू नहीं हुआ। माइक दबाकर फिर कोशिश करें।');
  }
}

function startVoiceFor(input, guided = true) {
  if (voiceActive || voiceRecognition) stopVoice();
  if (!SpeechRecognitionAPI || !window.speechSynthesis) {
    setVoiceStatus('इस ब्राउज़र में हिन्दी वॉइस एंट्री उपलब्ध नहीं है। कृपया लिखकर भरें।');
    return;
  }
  voiceRun += 1;
  voiceActive = true;
  guidedVoice = guided;
  if (guided) {
    voiceQueue = voiceFields();
    voiceIndex = input ? voiceQueue.indexOf(input) : 0;
    if (voiceIndex < 0) return stopVoice('यह खाना अभी उपलब्ध नहीं है।');
    if (!voiceQueue.length) return stopVoice('भरने के लिए कोई खुला खाना नहीं है।');
    refreshVoiceButton();
    setVoiceStatus('हिन्दी वॉइस एंट्री शुरू हो रही है।');
    if (input) askVoiceField(input);
    else speakHindi('हिन्दी में जवाब दें। हर जवाब के बाद अगला सवाल अपने आप आएगा। छोड़ने के लिए अगला और रोकने के लिए रुको कहें।', () => askVoiceField(voiceQueue[voiceIndex]));
  } else {
    voiceQueue = [];
    refreshVoiceButton();
    askVoiceField(input);
  }
}

function setupVoiceButtons(root = document) {
  root.querySelectorAll('.input-wrap:not(.carried) input').forEach((input) => {
    if (input.dataset.voiceButton) return;
    input.dataset.voiceButton = 'true';
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'field-voice';
    button.setAttribute('aria-label', `${inputLabel(input)} से लगातार हिन्दी वॉइस एंट्री शुरू करें`);
    button.title = 'यहाँ से शुरू करें — अगले सवाल अपने आप आएँगे';
    button.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M12 14a3 3 0 0 0 3-3V6a3 3 0 1 0-6 0v5a3 3 0 0 0 3 3Z"/><path d="M18 11a6 6 0 0 1-12 0M12 17v4M8 21h8"/></svg>';
    button.addEventListener('click', (event) => {
      event.preventDefault();
      if (voiceActive && voiceTarget === input) stopVoice();
      else {
        if (voiceActive) stopVoice('पिछला सवाल रोक दिया गया।');
        startVoiceFor(input, true);
      }
    });
    input.closest('.input-wrap').append(button);
  });
}

document.querySelectorAll('[data-add-row]').forEach((button) => {
  button.addEventListener('click', () => addRepeatRow(button.dataset.addRow));
});
document.getElementById('start-voice').addEventListener('click', () => {
  if (voiceActive) stopVoice();
  else startVoiceFor(null, true);
});
document.querySelectorAll('[data-screen-link]').forEach((link) => {
  link.addEventListener('click', (event) => {
    event.preventDefault();
    const screen = link.dataset.screenLink;
    const previous = activeScreen;
    if (!showScreen(screen)) return;
    if (screen === 'entry' && previous !== 'entry') loadEntry();
  });
});
entryDate.addEventListener('change', () => {
  if (isDirty && !window.confirm('You have unsaved changes. Load a different date and discard them?')) {
    entryDate.value = loadedDate;
    return;
  }
  isDirty = false;
  loadEntry();
});
summaryDate.addEventListener('change', showSummary);
entryForm.addEventListener('submit', saveEntry);
entryForm.addEventListener('input', (event) => {
  if (event.target.matches('input:not(:disabled)')) event.target.dataset.autoSavePending = 'true';
  markDirty();
  updateQuickLook();
});
entryForm.addEventListener('focusout', (event) => {
  const input = event.target;
  if (!input.matches('input:not(:disabled)') || !input.dataset.autoSavePending) return;
  delete input.dataset.autoSavePending;
  window.setTimeout(() => {
    if (isDirty && !isSaving) saveEntry(null, { draft: true, autoSave: true });
  }, 0);
});
document.getElementById('entry-status').addEventListener('change', markDirty);
setupVoiceButtons();
updateSaveButton();

entryDate.value = localDateString();
summaryDate.value = entryDate.value;
document.querySelectorAll('input[type="number"]').forEach((input) => {
  input.min = '0';
  input.step = 'any';
});
const initialScreen = location.hash.replace('#', '');
if (['history', 'summary'].includes(initialScreen)) showScreen(initialScreen);
else loadEntry();
