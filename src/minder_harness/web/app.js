import {ACTIVE, RPCClient, RunWatcher, progress, groupEvents, applyMode, formatTimestamp} from "./client.js";
import {PipelineView} from "./pipeline.js";
import {SessionPicker} from "./sessions.js";

const $ = id => document.getElementById(id);
const rpc = new RPCClient();
let sessionId = null, runId = null, snapshot = null, events = [], jsonValue = {};
let busy = false, cancelPending = false, discarded = 0;
let sessionTitle = "";
const STATUS = {
  queued: "Đang chờ", running: "Đang chạy", cancel_requested: "Đang yêu cầu dừng",
  completed: "Hoàn tất", failed: "Thất bại", cancelled: "Đã dừng",
  interrupted: "Bị gián đoạn", limit_exceeded: "Đã chạm giới hạn"
};
function notice(message, bad = false) {
  $("notice").textContent = message;
  $("notice").classList.toggle("bad", bad);
}
function setConnected(value) {
  $("connection").textContent = value ? "● Đã kết nối RPC" : "○ Mất kết nối / chưa xác nhận";
}
function buttons() {
  for (const id of ["new-session", "session-toggle", "watch-run"]) $(id).disabled = busy;
  $("submit-task").disabled = busy || !sessionId || Boolean(snapshot && ACTIVE.has(snapshot.status));
  $("cancel-run").disabled = cancelPending || !runId || !snapshot || !ACTIVE.has(snapshot.status);
}
function showJSON(value, label) {
  jsonValue = value;
  $("json-value").textContent = JSON.stringify(value, null, 2);
  $("json-label").textContent = label;
}
function displaySnapshot(value) {
  snapshot = value; runId = value.run_id;
  showSession(value.session_id);
  $("run-label").textContent = runId;
  $("status").textContent = STATUS[value.status] || value.status;
  $("result").textContent = value.output == null
    ? (value.status === "cancelled" ? "Run đã dừng. Không có câu trả lời cuối. Điều này không đảm bảo tool từ xa đã rollback."
      : "Chưa có kết quả cuối được server xác nhận.")
    : (typeof value.output === "string" ? value.output : JSON.stringify(value.output, null, 2));
  $("run-error").textContent = value.error ? value.error.message : "";
  const fields = {
    "Bước model": value.usage?.model_steps, "Tool calls": value.usage?.tool_calls,
    "Input tokens (ghi nhận)": value.usage?.input_tokens,
    "Output tokens (ghi nhận)": value.usage?.output_tokens,
    "Bắt đầu": value.started_at, "Kết thúc": value.finished_at,
    "Giới hạn bước": value.limits?.max_steps, "Deadline (giây)": value.limits?.timeout_seconds
  };
  $("usage").replaceChildren();
  for (const [name, data] of Object.entries(fields)) {
    const box = document.createElement("div"), dt = document.createElement("dt"), dd = document.createElement("dd");
    dt.textContent = name; dd.textContent = data == null ? "Chưa có" : String(data);
    if (name === "Bắt đầu" || name === "Kết thúc") {
      const stamp = formatTimestamp(data);
      dd.textContent = data == null ? "Chưa có" : "Không hợp lệ";
      if (stamp) {
        const time = document.createElement("time");
        const clock = document.createElement("span"), day = document.createElement("span");
        time.className = "timestamp"; time.dateTime = data;
        time.title = stamp.zone + " · " + data;
        time.setAttribute("aria-label", stamp.clock + ", " + stamp.day + ", " + stamp.zone);
        clock.className = "timestamp-clock"; clock.textContent = stamp.clock;
        day.className = "timestamp-date"; day.textContent = stamp.day;
        time.append(clock, day); dd.replaceChildren(time);
      }
    }
    box.append(dt, dd); $("usage").append(box);
  }
  buttons();
}
function selectEvent(event) {
  showJSON(event, "Event #" + event.sequence + " · " + event.type);
  pipeline.select(event);
}
const pipeline = new PipelineView(document, selectEvent);
const picker = new SessionPicker(document, rpc, item => action("Mở session", async () => {
  const result = await rpc.call("get_session", {session_id: item.session_id});
  clearRun(); showSession(result.session_id, item.title);
  notice("Đã mở session cũ. Task tiếp theo sẽ tạo một run mới.");
  if (item.latest_run_id) selectRun(item.latest_run_id);
}));
function showSession(id, title) {
  if (id !== sessionId) sessionTitle = "";
  sessionId = id;
  sessionTitle = title || picker.names.get(id) || sessionTitle || "Phiên …" + id.slice(-6);
  picker.setCurrent(id, sessionTitle);
  $("session-label").textContent = sessionTitle;
  $("session-reference").textContent = id;
}
function drawTimeline() {
  const open = new Set([...$("timeline").querySelectorAll("details[open]")].map(el => el.dataset.key));
  $("timeline").replaceChildren();
  $("event-count").textContent = events.length + " events" + (discarded ? " · chỉ giữ 300 gần nhất" : "");
  for (const group of groupEvents(events)) {
    if (group.key.startsWith("tool:")) {
      const details = document.createElement("details"), summary = document.createElement("summary");
      details.className = "event-group"; details.dataset.key = group.key;
      details.open = open.has(group.key);
      summary.textContent = group.name + " · " + group.events.length + " events / một execution";
      details.append(summary);
      for (const event of group.events) details.append(eventButton(event));
      $("timeline").append(details);
    } else {
      $("timeline").append(eventButton(group.events[0]));
    }
  }
}
function eventButton(event) {
  const button = document.createElement("button");
  button.className = "event-button";
  const full = event.sequence + " · " + progress(event);
  button.textContent = full.length > 240 ? full.slice(0, 240) + "… [mở JSON để xem đủ]" : full;
  button.addEventListener("click", () => selectEvent(event));
  return button;
}
const watcher = new RunWatcher(rpc, (kind, data) => {
  if (kind === "disconnected") {
    pipeline.observe("disconnected");
    setConnected(false);
    notice("Đã ngừng theo dõi do lỗi kết nối/RPC. Trạng thái cuối được giữ; bấm Theo dõi để kết nối lại.", true);
    return;
  }
  setConnected(true);
  pipeline.observe("observing");
  if (kind === "snapshot" || kind === "terminal") displaySnapshot(data);
  if (kind === "events" && data.length) {
    events.push(...data);
    if (events.length > 300) {discarded += events.length - 300; events = events.slice(-300);}
    $("progress").textContent = progress(data.at(-1));
    drawTimeline();
    pipeline.update(events);
  }
  if (kind === "terminal") {
    $("progress").textContent = STATUS[data.status] || data.status;
    notice("Run đã kết thúc: " + (STATUS[data.status] || data.status) + ". Lịch sử đã được đọc hết.");
  }
});
function selectRun(id) {
  if (id !== runId) {
    watcher.stop(); runId = id; snapshot = null; events = []; discarded = 0;
    pipeline.reset();
    $("timeline").replaceChildren(); $("event-count").textContent = "0 events";
    $("run-label").textContent = id; $("result").textContent = "Đang đọc trạng thái run…";
    $("run-error").textContent = ""; $("status").textContent = "Đang xác nhận";
    $("usage").replaceChildren(); showJSON({}, "Chưa chọn event.");
  }
  $("run-id").value = id;
  void watcher.start(id);
}
function clearRun() {
  watcher.stop(); runId = null; snapshot = null; events = []; discarded = 0;
  pipeline.reset();
  $("timeline").replaceChildren(); $("event-count").textContent = "0 events";
  $("run-label").textContent = "—"; $("run-id").value = "";
  $("status").textContent = "Chưa có run"; $("progress").textContent = "Sẵn sàng nhận nhiệm vụ.";
  $("result").textContent = "Chưa có kết quả."; $("run-error").textContent = "";
  $("usage").replaceChildren(); showJSON({}, "Chưa chọn event.");
}
function handleError(error, operation) {
  if (["CONNECTION_LOST", "HTTP_ERROR", "INVALID_RESPONSE"].includes(error.code)) setConnected(false);
  const codes = {
    CONFLICT: "Session đang có run hoạt động. Không gửi lại task.",
    NOT_FOUND: "Không tìm thấy ID trên server.",
    VALIDATION_ERROR: "Dữ liệu không hợp lệ hoặc vượt giới hạn.",
    RUNTIME_UNAVAILABLE: "Runtime không thể nhận việc lúc này."
  };
  notice(error.uncertain
    ? operation + ": chưa xác nhận kết quả. Request có thể đã tới server; không tự gửi lại."
    : (codes[error.code] || operation + " không thành công. Mã: " + (error.code || "CLIENT_ERROR")), true);
}
async function action(operation, fn) {
  if (busy) return;
  busy = true; buttons();
  try {await fn(); setConnected(true);}
  catch (error) {handleError(error, operation);}
  finally {busy = false; buttons();}
}
$("new-session").addEventListener("click", () => action("Tạo session", async () => {
  const result = await rpc.call("create_session");
  picker.close(); clearRun(); showSession(result.session_id, "Phiên mới");
  notice("Session mới đã sẵn sàng.");
}));
$("task-form").addEventListener("submit", event => {
  event.preventDefault();
  void action("Gửi task", async () => {
    const content = $("task-input").value.trim();
    if (!sessionId || !content) {notice("Chọn session và nhập nhiệm vụ.", true); return;}
    const result = await rpc.call("submit_task", {session_id: sessionId, content});
    if (sessionTitle === "Phiên mới" || sessionTitle === "Phiên chưa có nhiệm vụ")
      showSession(sessionId, content.replace(/\s+/g, " ").slice(0, 80));
    notice("Server đã nhận task. Run: " + result.run_id);
    selectRun(result.run_id); displaySnapshot(result);
  });
});
$("cancel-run").addEventListener("click", async () => {
  if (!runId || cancelPending) return;
  const id = runId; cancelPending = true; buttons();
  try {
    const result = await rpc.call("cancel_run", {run_id: id});
    if (id !== runId) return;
    displaySnapshot(result);
    notice(result.status === "cancel_requested" ? "Đã yêu cầu dừng, đang chờ xác nhận."
      : "Server xác nhận: " + (STATUS[result.status] || result.status));
    void watcher.start(id);
  } catch (error) {handleError(error, "Cancel");}
  finally {cancelPending = false; buttons();}
});
$("watch-run").addEventListener("click", () => {
  const id = $("run-id").value.trim();
  if (!id) {notice("Nhập run ID.", true); return;}
  selectRun(id); buttons();
});
$("stop-watch").addEventListener("click", () => {
  pipeline.observe("paused");
  watcher.stop(); notice("Đã dừng quan sát. Không gửi yêu cầu cancel đến server.");
});
$("history-page").addEventListener("click", () => action("Đọc lịch sử", async () => {
  const id = $("run-id").value.trim() || runId;
  const cursor = Number($("after-sequence").value);
  if (!id || !Number.isSafeInteger(cursor) || cursor < 0) {notice("Kiểm tra run ID và cursor.", true); return;}
  const page = await rpc.call("list_run_events", {run_id: id, after_sequence: cursor, limit: 100});
  showJSON(page, "Trang lịch sử " + id + " · sau " + cursor);
  $("after-sequence").value = page.next_after_sequence;
  $("history-notice").textContent = page.has_more ? "Còn trang tiếp theo; bấm Đọc trang." : "Đã tới cuối lịch sử.";
}));
function setMode(mode) {
  let storage;
  try {storage = localStorage;} catch {}
  applyMode(mode, document, storage);
}
$("simple-mode").addEventListener("click", () => setMode("simple"));
$("detail-mode").addEventListener("click", () => setMode("detailed"));
$("snapshot-json").addEventListener("click", () => showJSON(snapshot || {}, "Run snapshot cuối đã xác nhận"));
async function copy(value) {
  try {await navigator.clipboard.writeText(value); notice("Đã copy.");}
  catch {notice("Trình duyệt không cho copy tự động; hãy chọn và copy nội dung.", true);}
}
$("copy-json").addEventListener("click", () => copy(JSON.stringify(jsonValue, null, 2)));
$("copy-run").addEventListener("click", () => runId && copy(runId));
window.addEventListener("pagehide", () => watcher.stop());
let preference = "simple";
try {preference = localStorage.getItem("minder.view") || "simple";} catch {}
setMode(preference); buttons();
fetch("/health", {signal: AbortSignal.timeout(10000)})
  .then(response => {setConnected(response.ok); if (!response.ok) notice("Server chưa sẵn sàng.", true);})
  .catch(() => setConnected(false));
