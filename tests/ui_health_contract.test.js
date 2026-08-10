'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const htmlPath = path.join(__dirname, '..', 'docs', 'ui2-live.html');
const html = fs.readFileSync(htmlPath, 'utf8');
const scriptMatch = html.match(/<script>([\s\S]*)<\/script>/);
assert.ok(scriptMatch, 'dashboard inline script was not found');
const dashboardScript = scriptMatch[1];
const elementIds = new Set(Array.from(html.matchAll(/\bid="([^"]+)"/g), match => match[1]));

class ClassList {
  constructor() {
    this.values = new Set();
  }

  add(...names) {
    names.forEach(name => this.values.add(name));
  }

  remove(...names) {
    names.forEach(name => this.values.delete(name));
  }

  toggle(name, force) {
    const enabled = force === undefined ? !this.values.has(name) : Boolean(force);
    if (enabled) this.values.add(name);
    else this.values.delete(name);
    return enabled;
  }

  contains(name) {
    return this.values.has(name);
  }
}

class ElementStub {
  constructor() {
    this.classList = new ClassList();
    this.style = {};
    this.dataset = {};
    this.value = '';
    this.checked = false;
    this.className = '';
    this._textContent = '';
    this._innerHTML = '';
    this.children = [];
  }

  set textContent(value) {
    this._textContent = String(value);
  }

  get textContent() {
    return this._textContent;
  }

  set innerHTML(value) {
    this._innerHTML = String(value);
  }

  get innerHTML() {
    return this._innerHTML;
  }

  addEventListener() {}
  focus() {}
  closest() { return null; }
  querySelector() { return null; }
  querySelectorAll() { return []; }
  appendChild(child) { this.children.push(child); return child; }
}

function buildElements() {
  return new Map(Array.from(elementIds, id => [id, new ElementStub()]));
}

let elements = buildElements();
const document = {
  getElementById(id) {
    return elements.get(id) || null;
  },
  createElement() {
    return new ElementStub();
  },
  querySelectorAll() {
    return [];
  },
  addEventListener() {},
};

const healthPayload = {
  status: 'ok',
  notifications_muted: false,
  active_runs: 2,
  unacked_incidents: 3,
  uptime_seconds: 65,
};

async function fetchStub(url) {
  let payload;
  if (url === '/v1/health') payload = healthPayload;
  else if (url === '/v1/timers') payload = {timers: []};
  else if (url === '/v1/runs?limit=20') payload = {runs: []};
  else throw new Error(`unexpected dashboard request: ${url}`);
  return {json: async () => payload};
}

const context = vm.createContext({
  console,
  confirm: () => true,
  document,
  fetch: fetchStub,
  setInterval: () => 1,
  clearInterval: () => {},
  setTimeout: () => 1,
  clearTimeout: () => {},
  window: {scrollTo() {}},
});

vm.runInContext(dashboardScript, context);

function snapshot() {
  return {
    incidents: document.getElementById('metricIncidents'),
    incidentsCard: document.getElementById('metricIncidentsCard'),
    incidentsSub: document.getElementById('metricIncidentsSub'),
    uptime: document.getElementById('metricUptime'),
    uptimeSub: document.getElementById('metricUptimeSub'),
    banner: document.getElementById('incidentBanner'),
    incidentText: document.getElementById('incidentText'),
  };
}

function render(health) {
  elements = buildElements();
  context.healthUnderTest = health;
  vm.runInContext('renderHealthMetrics(healthUnderTest)', context);
  delete context.healthUnderTest;
  return snapshot();
}

async function run() {
  for (const id of [
    'metricIncidents',
    'metricIncidentsCard',
    'metricIncidentsSub',
    'metricUptime',
    'metricUptimeSub',
    'incidentBanner',
    'incidentText',
  ]) {
    assert.ok(elementIds.has(id), `dashboard DOM is missing #${id}`);
  }

  await vm.runInContext('loadDashboard()', context);
  let view = snapshot();
  assert.equal(view.incidents.textContent, '3');
  assert.equal(view.uptime.textContent, '1m');

  view = render({unacked_incidents: 3, uptime_seconds: 65});
  assert.equal(view.incidents.textContent, '3');
  assert.equal(view.incidentsSub.textContent, 'Unacknowledged');
  assert.equal(view.incidentsCard.classList.contains('danger'), true);
  assert.equal(view.banner.classList.contains('hidden'), false);
  assert.equal(view.incidentText.innerHTML, '<strong>3 unacknowledged incidents</strong> require attention');
  assert.equal(view.uptime.textContent, '1m');
  assert.match(view.uptimeSub.textContent, /^Since /);

  view = render({unacked_incidents: 0, uptime_seconds: 0});
  assert.equal(view.incidents.textContent, '0');
  assert.equal(view.incidentsSub.textContent, 'None');
  assert.equal(view.incidentsCard.classList.contains('danger'), false);
  assert.equal(view.banner.classList.contains('hidden'), true);
  assert.equal(view.uptime.textContent, '0s');
  assert.match(view.uptimeSub.textContent, /^Since /);

  view = render({unacked_incidents: 1, uptime_seconds: 3600});
  assert.equal(view.incidentText.innerHTML, '<strong>1 unacknowledged incident</strong> requires attention');
  assert.equal(view.uptime.textContent, '1h');

  for (const invalid of [undefined, null, -1, '7', Number.NaN, Number.POSITIVE_INFINITY]) {
    view = render({unacked_incidents: invalid, uptime_seconds: invalid});
    assert.equal(view.incidents.textContent, 'Unavailable');
    assert.equal(view.incidentsSub.textContent, 'Runner did not report incidents');
    assert.equal(view.incidentsCard.classList.contains('danger'), false);
    assert.equal(view.banner.classList.contains('hidden'), true);
    assert.equal(view.uptime.textContent, 'Unavailable');
    assert.equal(view.uptimeSub.textContent, 'Runner did not report uptime');
  }

  view = render({unacked_incidents: 1.5, uptime_seconds: 1.5});
  assert.equal(view.incidents.textContent, 'Unavailable');
  assert.equal(view.banner.classList.contains('hidden'), true);
  assert.equal(view.uptime.textContent, '2s');

  view = render({unacknowledged_incidents: 7, uptime_seconds: 7});
  assert.equal(view.incidents.textContent, 'Unavailable');
  assert.equal(view.banner.classList.contains('hidden'), true);
}

run().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
