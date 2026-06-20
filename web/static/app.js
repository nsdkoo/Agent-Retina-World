async function fetchJson(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(res.statusText);
  return res.json();
}

function renderStats(data) {
  const p = data.pipeline || {};
  const d = data.dedup || {};
  const m = data.memory || {};
  const cards = [
    ["采集帧数", p.captured ?? 0],
    ["去重跳过", p.dedup_skipped ?? 0],
    ["VLM 分析", p.analyzed ?? 0],
    ["活动事件", m.event_count ?? 0],
    ["去重率", ((p.dedup_skip_rate ?? 0) * 100).toFixed(1) + "%"],
    ["语义命中", d.semantic?.hits ?? 0],
  ];
  document.getElementById("stats").innerHTML = cards
    .map(([label, value]) => `
      <div class="stat-card">
        <div class="label">${label}</div>
        <div class="value">${value}</div>
      </div>`)
    .join("");
}

function renderCategories(data) {
  const cats = data.categories || [];
  const max = Math.max(...cats.map((c) => c.minutes), 1);
  const el = document.getElementById("categories");
  if (!cats.length) {
    el.innerHTML = '<div class="empty">暂无数据，先运行 python main.py watch</div>';
    return;
  }
  el.innerHTML = cats
    .map((c) => `
      <div class="bar-row">
        <div class="name">${c.name}</div>
        <div class="track"><div class="fill" style="width:${(c.minutes / max) * 100}%"></div></div>
        <div class="mins">${c.minutes}m</div>
      </div>`)
    .join("");
}

function renderTimeline(data) {
  const events = data.events || [];
  const el = document.getElementById("timeline");
  if (!events.length) {
    el.innerHTML = '<div class="empty">暂无活动记录</div>';
    return;
  }
  el.innerHTML = events
    .map((e) => {
      const start = new Date(e.started_at);
      const time = start.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
      return `
        <div class="event">
          <div class="time">${time} · ${e.duration_minutes} 分钟</div>
          <div class="tags">
            <span class="tag">${e.page_category}</span>
            <span class="tag">${e.user_action}</span>
          </div>
          <div class="summary">${e.summary}</div>
          <div class="meta">${e.frame_count} 帧 · ${e.evidence_count} 证据</div>
        </div>`;
    })
    .join("");
}

async function refresh() {
  try {
    const [stats, categories, events] = await Promise.all([
      fetchJson("/api/stats"),
      fetchJson("/api/categories"),
      fetchJson("/api/events?limit=30"),
    ]);
    renderStats(stats);
    renderCategories(categories);
    renderTimeline(events);
  } catch (err) {
    console.error(err);
  }
}

refresh();
setInterval(refresh, 15000);
