import {progress} from "./client.js";

// Associations explain architecture; they do not assert per-function execution.
export const COMPONENTS = {
  client: ["CLI / Web UI", "Nhập nhiệm vụ, gửi RPC và trình bày dữ liệu; không tự gọi model hoặc tool."],
  rpc: ["HTTP RPC", "Validate request và chuyển cho AgentService. Client disconnect không tự cancel run."],
  service: ["AgentService", "Quản lý session/run, admission, cancellation và recovery sau restart."],
  loop: ["AgentHarness / Loop", "Giới hạn các bước; gửi context tới model, thực thi tool được yêu cầu, rồi đưa kết quả vào bước model tiếp theo."],
  provider: ["Provider adapter", "Chuyển định dạng API, áp dụng timeout/retry và trả ModelResponse. Dispatch intent được ghi trước khi gửi request."],
  llm: ["LLM", "Trả câu trả lời hoặc yêu cầu gọi tool; không kết nối trực tiếp đến MCP. Event không tiết lộ suy nghĩ nội bộ."],
  mcp: ["MCP executor", "Kiểm tra tool/arguments, áp dụng deadline/cancel, ghi intent và outcome của attempt."],
  tool: ["MCP server / Tools", "Thực thi tool bên ngoài loop. Cancel tại client không chứng minh tool từ xa đã dừng hoặc rollback."],
  store: ["SQLite", "Lưu trạng thái, messages và events. Event đang xem đã được đọc từ kho lưu; không có nghĩa SQLite đang ghi ngay lúc này."]
};

export function pipelineEvent(event) {
  if (!event) return {nodes: [], edges: [], tone: "neutral", label: "Chưa có event"};
  const p = event.payload || {}, kind = event.type;
  let nodes = [], edges = [], tone = "neutral";
  if (kind === "run_queued") {
    nodes = ["client", "rpc", "service"]; edges = ["client-rpc", "rpc-service", "store-write"];
  } else if (kind.startsWith("run_")) {
    nodes = ["service", "loop"]; edges = ["service-loop", "store-write"];
  } else if (kind.startsWith("model_")) {
    nodes = ["loop"];
    if (p.record_type === "provider_attempt") {
      nodes.push("provider", "llm");
      edges = kind === "model_request_started"
        ? ["model-request", "provider-llm"] : ["llm-provider", "model-result"];
    } else {
      edges = p.phase === "processing_tool_results" ? ["tool-result", "model-request"] : [];
    }
  } else if (kind.startsWith("tool_")) {
    nodes = ["loop", "mcp"];
    if (p.record_type === "attempt_intent" || p.record_type === "attempt_result") nodes.push("tool");
    edges = kind === "tool_call_requested" || kind === "tool_execution_started"
      ? ["tool-request", ...(p.record_type === "attempt_intent" ? ["mcp-server"] : [])]
      : ["tool-result", ...(p.record_type === "attempt_result" ? ["server-mcp"] : [])];
  }
  const code = p.error?.code || p.validation_error?.code;
  if (kind === "run_cancelled" || code === "TOOL_CANCELLED") tone = "cancelled";
  else if (kind === "run_cancel_requested" || p.retry_scheduled || kind === "run_queued") tone = "waiting";
  else if (p.error || p.validation_error || /failed|limit_exceeded|interrupted/.test(kind)) tone = "error";
  else if (/completed|response_received/.test(kind)) tone = "success";
  else if (/started|requested/.test(kind)) tone = "active";
  const label = p.phase === "dispatch_intent" || p.record_type === "attempt_intent"
    ? "Đã ghi ý định gửi request; chưa phải xác nhận kết quả."
    : progress(event);
  return {nodes, edges, tone, label};
}

export class PipelineSelection {
  constructor() {this.reset();}
  reset() {
    this.latest = null; this.pinned = null; this.observation = "observing";
  }
  update(event) {if (event) this.latest = event;}
  select(event) {this.pinned = event;}
  follow() {this.pinned = null;}
  get event() {return this.pinned || this.latest;}
  get caption() {
    if (this.pinned) return "Đang xem event đã chọn #" + this.pinned.sequence;
    const prefix = {observing: "Event mới nhất đã nhận", paused: "Đã dừng xem · event cuối",
      disconnected: "Mất kết nối · event cuối đã nhận"}[this.observation];
    return this.latest ? prefix + " #" + this.latest.sequence : "Chưa có event trong màn hình này";
  }
}

export class PipelineView {
  constructor(doc, onSelect) {
    this.doc = doc; this.onSelect = onSelect; this.selection = new PipelineSelection();
    this.events = []; this.component = null;
    this.root = doc.getElementById("architecture");
    this.nodes = [...this.root.querySelectorAll("[data-stage]")];
    this.edges = [...this.root.querySelectorAll("[data-edge]")];
    for (const node of this.nodes) {
      node.addEventListener("click", () => {this.component = node.dataset.stage; this.render();});
    }
    doc.getElementById("architecture-latest").addEventListener("click", () => {
      this.selection.follow(); this.component = null; this.render();
    });
    this.render();
  }
  reset() {
    this.selection.reset(); this.component = null; this.events = []; this.render();
  }
  update(events) {
    this.events = events; this.selection.update(events.at(-1)); this.render();
  }
  observe(state) {this.selection.observation = state; this.render();}
  select(event) {this.selection.select(event); this.component = null; this.render();}
  render() {
    const event = this.selection.event, mapping = pipelineEvent(event);
    this.root.dataset.tone = mapping.tone;
    for (const node of this.nodes) {
      node.classList.toggle("selected", mapping.nodes.includes(node.dataset.stage));
      node.classList.toggle("recorded", Boolean(event) && node.dataset.stage === "store");
      node.setAttribute("aria-pressed", String(this.component === node.dataset.stage));
    }
    for (const edge of this.edges) edge.classList.toggle("selected", mapping.edges.includes(edge.dataset.edge));
    const symbols = {neutral: "○", active: "→", success: "✓", error: "!", cancelled: "■", waiting: "…"};
    const labels = {neutral: "Thông tin", active: "Bước thực thi", success: "Đã nhận kết quả",
      error: "Lỗi / gián đoạn", cancelled: "Đã hủy", waiting: "Đang chờ / yêu cầu dừng"};
    this.doc.getElementById("architecture-context").textContent =
      this.selection.caption + " · " + symbols[mapping.tone] + " " + labels[mapping.tone] +
      (event ? " · " + mapping.label : ". Các mũi tên thể hiện luồng thiết kế.");
    const note = this.doc.getElementById("architecture-note");
    const related = this.doc.getElementById("architecture-events");
    related.replaceChildren();
    if (!this.component) {
      note.textContent = "Bấm một khối để xem trách nhiệm và events liên quan. Màu áp dụng cho event đang xem, không phải trạng thái toàn bộ hệ thống.";
      return;
    }
    const [name, description] = COMPONENTS[this.component];
    note.textContent = name + ": " + description;
    const matches = this.events.filter(e => this.component === "store" || pipelineEvent(e).nodes.includes(this.component));
    for (const item of matches.slice(-8)) {
      const button = this.doc.createElement("button");
      button.textContent = "#" + item.sequence + " · " + item.type;
      button.addEventListener("click", () => this.onSelect(item)); related.append(button);
    }
    if (!matches.length) related.textContent = "Chưa có event liên quan trong phần trace đang hiển thị.";
    else if (matches.length > 8) {
      const text = this.doc.createElement("p");
      text.className = "footnote"; text.textContent = "Hiện 8 event liên quan gần nhất; xem timeline/JSON để đọc thêm.";
      related.append(text);
    }
  }
}
