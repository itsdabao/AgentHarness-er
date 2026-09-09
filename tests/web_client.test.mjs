import test from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {SessionPicker} from "../src/minder_harness/web/sessions.js";
import {pipelineEvent, PipelineSelection, PipelineView, COMPONENTS} from "../src/minder_harness/web/pipeline.js";
import {RPCClient, RPCError, RunWatcher, acceptPage, groupEvents, progress, applyMode, formatTimestamp}
  from "../src/minder_harness/web/client.js";

const event = (sequence, extra = {}) => ({sequence, type: "run_started", payload: {}, ...extra});
const page = (events, has_more = false) => ({events, has_more});

test("timestamps show compact local clock and day/month/year, preserving input", () => {
  const raw = "2026-09-08T11:26:32.540869+00:00";
  assert.deepEqual(formatTimestamp(raw, "Asia/Bangkok"), {
    clock: "6:26 PM", day: "08/09/2026", zone: "GMT+7"
  });
  assert.equal(raw, "2026-09-08T11:26:32.540869+00:00");
  assert.equal(formatTimestamp(raw).day, new Intl.DateTimeFormat("en-GB", {
    day: "2-digit", month: "2-digit", year: "numeric"
  }).format(new Date(raw)));
});

test("timestamps handle local date rollover, midnight/noon and absent or invalid values", () => {
  assert.deepEqual(formatTimestamp("2026-09-08T17:05:00Z", "Asia/Bangkok"), {
    clock: "12:05 AM", day: "09/09/2026", zone: "GMT+7"
  });
  assert.equal(formatTimestamp("2026-09-08T05:00:00Z", "Asia/Bangkok").clock, "12:00 PM");
  for (const value of [null, undefined, "", "invalid", 0])
    assert.equal(formatTimestamp(value), null);
});

function pickerDocument() {
  function element() {
    return {children: [], dataset: {}, attrs: {}, listeners: {}, hidden: true,
      textContent: "", disabled: false, focused: false,
      addEventListener(k, fn) {this.listeners[k] = fn;},
      setAttribute(k, v) {this.attrs[k] = v;},
      append(...children) {this.children.push(...children);},
      replaceChildren() {this.children = [];},
      querySelectorAll() {return this.children.map(row => row.children[0]);},
      contains(target) {return this === target || this.children.some(c => c.contains(target));},
      focus() {this.focused = true;}
    };
  }
  const elements = Object.fromEntries(["session-picker", "session-toggle", "session-menu",
    "session-list", "session-list-status", "session-more"].map(id => [id, element()]));
  return {elements, listeners: {}, getElementById: id => elements[id],
    createElement: element, addEventListener(k, fn) {this.listeners[k] = fn;}};
}
const sessionItem = id => ({session_id: id, title: "<img src=x onerror=alert(1)>",
  created_at: "2026-09-08T10:00:00Z", latest_run_id: "run_" + id, latest_run_status: "completed"});

test("session picker pages, deduplicates, selects real IDs and uses inert titles", async () => {
  const doc = pickerDocument(), calls = [], selected = [];
  const responses = [
    {sessions: [sessionItem("s3"), sessionItem("s2")], next_before: 2, has_more: true},
    {sessions: [sessionItem("s2"), sessionItem("s1")], next_before: 1, has_more: false}
  ];
  const picker = new SessionPicker(doc, {call: async (...args) => {calls.push(args); return responses.shift();}},
    item => selected.push(item));
  picker.setCurrent("s2"); await picker.open();
  assert.deepEqual(calls[0], ["list_sessions", {before: null, limit: 20}]);
  const list = doc.elements["session-list"];
  assert.equal(list.children[1].children[0].attrs["aria-current"], "true");
  assert.equal(list.children[0].children[0].children[0].textContent, sessionItem("s3").title);
  await picker.load();
  assert.deepEqual(calls[1], ["list_sessions", {before: 2, limit: 20}]);
  assert.equal(list.children.length, 3);
  assert.equal(doc.elements["session-more"].hidden, true);
  list.children[2].children[0].listeners.click();
  assert.equal(selected[0].session_id, "s1");
  assert.equal(selected[0].latest_run_id, "run_s1");
  assert.equal(doc.elements["session-menu"].hidden, true);
  assert.equal(doc.elements["session-toggle"].focused, true);
});

test("session picker keeps selection on failure and supports retry", async () => {
  const doc = pickerDocument(); let fail = true;
  const picker = new SessionPicker(doc, {call: async () => {
    if (fail) throw {code: "-32601"};
    return {sessions: [], next_before: null, has_more: false};
  }}, () => assert.fail("No selection on load"));
  picker.setCurrent("existing"); await picker.open();
  assert.equal(picker.current, "existing");
  assert.match(doc.elements["session-list-status"].textContent, /khởi động lại/);
  assert.equal(doc.elements["session-more"].hidden, false);
  fail = false; await picker.load();
  assert.match(doc.elements["session-list-status"].textContent, /Chưa có session/);
});

test("closing/reopening ignores stale session responses; Escape closes and restores focus", async () => {
  const doc = pickerDocument(), resolve = [];
  const picker = new SessionPicker(doc, {call: () => new Promise(r => resolve.push(r))}, () => {});
  const old = picker.open(); picker.close(); const fresh = picker.open();
  resolve[1]({sessions: [sessionItem("new")], next_before: 2, has_more: false}); await fresh;
  resolve[0]({sessions: [sessionItem("old")], next_before: 1, has_more: false}); await old;
  assert.deepEqual([...picker.items], ["new"]);
  doc.elements["session-picker"].listeners.keydown({key: "Escape"});
  assert.equal(doc.elements["session-menu"].hidden, true);
  assert.equal(doc.elements["session-toggle"].attrs["aria-expanded"], "false");
  assert.equal(doc.elements["session-toggle"].focused, true);
});

test("session picker rejects a stalled pagination cursor", async () => {
  const doc = pickerDocument();
  const picker = new SessionPicker(doc, {call: async () => ({
    sessions: [sessionItem("s2")], next_before: 2, has_more: true
  })}, () => {});
  await picker.open(); await picker.load();
  assert.equal(picker.items.size, 1);
  assert.match(doc.elements["session-more"].textContent, /Thử tải lại/);
});
test("pagination, duplicates, ordering and cursor stall", () => {
  assert.deepEqual(acceptPage(page([event(2), event(1), event(2)]), 0),
    {events: [event(1), event(2)], cursor: 2});
  assert.throws(() => acceptPage(page([event(1)], true), 1));
});
test("group attempt and logical outcome by execution, not tool name", () => {
  const payload = {execution_id: "x", tool_name: "read"};
  const groups = groupEvents([event(1, {payload}),
    event(2, {payload: {...payload, record_type: "attempt_result"}}),
    event(3, {payload: {...payload, execution_id: "y"}})]);
  assert.equal(groups.length, 2); assert.equal(groups[0].events.length, 2);
});
test("only explicit retry metadata renders a retry", () => {
  assert.match(progress(event(1, {payload: {retry_reason: "PROVIDER_TIMEOUT", attempt: 1,
    max_attempts: 2, retry_delay_ms: 100}})), /100 ms.*2\/2/);
  assert.doesNotMatch(progress(event(1, {payload: {text: "retry now"}})), /Sẽ thử lại/);
});
test("mode toggle affects presentation only; unavailable storage is harmless", () => {
  const nodes = [{hidden: true}], attrs = {};
  const doc = {querySelectorAll: () => nodes, getElementById: id => ({
    setAttribute: (k, v) => {attrs[id] = v;}
  })};
  const storage = {setItem() {throw Error("denied");}};
  applyMode("detailed", doc, storage);
  assert.equal(nodes[0].hidden, false); assert.equal(attrs["detail-mode"], "true");
  applyMode("simple", doc, storage);
  assert.equal(nodes[0].hidden, true);
});
test("ambiguous submit fails once, never retries", async () => {
  let count = 0;
  const rpc = new RPCClient(async () => {count++; throw Error("secret");});
  await assert.rejects(rpc.call("submit_task", {}), e =>
    e.uncertain && e.code === "CONNECTION_LOST" && !e.message.includes("secret"));
  assert.equal(count, 1);
});
test("confirmed operation errors are not ambiguous", async () => {
  const rpc = new RPCClient(async (_, options) => ({ok: true, json: async () => ({
    jsonrpc: "2.0", id: JSON.parse(options.body).id,
    result: {ok: false, data: null, error: {code: "CONFLICT"}}
  })}));
  await assert.rejects(rpc.call("submit_task"), e => e instanceof RPCError && !e.uncertain);
});
test("malformed identity on mutation is unconfirmed", async () => {
  const rpc = new RPCClient(async () => ({ok: true, json: async () => ({id: 999})}));
  await assert.rejects(rpc.call("create_session"), e => e.uncertain);
});
test("terminal status drains remaining pages", async () => {
  const values = [{status: "completed", run_id: "a"}, page([event(1)], true), page([event(2)])];
  const received = [];
  const watcher = new RunWatcher({call: async () => values.shift()},
    (kind, data) => received.push([kind, data]));
  await watcher.start("a");
  assert.deepEqual(received.map(x => x[0]), ["snapshot", "events", "events", "terminal"]);
  assert.equal(watcher.cursors.get("a"), 2);
  watcher.stop();
});
test("run switch discards delayed old response", async () => {
  let release;
  const old = new Promise(resolve => {release = resolve;});
  const received = [];
  const watcher = new RunWatcher({call: async (method, params) => {
    if (params.run_id === "old") return old;
    return method === "get_run" ? {status: "completed", run_id: "new"} : page([]);
  }}, (kind, value) => received.push([kind, value]));
  const waiting = watcher.start("old");
  await watcher.start("new");
  release({status: "running", run_id: "old"}); await waiting;
  assert.ok(received.filter(x => x[0] === "snapshot").every(x => x[1].run_id === "new"));
  watcher.stop();
});
test("disconnect stops polling and reattach retains cursor", async () => {
  const requests = [], kinds = [];
  let broken = true;
  const watcher = new RunWatcher({call: async (method, params) => {
    requests.push([method, params]);
    if (broken) throw Error("offline");
    return method === "get_run" ? {status: "completed", run_id: "a"} : page([event(5)]);
  }}, kind => kinds.push(kind));
  watcher.cursors.set("a", 4);
  await watcher.start("a"); assert.deepEqual(kinds, ["disconnected"]);
  broken = false; await watcher.start("a");
  assert.equal(requests.at(-1)[1].after_sequence, 4);
  assert.equal(watcher.cursors.get("a"), 5); watcher.stop();
});
test("watch stop does not cancel server run", async () => {
  const methods = [];
  const watcher = new RunWatcher({call: async method => {
    methods.push(method);
    return method === "get_run" ? {status: "running", run_id: "a"} : page([]);
  }}, () => {});
  await watcher.start("a"); watcher.stop();
  assert.deepEqual(methods, ["get_run", "list_run_events"]);
});
test("UI uses text rendering; mode function does not call RPC", () => {
  const source = readFileSync(new URL("../src/minder_harness/web/app.js", import.meta.url), "utf8");
  assert.doesNotMatch(source, /innerHTML|outerHTML|insertAdjacentHTML|eval\(/);
  assert.doesNotMatch(applyMode.toString(), /rpc|fetch|watcher|submit_task/);
});

test("pipeline has separate model/tool branches and a feedback path", () => {
  const model = pipelineEvent(event(1, {type: "model_request_started",
    payload: {record_type: "provider_attempt", phase: "dispatch_intent"}}));
  assert.ok(model.nodes.includes("provider"));
  assert.ok(!model.nodes.includes("mcp"));
  assert.deepEqual(model.edges, ["model-request", "provider-llm"]);
  assert.match(model.label, /ý định/);
  const returned = pipelineEvent(event(2, {type: "tool_execution_completed",
    payload: {record_type: "attempt_result"}}));
  assert.deepEqual(returned.edges, ["tool-result", "server-mcp"]);
  const next = pipelineEvent(event(3, {type: "model_request_started",
    payload: {phase: "processing_tool_results"}}));
  assert.deepEqual(next.edges, ["tool-result", "model-request"]);
});
test("pipeline distinguishes error, retry, cancellation and success", () => {
  assert.equal(pipelineEvent(event(1, {type: "model_response_received",
    payload: {error: {code: "PROVIDER_ERROR"}}})).tone, "error");
  assert.equal(pipelineEvent(event(2, {type: "model_response_received",
    payload: {error: {code: "PROVIDER_TIMEOUT"}, retry_scheduled: true}})).tone, "waiting");
  assert.equal(pipelineEvent(event(3, {type: "tool_execution_failed",
    payload: {error: {code: "TOOL_CANCELLED"}}})).tone, "cancelled");
  assert.equal(pipelineEvent(event(4, {type: "run_completed"})).tone, "success");
  assert.equal(pipelineEvent(null).tone, "neutral");
});
test("selected historical event is pinned while latest events continue", () => {
  const state = new PipelineSelection();
  state.update(event(1)); state.select(event(1)); state.update(event(9));
  assert.equal(state.event.sequence, 1); assert.match(state.caption, /đã chọn #1/);
  state.follow(); assert.equal(state.event.sequence, 9); assert.match(state.caption, /mới nhất/);
});
test("paused/disconnected observations are never labeled live", () => {
  const state = new PipelineSelection();
  state.update(event(4)); state.observation = "disconnected";
  assert.match(state.caption, /Mất kết nối/);
  state.observation = "paused"; assert.match(state.caption, /dừng xem/);
  state.reset(); assert.equal(state.event, null); assert.match(state.caption, /Chưa có/);
});
test("all pipeline component and edge references exist in the diagram", () => {
  const html = readFileSync(new URL("../src/minder_harness/web/index.html", import.meta.url), "utf8");
  for (const name of Object.keys(COMPONENTS)) assert.ok(html.includes('data-stage="' + name + '"'));
  const samples = [
    event(1, {type: "run_queued"}), event(2, {type: "run_completed"}),
    event(3, {type: "model_request_started", payload: {record_type: "provider_attempt"}}),
    event(4, {type: "model_response_received", payload: {record_type: "provider_attempt"}}),
    event(5, {type: "tool_execution_started", payload: {record_type: "attempt_intent"}}),
    event(6, {type: "tool_execution_completed", payload: {record_type: "attempt_result"}})
  ];
  for (const sample of samples) for (const edge of pipelineEvent(sample).edges)
    assert.ok(html.includes('data-edge="' + edge + '"'), edge);
  assert.ok(!html.includes('data-edge="llm-tool"'));
});
function fakePipelineDocument() {
  function element(dataset = {}) {
    const classes = new Set();
    return {dataset, children: [], textContent: "", attrs: {}, listeners: {},
      classList: {toggle(name, enabled) {enabled ? classes.add(name) : classes.delete(name);},
        contains(name) {return classes.has(name);}},
      addEventListener(name, fn) {this.listeners[name] = fn;},
      setAttribute(k, v) {this.attrs[k] = v;},
      replaceChildren() {this.children = []; this.textContent = "";},
      append(child) {this.children.push(child);}
    };
  }
  const nodes = Object.keys(COMPONENTS).map(stage => element({stage}));
  const edges = ["model-request", "tool-request", "tool-result", "store-write"].map(edge => element({edge}));
  const elements = Object.fromEntries(["architecture", "architecture-latest", "architecture-note",
    "architecture-context", "architecture-events"].map(id => [id, element()]));
  elements.architecture.querySelectorAll = selector => selector === "[data-stage]" ? nodes : edges;
  return {nodes, elements, getElementById: id => elements[id], createElement: () => element()};
}
test("pipeline node click explains responsibilities and opens related events", () => {
  const doc = fakePipelineDocument(), selected = [];
  const view = new PipelineView(doc, e => {selected.push(e); view.select(e);});
  view.update([event(1, {type: "tool_execution_started", payload: {record_type: "attempt_intent"}})]);
  doc.nodes.find(n => n.dataset.stage === "mcp").listeners.click();
  assert.match(doc.elements["architecture-note"].textContent, /MCP executor/);
  doc.elements["architecture-events"].children[0].listeners.click();
  assert.equal(selected[0].sequence, 1);
  assert.match(doc.elements["architecture-context"].textContent, /đã chọn #1/);
  view.update([event(2, {type: "run_completed"})]);
  assert.match(doc.elements["architecture-context"].textContent, /đã chọn #1/);
  doc.elements["architecture-latest"].listeners.click();
  assert.match(doc.elements["architecture-context"].textContent, /mới nhất.*#2/);
  view.reset(); assert.equal(doc.elements.architecture.dataset.tone, "neutral");
});
test("pipeline is display-only and treats SQLite as recorded, not a failed operation", () => {
  const source = readFileSync(new URL("../src/minder_harness/web/pipeline.js", import.meta.url), "utf8");
  assert.doesNotMatch(source, /innerHTML|fetch\(|\.call\(|submit_task|cancel_run/);
  const doc = fakePipelineDocument(), view = new PipelineView(doc, () => {});
  view.update([event(1, {type: "run_failed", payload: {error: {code: "PROVIDER_ERROR"}}})]);
  const store = doc.nodes.find(n => n.dataset.stage === "store");
  assert.ok(store.classList.contains("recorded"));
  assert.ok(!store.classList.contains("selected"));
});
