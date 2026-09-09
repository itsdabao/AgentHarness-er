// Browser transport and observation only. No model, tool or storage execution.
export const ACTIVE = new Set(["queued", "running", "cancel_requested"]);

export function formatTimestamp(value, timeZone) {
  if (typeof value !== "string" || !value.trim()) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  const clock = new Intl.DateTimeFormat("en-US", {
    hour: "numeric", minute: "2-digit", hour12: true, timeZone
  }).format(date);
  const day = new Intl.DateTimeFormat("en-GB", {
    day: "2-digit", month: "2-digit", year: "numeric", timeZone
  }).format(date);
  const zone = new Intl.DateTimeFormat("en-US", {timeZone, timeZoneName: "shortOffset"})
    .formatToParts(date).find(part => part.type === "timeZoneName").value;
  return {clock, day, zone};
}

export class RPCError extends Error {
  constructor(code, uncertain = false) {
    super(code); this.code = code; this.uncertain = uncertain;
  }
}
export class RPCClient {
  constructor(fetcher = (...args) => fetch(...args)) {
    this.fetcher = fetcher; this.id = 0;
  }
  async call(method, params = {}, signal) {
    const id = ++this.id;
    const controller = new AbortController();
    const abort = () => controller.abort();
    if (signal?.aborted) abort();
    signal?.addEventListener("abort", abort, {once: true});
    const timer = setTimeout(abort, 10000);
    const mutation = ["submit_task", "create_session", "cancel_run"].includes(method);
    try {
      const response = await this.fetcher("/rpc", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({jsonrpc: "2.0", id, method, params}),
        signal: controller.signal, redirect: "error"
      });
      if (!response.ok) throw new RPCError("HTTP_ERROR", mutation);
      const body = await response.json();
      if (body.id !== id || body.jsonrpc !== "2.0")
        throw new RPCError("INVALID_RESPONSE", mutation);
      if (body.error) throw new RPCError(String(body.error.code));
      if (!body.result || typeof body.result.ok !== "boolean")
        throw new RPCError("INVALID_RESPONSE", mutation);
      if (!body.result.ok) throw new RPCError(body.result.error.code);
      return body.result.data;
    } catch (error) {
      if (error instanceof RPCError) throw error;
      throw new RPCError("CONNECTION_LOST", mutation);
    } finally {
      clearTimeout(timer); signal?.removeEventListener("abort", abort);
    }
  }
}
export function acceptPage(page, cursor) {
  if (!Array.isArray(page.events)) throw new RPCError("INVALID_EVENT_PAGE");
  const events = [...page.events].sort((a, b) => a.sequence - b.sequence);
  const accepted = [];
  for (const event of events) {
    if (!Number.isSafeInteger(event.sequence) || event.sequence < 1)
      throw new RPCError("INVALID_EVENT_SEQUENCE");
    if (event.sequence > cursor) {accepted.push(event); cursor = event.sequence;}
  }
  if (page.has_more && !accepted.length) throw new RPCError("EVENT_CURSOR_STALLED");
  return {events: accepted, cursor};
}
export class RunWatcher {
  constructor(client, receive, interval = 500) {
    this.client = client; this.receive = receive; this.interval = interval;
    this.cursors = new Map(); this.generation = 0; this.timer = null;
    this.abort = null; this.runId = null;
  }
  stop() {
    this.generation++; clearTimeout(this.timer); this.abort?.abort();
  }
  start(runId, cursor) {
    this.stop(); this.runId = runId;
    if (cursor !== undefined) this.cursors.set(runId, cursor);
    const generation = this.generation;
    this.abort = new AbortController();
    const signal = this.abort.signal;
    const current = () => generation === this.generation;
    const tick = async () => {
      try {
        const state = await this.client.call("get_run", {run_id: runId}, signal);
        if (!current()) return;
        this.receive("snapshot", state);
        while (current()) {
          const page = await this.client.call("list_run_events", {
            run_id: runId, after_sequence: this.cursors.get(runId) || 0
          }, signal);
          if (!current()) return;
          const accepted = acceptPage(page, this.cursors.get(runId) || 0);
          this.receive("events", accepted.events);
          this.cursors.set(runId, accepted.cursor);
          if (!page.has_more) break;
          await new Promise(resolve => setTimeout(resolve, 0));
        }
        if (!current()) return;
        if (!ACTIVE.has(state.status)) this.receive("terminal", state);
        else this.timer = setTimeout(tick, this.interval);
      } catch (error) {
        if (current()) this.receive("disconnected", error);
      }
    };
    return tick();
  }
}
const REASONS = {
  PROVIDER_RATE_LIMITED: "provider giới hạn tốc độ/quota",
  PROVIDER_TIMEOUT: "provider quá thời gian chờ",
  PROVIDER_UNAVAILABLE: "provider tạm không khả dụng",
  PROVIDER_CONNECTION_LOST: "mất kết nối provider",
  MCP_CONNECTION_LOST: "mất kết nối MCP", MCP_TIMEOUT: "MCP quá thời gian chờ"
};
export function progress(event) {
  const p = event.payload || {};
  if (p.retry_reason)
    return `Sẽ thử lại do ${REASONS[p.retry_reason] || "lỗi tạm thời đã được phân loại"}; sau ${p.retry_delay_ms ?? 0} ms, lần ${(p.attempt || 0) + 1}/${p.max_attempts ?? "?"}.`;
  const labels = {
    run_queued: "Đã nhận task, đang chờ chạy.", run_started: "Run bắt đầu.",
    run_cancel_requested: "Đã yêu cầu dừng, đang chờ xác nhận.",
    run_cancelled: "Run đã dừng.", run_completed: "Task hoàn tất.",
    run_failed: "Run thất bại.", run_limit_exceeded: "Run đã hết ngân sách thực thi.",
    run_interrupted: "Run bị gián đoạn; không tự chạy lại tool."
  };
  if (labels[event.type]) return labels[event.type];
  if (event.type === "model_request_started")
    return p.phase === "processing_tool_results"
      ? "Đang gửi kết quả tool cho model xử lý." : "Đang chờ model phản hồi.";
  if (event.type === "model_response_received")
    return p.error ? "Provider báo lỗi." : "Đã nhận phản hồi model.";
  const name = JSON.stringify(p.tool_name || "?");
  return ({
    tool_call_requested: `Model yêu cầu gọi ${name}.`,
    tool_execution_started: `Đang thực thi ${name}.`,
    tool_execution_completed: `Đã nhận kết quả ${name}.`,
    tool_execution_failed: `Thực thi ${name} không thành công.`
  })[event.type] || "Đã ghi nhận event.";
}
export function groupEvents(events) {
  const groups = new Map();
  for (const event of events) {
    const p = event.payload || {};
    const key = p.execution_id ? "tool:" + p.execution_id : "event:" + event.sequence;
    if (!groups.has(key)) groups.set(key, {key, name: p.tool_name, events: []});
    groups.get(key).events.push(event);
  }
  return [...groups.values()];
}
export function architectureStage(event) {
  if (event.payload?.record_type === "provider_attempt") return "provider";
  if (event.type.startsWith("tool_")) return "mcp";
  if (event.type.startsWith("model_")) return "loop";
  return "service";
}

export function applyMode(mode, doc, storage) {
  const detailed = mode === "detailed";
  doc.querySelectorAll(".detail-only").forEach(el => {el.hidden = !detailed;});
  doc.getElementById("simple-mode").setAttribute("aria-pressed", String(!detailed));
  doc.getElementById("detail-mode").setAttribute("aria-pressed", String(detailed));
  try {storage?.setItem("minder.view", detailed ? "detailed" : "simple");} catch {}
}
