// A server-backed, paginated picker. No session history is persisted in the browser.
const STATUS = {queued: "Đang chờ", running: "Đang chạy", cancel_requested: "Đang dừng",
  completed: "Hoàn tất", failed: "Lỗi", cancelled: "Đã dừng", interrupted: "Gián đoạn",
  limit_exceeded: "Hết giới hạn"};
export function sessionLabel(item) {
  return item.title || "Phiên chưa có nhiệm vụ";
}
export class SessionPicker {
  constructor(doc, rpc, choose) {
    this.doc = doc; this.rpc = rpc; this.choose = choose;
    this.root = doc.getElementById("session-picker");
    this.toggle = doc.getElementById("session-toggle");
    this.menu = doc.getElementById("session-menu");
    this.list = doc.getElementById("session-list");
    this.status = doc.getElementById("session-list-status");
    this.more = doc.getElementById("session-more");
    this.current = null; this.names = new Map(); this.items = new Set();
    this.cursor = null; this.version = 0; this.loading = false;
    this.toggle.addEventListener("click", () => this.menu.hidden ? this.open() : this.close());
    this.more.addEventListener("click", () => this.load());
    doc.addEventListener("click", event => {if (!this.root.contains(event.target)) this.close();});
    this.root.addEventListener("keydown", event => {
      if (event.key === "Escape") {this.close(); this.toggle.focus();}
    });
  }
  setCurrent(id, title) {
    this.current = id;
    if (title) this.names.set(id, title);
    for (const button of this.list.querySelectorAll("button"))
      button.setAttribute("aria-current", String(button.dataset.sessionId === id));
  }
  close() {
    this.version++; this.loading = false;
    this.menu.hidden = true; this.toggle.setAttribute("aria-expanded", "false");
  }
  async open() {
    this.version++; this.loading = false; this.cursor = null; this.items.clear();
    this.list.replaceChildren(); this.list.scrollTop = 0; this.more.hidden = true;
    this.menu.hidden = false; this.toggle.setAttribute("aria-expanded", "true");
    await this.load();
  }
  async load() {
    if (this.loading || this.menu.hidden) return;
    this.loading = true; this.more.disabled = true;
    this.status.textContent = "Đang tải session…";
    const version = this.version, before = this.cursor;
    try {
      const page = await this.rpc.call("list_sessions", {before, limit: 20});
      if (version !== this.version) return;
      if (!Array.isArray(page.sessions) ||
          (page.has_more && (!Number.isSafeInteger(page.next_before) ||
            page.next_before <= 0 || (before !== null && page.next_before >= before))))
        throw Error("Invalid page");
      for (const item of page.sessions) {
        if (this.items.has(item.session_id)) continue;
        this.items.add(item.session_id); this.names.set(item.session_id, sessionLabel(item));
        const row = this.doc.createElement("li"), button = this.doc.createElement("button");
        button.className = "session-option"; button.dataset.sessionId = item.session_id;
        button.setAttribute("aria-current", String(item.session_id === this.current));
        const name = this.doc.createElement("strong"), detail = this.doc.createElement("small");
        name.textContent = sessionLabel(item);
        const date = new Date(item.updated_at || item.created_at);
        const dateText = Number.isNaN(date.getTime()) ? "" : date.toLocaleString("vi-VN");
        detail.textContent = dateText + " · " + (STATUS[item.latest_run_status] || "Chưa có run")
          + " · …" + item.session_id.slice(-6);
        button.append(name, detail);
        button.addEventListener("click", () => {this.close(); this.choose(item); this.toggle.focus();});
        row.append(button); this.list.append(row);
      }
      this.cursor = page.next_before; this.more.hidden = !page.has_more;
      this.more.textContent = "Tải thêm session";
      this.status.textContent = this.items.size ? "Chọn một phiên để mở. Có thể cuộn danh sách."
        : "Chưa có session nào. Bấm + Session mới để bắt đầu.";
    } catch (error) {
      if (version !== this.version) return;
      this.status.textContent = error.code === "-32601"
        ? "Server chưa có API danh sách. Hãy khởi động lại server sau khi cập nhật."
        : "Không tải được danh sách. Session đang chọn vẫn được giữ nguyên.";
      this.more.hidden = false; this.more.textContent = "Thử tải lại";
    } finally {
      if (version === this.version) {this.loading = false; this.more.disabled = false;}
    }
  }
}
