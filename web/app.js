/* 통학버스 안전관리 - 관리자 웹 (빌드 없는 단일 파일)
   FastAPI 가 /admin 으로 서빙하므로 API 와 같은 출처에서 동작한다. */
(() => {
  'use strict';

  // ---------- 기본 도구 ----------
  const $ = (sel, el = document) => el.querySelector(sel);
  const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const pad = (n) => String(n).padStart(2, '0');

  const store = {
    get(k) { try { return localStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch { /* 저장소를 못 쓰는 환경 */ } },
    del(k) { try { localStorage.removeItem(k); } catch { /* 무시 */ } },
  };
  const TOKEN_KEY = 'bus_admin_token';
  const USER_KEY = 'bus_admin_user';
  let token = store.get(TOKEN_KEY) || '';
  let me = null;
  try { me = JSON.parse(store.get(USER_KEY) || 'null'); } catch { me = null; }

  function fmtTime(s) {
    if (!s) return '-';
    const d = new Date(s);
    return isNaN(d) ? '-' : `${pad(d.getMonth() + 1)}.${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }
  function fmtDate(s) {
    if (!s) return '-';
    const d = new Date(s);
    return isNaN(d) ? '-' : `${d.getFullYear()}.${pad(d.getMonth() + 1)}.${pad(d.getDate())}`;
  }
  function fmtMs(ms) {
    if (ms == null) return '-';
    return ms < 1000 ? `${Math.round(ms)}ms` : `${(ms / 1000).toFixed(1)}초`;
  }
  const num = (v) => (v == null ? '-' : v);
  const localIso = (d) =>
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;

  const RESULT = { OK: ['정상', 'ok'], MISSED: ['미하차 의심', 'bad'], NEEDS_CHECK: ['확인 필요', 'warn'], PENDING: ['대기', 'muted'] };
  const TRIP_STATUS = {
    IN_PROGRESS: ['진행 중', 'warn'],
    CLOSED: ['종료', 'ok']
  };
  const ALERT_STATUS = { OPEN: ['미확인', 'bad'], ACKED: ['확인함', 'warn'], RESOLVED: ['해결됨', 'ok'] };
  const LEVEL = { URGENT: ['긴급', 'bad'], CHECK: ['확인 필요', 'warn'] };
  const EVENT_LABEL = { BOARDING: '승차 사진', DROPOFF: '종점 사진', MISMATCH: '불일치 기록' };
  const badge = (map, key) => {
    const [text, cls] = map[key] || [key, 'muted'];
    return `<span class="badge ${cls}">${esc(text)}</span>`;
  };

  // ---------- API ----------
  async function api(path, { method = 'GET', body } = {}) {
    const headers = {};
    if (token) headers.Authorization = `Bearer ${token}`;
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    const res = await fetch(path, { method, headers, body: body !== undefined ? JSON.stringify(body) : undefined });
    if (res.status === 401 && token && path !== '/web/login') {
      logout('로그인이 만료되었습니다. 다시 로그인하세요.');
      throw new Error('unauthorized');
    }
    if (res.status === 204) return null;
    let data = null;
    try { data = await res.json(); } catch { /* 본문 없음 */ }
    if (!res.ok) {
      const d = data && data.detail;
      const msg = typeof d === 'string' ? d
        : Array.isArray(d) ? d.map((x) => x.msg.replace(/^Value error, /, '')).join(', ')
        : `요청에 실패했습니다 (${res.status})`;
      throw new Error(msg);
    }
    return data;
  }

  // ---------- 토스트 ----------
  function toast(msg, kind = '') {
    const el = document.createElement('div');
    el.className = `toast ${kind}`;
    el.textContent = msg;
    $('#toasts').appendChild(el);
    setTimeout(() => el.remove(), 4500);
  }

  // ---------- 페이지 상태 ----------
  let dashPeriod = 'today';
  let dashSimulation = false;
  const tripsState = {
    result: '',
    status: '',
    offset: 0,
    simulation: false
  };
  const alertsState = {
    status: 'OPEN',
    offset: 0,
    simulation: false
  };
  const reqState = { status: 'PENDING', offset: 0 };
  const REQ_LIMIT = 10;
  const rosterState = { q: '', class_name: '', offset: 0, edit: null };
  const ROSTER_LIMIT = 20;
  const REQ_STATUS = { PENDING: ['승인 대기', 'warn'], APPROVED: ['승인됨', 'ok'], REJECTED: ['거절됨', 'muted'] };
  const TRIP_LIMIT = 15;
  const ALERT_LIMIT = 15;

  const pager = (total, offset, limit, key) => {
    const from = total ? offset + 1 : 0;
    const to = Math.min(offset + limit, total);
    return `<nav class="pager" aria-label="페이지 이동">
      <span>${total}건 중 ${from}-${to}</span>
      <button type="button" class="btn small" data-act="page" data-key="${key}" data-offset="${Math.max(offset - limit, 0)}" ${offset <= 0 ? 'disabled' : ''}>이전</button>
      <button type="button" class="btn small" data-act="page" data-key="${key}" data-offset="${offset + limit}" ${offset + limit >= total ? 'disabled' : ''}>다음</button>
    </nav>`;
  };
  const options = (list, valueFn, labelFn, selected = '') =>
    list.map((x) => `<option value="${esc(valueFn(x))}" ${String(valueFn(x)) === String(selected) ? 'selected' : ''}>${esc(labelFn(x))}</option>`).join('');
  const errorBox = (msg) => `<p class="panel-error" role="alert">${esc(msg)}</p>`;

  // ======================================================================
  // 페이지: 대시보드
  // ======================================================================
  function periodRange(p) {
    const now = new Date();
    if (p === 'today') return { from: localIso(new Date(now.getFullYear(), now.getMonth(), now.getDate())) };
    if (p === 'week') return { from: localIso(new Date(now.getFullYear(), now.getMonth(), now.getDate() - 6)) };
    return {};
  }

  function chartSvg(daily) {
    if (!daily.length) {
      return '<p class="empty">이 기간에 운행 기록이 없습니다.</p>';
    }

    const W = 1000;
    const H = 260;
    const top = 35;
    const bottom = 45;
    const side = 55;
    const bw = 24;

    const max = Math.max(
      1,
      ...daily.flatMap(d => [d.trips, d.alerts])
    );

    const plotH = H - top - bottom;
    const slot = (W - side * 2) / daily.length;

    let out = '';

    daily.forEach((d, i) => {
      const cx = side + slot * i + slot / 2;

      const bar = (value, x, cls) => {
        const h = (value / max) * plotH;
        const y = H - bottom - h;

        return `
          ${value > 0 ? `
            <rect class="${cls}"
              x="${x}" y="${y}"
              width="${bw}" height="${h}" rx="4"/>
          ` : ''}
          <text x="${x + bw / 2}"
            y="${y - 9}"
            text-anchor="middle"
            font-size="13">${value}</text>
        `;
      };

      out += bar(d.trips, cx - bw - 4, 'bar-trip');
      out += bar(d.alerts, cx + 4, 'bar-alert');

      out += `
        <text x="${cx}" y="${H - 15}"
          text-anchor="middle"
          font-size="13">
          ${esc(d.date.slice(5).replace('-', '.'))}
        </text>
      `;
    });

    return `
      <div class="legend">
        <span><i style="background:var(--slate)"></i>운행</span>
        <span><i style="background:var(--red)"></i>경고</span>
      </div>
      <svg viewBox="0 0 ${W} ${H}"
        role="img"
        aria-label="일별 운행 수와 경고 수 막대 그래프"
        style="width:100%;height:260px;display:block"
        preserveAspectRatio="xMidYMid meet">
        ${out}
        <line x1="${side}" x2="${W - side}"
          y1="${H - bottom}" y2="${H - bottom}"
          stroke="#d8dee4"/>
      </svg>
    `;
  }

  const uiIcon = (name, size = 24) => {
    const paths = {
      bus: '<rect x="4" y="3" width="16" height="15" rx="3"/><path d="M4 11h16M7 18v3m10-3v3M7.5 14.5h.01m9 0h.01M7 7h10"/>',
      people: '<circle cx="9" cy="7" r="3"/><path d="M2.5 20v-2a6.5 6.5 0 0 1 13 0v2z"/><path d="M17 4a3 3 0 0 1 0 6m1 4a5 5 0 0 1 3.5 5v1"/>',
      bell: '<path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M10 21h4"/>',
      home: '<path d="m3 10 9-7 9 7v11h-6v-7H9v7H3z"/>',
      link: '<circle cx="9" cy="7" r="3"/><path d="M3 20v-2a6 6 0 0 1 10-4.5"/><path d="M16 12h3a3 3 0 0 1 0 6h-3m-1-6h-2a3 3 0 0 0 0 6h2m-2-3h5"/>',
      user: '<circle cx="12" cy="7" r="4"/><path d="M4 21v-2a8 8 0 0 1 16 0v2z"/>',
      logout: '<path d="M10 4H5a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h5M14 8l4 4-4 4m-8-4h12"/>'
    };
    return `<svg class="ui-line-icon" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[name] || paths.bus}</svg>`;
  };

  function decorateSidebar() {
    const nav = $('.rail nav');
    if (!nav || nav.dataset.decorated) return;
    nav.dataset.decorated = 'true';
    const icons = { dashboard: 'home', trips: 'bus', alerts: 'bell', requests: 'link', students: 'user', buses: 'bus' };
    const names = { dashboard: '대시보드', trips: '운행 관리', alerts: '경고 관리', requests: '연결 요청 관리', students: '학생 · 보호자 관리', buses: '버스 · 배정 관리' };
    nav.querySelectorAll('a[data-nav]').forEach(a => {
      const key = a.dataset.nav;
      if (!icons[key]) return;
      const existingBadge = a.querySelector('.count, [id^="nav-"]');
      const badgeEl = existingBadge ? existingBadge.cloneNode(true) : null;
      a.replaceChildren();
      const main = document.createElement('span');
      main.className = 'rail-nav-label';
      main.innerHTML = uiIcon(icons[key], 20);
      const label = document.createElement('span');
      label.textContent = names[key];
      main.appendChild(label);
      a.appendChild(main);
      if (badgeEl) a.appendChild(badgeEl);
    });
    const brand = $('.rail .brand');
    if (brand) {
      const mark = brand.querySelector('.brand-mark');
      if (mark) { mark.outerHTML = `<span class="rail-brand-icon">${uiIcon('bus', 28)}</span>`; }
      const title = [...brand.childNodes].find(n => n.nodeType === Node.TEXT_NODE && n.textContent.trim());
      if (title) {
        const text = title.textContent.trim();
        title.remove();
        const wrap = document.createElement('span');
        wrap.className = 'rail-brand-title';
        const strong = document.createElement('strong'); strong.textContent = text;
        const small = document.createElement('small'); small.textContent = '관리자 시스템';
        wrap.append(strong, small);
        brand.appendChild(wrap);
      }
    }
    const logoutBtn = $('#logout');
    if (logoutBtn) logoutBtn.insertAdjacentHTML('afterbegin', uiIcon('logout', 18));
  }

  async function pageDashboard() {
    const { from } = periodRange(dashPeriod);
    const [summary, open, trips, alerts, buses, requests, activeTrips] = await Promise.all([
      api('/stats/summary?' + new URLSearchParams({ ...(from ? { from } : {}), simulation_mode: String(dashSimulation) })),
      api('/alerts?' + new URLSearchParams({ status: 'OPEN', limit: '5', simulation_mode: String(dashSimulation) })),
      api('/trips?' + new URLSearchParams({ limit: '100', ...(from ? { from } : {}), simulation_mode: String(dashSimulation) })),
      api('/alerts?' + new URLSearchParams({ limit: '5', ...(from ? { from } : {}), simulation_mode: String(dashSimulation) })),
      api('/web/buses'),
      api('/web/child-requests?status=PENDING&limit=4').catch(() => null),
      api('/trips?' + new URLSearchParams({ status: 'IN_PROGRESS', simulation_mode: String(dashSimulation), limit: '100' }))
    ]);
    const activeByBus = new Map();
    for (const t of (activeTrips.items || [])) if (!activeByBus.has(String(t.bus_id))) activeByBus.set(String(t.bus_id), t);
    const activeBusCount = activeByBus.size;
    const s = summary;
    const unresolved = (s.alert_by_status?.OPEN || 0) + (s.alert_by_status?.ACKED || 0);
    const tripItems = trips.items || [];
    const recentTrips = tripItems.slice(0, 4);
    const recentAlerts = alerts.items || [];
    const latestTripByBus = new Map();
    for (const t of tripItems) if (!latestTripByBus.has(String(t.bus_id))) latestTripByBus.set(String(t.bus_id), t);
    const nd = s.notify_delay || {};
    const periodText = dashPeriod === 'today' ? '오늘' : dashPeriod === 'week' ? '최근 7일' : '전체 기간';
    const periods = [['today', '오늘'], ['week', '최근 7일'], ['all', '전체']];
    const riskCount = s.result_counts?.MISSED || 0;
    const totalTrips = Number(s.trip_count) || 0;
    const normalCount = Number(s.result_counts?.OK) || 0;
    const checkCount = Number(s.result_counts?.NEEDS_CHECK) || 0;
    const ringTotal = Math.max(totalTrips, 1);
    const normalPct = totalTrips ? Math.round(normalCount / totalTrips * 100) : 0;
    const checkPct = totalTrips ? Math.round(checkCount / totalTrips * 100) : 0;
    const missedPct = totalTrips ? Math.round(riskCount / totalTrips * 100) : 0;
    const ring = totalTrips ? `conic-gradient(#24815b 0 ${normalPct}%, #e8ac19 ${normalPct}% ${normalPct + checkPct}%, #dc404c ${normalPct + checkPct}% ${normalPct + checkPct + missedPct}%, #dce4ee ${normalPct + checkPct + missedPct}% 100%)` : '#e7edf4';
    const kpi = (kind, label, value, unit, detail, href, icon) => `<a class="overview-kpi ${kind}" href="${href}"><div class="overview-kpi-top"><span class="overview-kpi-icon" aria-hidden="true">${icon}</span><span class="overview-kpi-arrow" aria-hidden="true">↗</span></div><span class="overview-kpi-label">${label}</span><strong>${value}<small>${unit}</small></strong><span class="overview-kpi-note">${detail}</span></a>`;
    const tripStatus = (t) => `${badge(TRIP_STATUS, t.status)} ${badge(RESULT, t.result)}`;
    const dateLabel = new Date().toLocaleDateString('ko-KR', { year: 'numeric', month: 'long', day: 'numeric', weekday: 'short' });
    const scope = `${dashSimulation ? '시뮬레이션' : '실제 운행'} · ${periodText}`;
    return {
      html: `<div class="dashboard-v2">
        <header class="dashboard-hero">
          <div><p class="dashboard-eyebrow">SCHOOL BUS SAFETY CENTER</p><h1>대시보드</h1><p class="sub">통학버스의 운행 현황과 안전 상황을 한눈에 확인하세요.</p></div>
          <div class="dashboard-hero-meta"><strong>${esc(dateLabel)}</strong><span><span class="dashboard-meta-dot"></span>${esc(scope)} 기준</span></div>
        </header>
        <div class="dashboard-toolbar"><div class="seg" role="group" aria-label="운행 구분">
          <button type="button" data-dash-sim="false" aria-pressed="${!dashSimulation}" class="${!dashSimulation ? 'active' : ''}">실제 운행</button>
          <button type="button" data-dash-sim="true" aria-pressed="${dashSimulation}" class="${dashSimulation ? 'active' : ''}">시뮬레이션</button>
        </div><span class="dashboard-toolbar-note">선택한 유형의 기록만 표시됩니다</span></div>
        <section class="dashboard-kpis" aria-label="핵심 안전 현황">
          ${kpi('danger', '미해결 경고', unresolved, '건', `미확인 ${s.alert_by_status?.OPEN || 0}건 · 확인함 ${s.alert_by_status?.ACKED || 0}건`, '#/alerts', uiIcon('bell', 30))}
          ${kpi('warning', '운행 중인 버스', activeBusCount, '대', '서버의 진행 중 운행 기록 기준', '#/trips', uiIcon('bus', 30))}
          ${kpi('danger', '미하차 의심', riskCount, '건', '안전 확인이 필요한 운행 기록', '#/trips', uiIcon('people', 30))}
          <div class="overview-distribution"><div class="overview-distribution-heading"><strong>운행 결과 현황</strong><span>${esc(periodText)}</span></div>
            <div class="overview-distribution-body"><div class="overview-donut" style="--ring:${ring}" role="img" aria-label="정상 ${normalCount}건, 확인 필요 ${checkCount}건, 미하차 의심 ${riskCount}건"><div><strong>${totalTrips}</strong><small>총 운행</small></div></div>
            <div class="overview-donut-legend"><span><i class="normal"></i>정상 <strong>${normalCount}건</strong></span><span><i class="check"></i>확인 필요 <strong>${checkCount}건</strong></span><span><i class="missed"></i>미하차 의심 <strong>${riskCount}건</strong></span></div></div>
          </div>
        </section>
        ${open.total ? `<section class="dashboard-urgent" aria-label="미확인 경고"><div class="dashboard-urgent-head"><strong>🚨 지금 확인할 경고 <span>${open.total}건</span></strong><a href="#/alerts">경고 관리로 이동 →</a></div><div class="dashboard-urgent-list">${open.items.map(a => `<a href="#/alerts/${a.id}"><span>${badge(LEVEL, a.level)} <strong>${esc(a.message || '경고 발생')}</strong></span><small>${esc(a.bus_name || '차량 미상')} · ${fmtTime(a.created_at)}</small></a>`).join('')}</div></section>` : ''}
        <div class="dashboard-main-grid">
          <section class="dashboard-panel dashboard-chart-panel"><div class="dashboard-panel-head"><div><h2>운행 및 경고 추이</h2><p>선택 기간의 일별 발생 현황</p></div><div class="seg" role="group" aria-label="조회 기간">${periods.map(([key,label]) => `<button type="button" data-act="period" data-value="${key}" aria-pressed="${dashPeriod === key}">${label}</button>`).join('')}</div></div><div class="dashboard-chart-content">${chartSvg(s.daily || [])}</div></section>
          <section class="dashboard-panel dashboard-fleet-panel"><div class="dashboard-panel-head"><div><h2>버스별 현재 상태</h2><p>서버의 운행 상태 기록 기준</p></div><a href="#/buses">전체 보기 →</a></div><div class="dashboard-fleet-list">${buses.map(b => {
            const t = activeByBus.get(String(b.id));
            const status = t ? '운행 중' : '대기 중';
            const cls = t ? 'running' : 'idle';
            return `<${t ? 'a' : 'div'} class="dashboard-fleet-row" ${t ? `href="#/trips/${t.id}"` : ''}><span class="dashboard-bus-icon" aria-hidden="true">${uiIcon('bus', 26)}</span><span class="dashboard-fleet-info"><strong>${esc(b.name)}</strong><small>${esc(b.plate_no || '차량번호 없음')} · 배정 ${b.student_count ?? 0}명</small></span><span class="bus-status ${cls}">${status}</span>${t ? '<span class="dashboard-row-chevron">›</span>' : ''}</${t ? 'a' : 'div'}>`;
          }).join('') || '<p class="empty">등록된 차량이 없습니다.</p>'}</div></section>
        </div>
        <div class="dashboard-bottom-grid">
          <section class="dashboard-panel"><div class="dashboard-panel-head"><div><h2>최근 위험 경고</h2><p>최근 발생한 경고 기록</p></div><a href="#/alerts">전체 보기 →</a></div><div class="dashboard-feed">${recentAlerts.map(a => `<a href="#/alerts/${a.id}" class="dashboard-feed-row"><span class="dashboard-feed-icon danger">!</span><span class="dashboard-feed-info"><strong>${esc(a.message || '경고 발생')}</strong><small>${esc(a.bus_name || '차량 미상')} · ${fmtTime(a.created_at)}</small></span><span class="dashboard-feed-tags">${badge(LEVEL, a.level)}${badge(ALERT_STATUS, a.status)}</span></a>`).join('') || '<p class="empty">해당 기간 경고가 없습니다.</p>'}</div></section>
          <section class="dashboard-panel"><div class="dashboard-panel-head"><div><h2>최근 운행 기록</h2><p>최근 운행 결과 및 상태</p></div><a href="#/trips">전체 보기 →</a></div><div class="dashboard-feed">${recentTrips.map(t => `<a href="#/trips/${t.id}" class="dashboard-feed-row"><span class="dashboard-feed-icon bus">🚌</span><span class="dashboard-feed-info"><strong>${esc(t.bus_name || '차량 정보 없음')}</strong><small>${fmtTime(t.started_at)} · 승차 ${num(t.boarding_count)}명 / 하차 ${num(t.alighted_count)}명</small></span><span class="dashboard-feed-tags">${tripStatus(t)}</span></a>`).join('') || '<p class="empty">해당 기간 운행 기록이 없습니다.</p>'}</div></section>
          <section class="dashboard-panel"><div class="dashboard-panel-head"><div><h2>보호자 연결 요청</h2><p>승인을 기다리는 신청</p></div><a href="#/requests">전체 보기 →</a></div><div class="dashboard-feed">${requests ? (requests.items || []).map(r => `<a href="#/requests" class="dashboard-feed-row"><span class="dashboard-feed-icon person">♙</span><span class="dashboard-feed-info"><strong>${esc(r.user?.name || '신청자')}</strong><small>${esc(r.child_name || '학생 정보 없음')}${r.class_name ? ` (${esc(r.class_name)})` : ''} · ${fmtTime(r.created_at)}</small></span><span class="badge warn">승인 대기</span></a>`).join('') || '<p class="empty">대기 중인 연결 요청이 없습니다.</p>' : '<p class="empty">연결 요청 정보를 불러올 수 없습니다.</p>'}</div></section>
        </div>
        <footer class="dashboard-footer"><span>사진 수신 후 알림까지 평균 ${fmtMs(nd.avg_ms)}${nd.sample_count ? ` · ${nd.sample_count}건 기준` : ''}</span><span>표시된 기록은 선택한 기간과 운행 유형을 따릅니다.</span></footer>
      </div>`
    };
  }

  // ======================================================================
  // 페이지: 운행
  // ======================================================================
  async function pageTrips() {
    const p = new URLSearchParams({
      limit: TRIP_LIMIT,
      offset: tripsState.offset,
      simulation_mode: String(tripsState.simulation)
    });
    if (tripsState.result) p.set('result', tripsState.result);
    if (tripsState.status) p.set('status', tripsState.status);

    const [data, summary] = await Promise.all([
      api('/trips?' + p),
      api('/stats/summary?' + new URLSearchParams({
        simulation_mode: String(tripsState.simulation)
      }))
    ]);

    const stats = [
      ['전체 운행', summary.trip_count, '전체 운행 기록', 'bus'],
      ['운행 중', summary.in_progress_count, '현재 진행 상태로 기록된 운행', 'running'],
      ['정상 종료', summary.closed_count, '종료된 운행 기록', 'done'],
      ['미하차 의심', summary.result_counts.MISSED || 0, '안전 확인이 필요한 운행', 'danger']
    ];

    const rows = data.items.map(t => `
      <tr>
        <td><a href="#/trips/${t.id}">#${t.id}</a></td>
        <td><strong>${esc(t.bus_name || t.bus_id)}</strong></td>
        <td>${fmtTime(t.started_at)}</td>
        <td>${badge(TRIP_STATUS, t.status)}</td>
        <td>${badge(RESULT, t.result)}</td>
        <td class="num">${num(t.boarding_count)}명</td>
        <td class="num">${num(t.alighted_count)}명</td>
        <td class="num">${num(t.remaining_count)}명</td>
        <td><a class="trip-detail-btn" href="#/trips/${t.id}">상세 보기 →</a></td>
      </tr>
    `).join('');

    return {
      html: `
        <header class="pagehead">
          <div>
            <h1>운행 관리</h1>
            <p class="sub">차량별 운행 기록과 학생 안전 상태를 관리합니다.</p>
          </div>
          <span class="badge muted">${tripsState.simulation ? '시뮬레이션' : '실제 운행'}</span>
        </header>
  
        <div class="trip-summary trip-summary-v2">
          ${stats.map(([label, value, note, type]) => `
            <div class="trip-summary-card ${type}">
              <span class="trip-summary-label">${label}</span>
              <strong class="trip-summary-value">${value}<small>건</small></strong>
              <span class="trip-summary-note">${note}</span>
            </div>
          `).join('')}
        </div>
  
        <section class="trip-list-section">
          <div class="trip-list-heading">
            <div>
              <h2>운행 기록 목록</h2>
              <p>선택한 조건에 맞는 운행 ${data.total}건</p>
            </div>
            <div class="seg">
              <button type="button" class="${!tripsState.simulation ? 'active' : ''}"
                data-act="trips-simulation" data-simulation="false">실제 운행</button>
              <button type="button" class="${tripsState.simulation ? 'active' : ''}"
                data-act="trips-simulation" data-simulation="true">시뮬레이션</button>
            </div>
          </div>
  
          <div class="filters trip-filters">
            <label>운행 결과
              <select data-filter="trips-result">
                <option value="">전체 결과</option>
                ${options(Object.entries(RESULT), e => e[0], e => e[1][0], tripsState.result)}
              </select>
            </label>
            <label>운행 상태
              <select data-filter="trips-status">
                <option value="">전체 상태</option>
                ${options(Object.entries(TRIP_STATUS), e => e[0], e => e[1][0], tripsState.status)}
              </select>
            </label>
          </div>
  
          <div class="tablewrap">
            <table>
              <thead><tr>
                <th>운행 번호</th><th>버스</th><th>시작 시간</th>
                <th>운행 상태</th><th>안전 결과</th>
                <th class="num">승차</th><th class="num">하차</th>
                <th class="num">잔류</th><th>관리</th>
              </tr></thead>
              <tbody>${rows || '<tr><td colspan="9" class="empty">조건에 맞는 운행 기록이 없습니다.</td></tr>'}</tbody>
            </table>
          </div>
          ${pager(data.total, tripsState.offset, TRIP_LIMIT, 'trips')}
        </section>
      `
    };
  }

  const imgUrl = (eventId) => `/events/${eventId}/image?token=${encodeURIComponent(token)}`;

  function eventList(events) {
    if (!events.length) return '<p class="empty">아직 기록된 사진이 없습니다.</p>';
    return `<ul class="events">${events.map((e) => `<li>
      <div><h3>${esc(EVENT_LABEL[e.event_type] || e.event_type)}</h3>
        <p class="meta">검출 ${e.person_count}명, 제외 ${e.exclude_count}명, 신뢰도 낮음 ${e.low_conf_count}건<br>수신 ${fmtTime(e.uploaded_at)}</p></div>
      ${e.image_available && e.event_type !== 'MISMATCH'
        ? `<img src="${imgUrl(e.id)}" alt="${esc(EVENT_LABEL[e.event_type] || '')} 실내 사진" loading="lazy">`
        : `<span class="nophoto">${e.event_type === 'MISMATCH' ? '' : '보관 기간이 지나 사진이 삭제되었습니다'}</span>`}
    </li>`).join('')}</ul>`;
  }

  async function pageTripDetail(id) {
    const t = await api(`/trips/${id}`);
    const remaining = Number(t.remaining_count) || 0;
    const openAlerts = t.open_alerts || [];
    const urgentAlerts = openAlerts.filter(
      a => a.level === 'URGENT'
    );

    const safetyPanel = urgentAlerts.length > 0
      ? `
        <div class="trip-safety-panel danger">
          <div class="trip-safety-icon">🚨</div>
          <div class="trip-safety-content">
            <strong>긴급 안전 확인 필요</strong>
            <p>
              기록된 잔류 의심 ${num(remaining)}명 ·
              열린 긴급 경고 ${num(urgentAlerts.length)}건
            </p>
            <a href="#/alerts/${urgentAlerts[0].id}">
              경고 상세 확인 →
            </a>
          </div>
        </div>
      `
      : t.result === 'MISSED'
        ? `
          <div class="trip-safety-panel warning">
            <div class="trip-safety-icon">⚠️</div>
            <div class="trip-safety-content">
              <strong>미하차 의심 기록</strong>
              <p>
                잔류 의심 ${num(remaining)}명 ·
                현재 열린 긴급 경고는 없습니다.
              </p>
            </div>
          </div>
        `
        : `
          <div class="trip-safety-panel safe">
            <div class="trip-safety-icon">✓</div>
            <div class="trip-safety-content">
              <strong>
                ${t.result === 'OK'
                  ? '하차 확인 완료'
                  : '열린 긴급 미하차 경고 없음'}
              </strong>
              <p>
                ${t.result === 'OK'
                  ? '기록된 운행 결과가 정상입니다.'
                  : '운행 결과와 사진 기록을 확인해 주세요.'}
              </p>
            </div>
          </div>
        `;

    const metrics = [
      {
        label: '탑승 예정',
        value: t.expected_count,
        note: '배정 기준',
        type: 'expected'
      },
      {
        label: '승차 인원',
        value: t.boarding_count,
        note: '승차 기록 기준',
        type: 'boarding'
      },
      {
        label: '하차 인원',
        value: t.alighted_count,
        note: '하차 기록 기준',
        type: 'alighted'
      },
      {
        label: '종점 잔류',
        value: t.remaining_count,
        note: '잔류 의심 인원',
        type: 'remaining'
      }
    ];

    const alertRows = openAlerts.map(a => `
      <tr>
        <td>
          <a href="#/alerts/${a.id}">
            ${esc(a.message)}
          </a>
        </td>
        <td>${badge(LEVEL, a.level)}</td>
        <td>${badge(ALERT_STATUS, a.status)}</td>
        <td>${fmtTime(a.created_at)}</td>
        <td>
          <a class="trip-detail-link" href="#/alerts/${a.id}">
            상세 보기 →
          </a>
        </td>
      </tr>
    `).join('');

    return {
      html: `
        <div class="trip-detail-page">
          <a class="back" href="#/trips">← 운행 목록으로</a>
  
          <header class="pagehead">
            <div>
              <h1>${esc(t.bus_name || '버스')} 운행 #${t.id}</h1>
              <p class="sub">
                시작 ${fmtTime(t.started_at)}
                ${t.ended_at
                  ? ` · 종료 ${fmtTime(t.ended_at)}`
                  : ''}
              </p>
            </div>
  
            <div class="trip-detail-badges">
              ${badge(TRIP_STATUS, t.status)}
              ${badge(RESULT, t.result)}
              ${t.simulation_mode === true
                ? '<span class="badge warn">시뮬레이션</span>'
                : ''}
            </div>
          </header>
  
          ${safetyPanel}
  
          <div class="trip-detail-summary">
            ${metrics.map(m => `
              <div class="trip-detail-metric ${m.type}">
                <span class="trip-detail-metric-label">
                  ${m.label}
                </span>
                <div class="trip-detail-metric-value">
                  ${num(m.value)}<small>명</small>
                </div>
                <p>${m.note}</p>
              </div>
            `).join('')}
          </div>
  
          <section class="trip-detail-section">
            <div class="trip-detail-section-head">
              <h2>운행 기본 정보</h2>
            </div>
  
            <dl class="trip-detail-facts">
              <div>
                <dt>운행 번호</dt>
                <dd>#${t.id}</dd>
              </div>
              <div>
                <dt>운행 상태</dt>
                <dd>${badge(TRIP_STATUS, t.status)}</dd>
              </div>
              <div>
                <dt>안전 결과</dt>
                <dd>${badge(RESULT, t.result)}</dd>
              </div>
              <div>
                <dt>종료 확인 방식</dt>
                <dd>${t.confirm_method === 'button'
                      ? '버튼으로 종료 확인'
                      : t.confirm_method
                        ? esc(t.confirm_method)
                        : '-'}</dd>
              </div>
            </dl>
          </section>
  
          ${openAlerts.length ? `
            <section class="trip-detail-section">
              <div class="trip-detail-section-head">
                <h2>열린 안전 경고</h2>
                <span class="trip-detail-count">
                  ${num(openAlerts.length)}건
                </span>
              </div>
  
              <div class="tablewrap">
                <table>
                  <thead>
                    <tr>
                      <th>경고 내용</th>
                      <th>수준</th>
                      <th>처리 상태</th>
                      <th>발생 시간</th>
                      <th>관리</th>
                    </tr>
                  </thead>
                  <tbody>${alertRows}</tbody>
                </table>
              </div>
            </section>
          ` : ''}
  
          <section class="trip-detail-section">
            <div class="trip-detail-section-head">
              <div>
                <h2>사진 및 검출 기록</h2>
                <p>운행 중 수집된 사진과 검출 결과입니다.</p>
              </div>
              <span class="trip-detail-count">
                ${num((t.events || []).length)}건
              </span>
            </div>
  
            ${eventList(t.events || [])}
          </section>
        </div>
      `,
      after: fixImages
    };
  }

  function fixImages(root) {
    root.querySelectorAll('img').forEach((img) => img.addEventListener('error', () => {
      const s = document.createElement('span');
      s.className = 'nophoto';
      s.textContent = '사진을 불러올 수 없습니다';
      img.replaceWith(s);
    }));
  }

  // ======================================================================
  // 페이지: 경고
  // ======================================================================
  async function pageAlerts() {
    const p = new URLSearchParams({
      limit: ALERT_LIMIT,
      offset: alertsState.offset,
      simulation_mode: String(alertsState.simulation)
    });

    if (alertsState.status) {
      p.set('status', alertsState.status);
    }

    const mode = String(alertsState.simulation);

    const [data, summary, openSummary, urgentSummary] =
      await Promise.all([
        api('/alerts?' + p),
        api('/alerts?' + new URLSearchParams({
          limit: '1',
          simulation_mode: mode
        })),
        api('/alerts?' + new URLSearchParams({
          limit: '1',
          status: 'OPEN',
          simulation_mode: mode
        })),
        api('/alerts?' + new URLSearchParams({
          limit: '1',
          level: 'URGENT',
          simulation_mode: mode
        }))
      ]);

    const stats = [
      {
        label: '전체 경고',
        value: summary.total,
        note: '선택한 운행 유형의 전체 경고',
        type: 'total',
        icon: '◉'
      },
      {
        label: '미확인 경고',
        value: openSummary.total,
        note: '관리자 확인이 필요한 경고',
        type: 'open',
        icon: '!'
      },
      {
        label: '긴급 경고',
        value: urgentSummary.total,
        note: '긴급 수준으로 기록된 경고',
        type: 'urgent',
        icon: '⚠'
      }
    ];

    const rows = data.items.map(a => `
      <tr class="${a.level === 'URGENT' ? 'alert-urgent-row' : ''}">
        <td>${badge(LEVEL, a.level)}</td>
        <td class="alert-message-cell">
          <a href="#/alerts/${a.id}">
            ${esc(a.message)}
          </a>
        </td>
        <td>${esc(a.bus_name || '-')}</td>
        <td>${badge(ALERT_STATUS, a.status)}</td>
        <td>${fmtTime(a.created_at)}</td>
        <td>
          <a class="alert-detail-btn" href="#/alerts/${a.id}">
            상세 보기 →
          </a>
        </td>
      </tr>
    `).join('');

    return {
      html: `
        <div class="alerts-page">
          <header class="pagehead">
            <div>
              <h1>경고 관리</h1>
              <p class="sub">
                차량별 안전 경고와 처리 상태를 확인합니다.
              </p>
            </div>
            <span class="badge muted">
              ${alertsState.simulation ? '시뮬레이션' : '실제 운행'}
            </span>
          </header>
  
          <div class="alert-summary-grid">
            ${stats.map(s => `
              <div class="alert-summary-card ${s.type}">
                <div class="alert-summary-top">
                  <span>${s.label}</span>
                  <span class="alert-summary-icon" aria-hidden="true">
                    ${s.icon}
                  </span>
                </div>
                <div class="alert-summary-value">
                  ${num(s.value)}<small>건</small>
                </div>
                <p>${s.note}</p>
              </div>
            `).join('')}
          </div>
  
          <section class="alert-list-section">
            <div class="alert-list-heading">
              <div>
                <h2>안전 경고 목록</h2>
                <p>선택한 조건에 맞는 경고 ${num(data.total)}건</p>
              </div>
  
              <div class="seg" aria-label="운행 구분">
                <button type="button"
                  class="${!alertsState.simulation ? 'active' : ''}"
                  data-act="alerts-simulation"
                  data-simulation="false">
                  실제 운행
                </button>
                <button type="button"
                  class="${alertsState.simulation ? 'active' : ''}"
                  data-act="alerts-simulation"
                  data-simulation="true">
                  시뮬레이션
                </button>
              </div>
            </div>
  
            <div class="filters alert-filters">
              <label>처리 상태
                <select data-filter="alerts-status">
                  <option value="">전체 상태</option>
                  ${options(
                    Object.entries(ALERT_STATUS),
                    e => e[0],
                    e => e[1][0],
                    alertsState.status
                  )}
                </select>
              </label>
            </div>
  
            <div class="tablewrap">
              <table>
                <thead>
                  <tr>
                    <th>경고 수준</th>
                    <th>경고 내용</th>
                    <th>버스</th>
                    <th>처리 상태</th>
                    <th>발생 시간</th>
                    <th>관리</th>
                  </tr>
                </thead>
                <tbody>
                  ${rows || `
                    <tr>
                      <td colspan="6" class="empty">
                        조건에 맞는 경고가 없습니다.
                      </td>
                    </tr>
                  `}
                </tbody>
              </table>
            </div>
  
            ${pager(
              data.total,
              alertsState.offset,
              ALERT_LIMIT,
              'alerts'
            )}
          </section>
        </div>
      `
    };
  }

  async function pageAlertDetail(id) {
    const a = await api(`/alerts/${id}`);
    const t = a.trip;
    const isResolved = a.status === 'RESOLVED';

    const steps = [
      `<li>
        <span class="when">${fmtTime(a.created_at)}</span>
        <strong>경고 발생</strong>
      </li>`
    ];

    if (a.acked_at) {
      steps.push(`
        <li>
          <span class="when">${fmtTime(a.acked_at)}</span>
          <strong>경고 확인</strong>
        </li>
      `);
    }

    if (a.resolved_at) {
      steps.push(`
        <li>
          <span class="when">${fmtTime(a.resolved_at)}</span>
          <strong>해결 처리</strong>
          ${a.note
            ? `<p class="alert-history-note">${esc(a.note)}</p>`
            : ''}
        </li>
      `);
    }

    const resolveForm = `
      <form data-form="resolve" class="resolve">
        <input type="hidden" name="id" value="${a.id}">
  
        <label>조치 메모
          <textarea
            name="note"
            id="note"
            placeholder="예: 차량 내부 확인 후 학생을 보호자에게 인계했습니다"
          ></textarea>
        </label>
  
        <label>해결 방식
          <select name="resolve_mode" required>
            <option value="">해결 방식 선택</option>
            <option value="safety">실제 차량 내부 안전 확인 완료</option>
            <option value="simulation">시뮬레이션 테스트 (실제 안전 확인 아님)</option>
          </select>
        </label>
  
        <button type="submit" class="btn primary">
          해결 처리
        </button>
      </form>
    `;

    const metrics = [
      ['승차 인원', t.boarding_count, '승차 기록 기준', 'boarding'],
      ['하차 인원', t.alighted_count, '하차 기록 기준', 'alighted'],
      ['종점 잔류', t.remaining_count, '미하차 의심 인원', 'remaining']
    ];

    return {
      html: `
        <div class="alert-detail-page">
  
          <a class="back" href="#/alerts">
            ← 경고 목록으로
          </a>
  
          <header class="pagehead">
            <div>
              <h1>경고 상세</h1>
              <p class="sub">
                안전 경고의 발생 정보와 조치 내역을 확인합니다.
              </p>
            </div>
  
            <div class="alert-detail-badges">
              ${badge(LEVEL, a.level)}
              ${badge(ALERT_STATUS, a.status)}
            </div>
          </header>
  
          <section class="alert-detail-hero ${isResolved ? 'resolved' : 'unresolved'}">
            <div class="alert-detail-hero-top">
              <span class="alert-detail-hero-label">
                ${isResolved ? '처리된 안전 경고' : '안전 확인이 필요한 경고'}
              </span>
              <span>${fmtTime(a.created_at)}</span>
            </div>
  
            <div class="alert-detail-hero-body">
              <div class="alert-detail-hero-icon" aria-hidden="true">
                ${isResolved ? '✓' : '⚠'}
              </div>
              <div>
                <h2>${esc(a.message)}</h2>
                <p>${esc(a.bus_name || '-')}</p>
              </div>
            </div>
  
            <div class="alert-detail-hero-status">
              ${badge(LEVEL, a.level)}
              ${badge(ALERT_STATUS, a.status)}
            </div>
          </section>
  
          <div class="alert-detail-summary">
            ${metrics.map(([label, value, note, type]) => `
              <div class="alert-detail-metric ${type}">
                <span class="alert-detail-metric-label">
                  ${label}
                </span>
                <div class="alert-detail-metric-value">
                  ${num(value)}<small>명</small>
                </div>
                <p>${note}</p>
              </div>
            `).join('')}
          </div>
  
          <section class="alert-detail-section">
            <div class="alert-detail-section-head">
              <h2>경고 기본 정보</h2>
            </div>
  
            <dl class="alert-detail-facts">
              <div>
                <dt>경고 수준</dt>
                <dd>${badge(LEVEL, a.level)}</dd>
              </div>
  
              <div>
                <dt>처리 상태</dt>
                <dd>${badge(ALERT_STATUS, a.status)}</dd>
              </div>
  
              <div>
                <dt>검출 신뢰도 낮음</dt>
                <dd>
                  ${a.event
                    ? `${num(a.event.low_conf_count)}건`
                    : '-'}
                </dd>
              </div>
  
              <div>
                <dt>관련 운행</dt>
                <dd>
                  <a href="#/trips/${t.id}">
                    운행 #${t.id} 상세 보기 →
                  </a>
                </dd>
              </div>
            </dl>
          </section>
  
          ${a.llm_summary ? `
            <section class="alert-detail-section">
              <div class="alert-detail-section-head">
                <h2>AI 분석 요약</h2>
                <span class="alert-detail-section-tag">AI 분석</span>
              </div>
  
              <div class="alert-detail-ai">
                <p>${esc(a.llm_summary)}</p>
              </div>
            </section>
          ` : ''}
  
          <section class="alert-detail-section">
            <div class="alert-detail-section-head">
              <h2>처리 이력</h2>
              <span class="alert-detail-section-tag">
                ${steps.length}단계
              </span>
            </div>
  
            <ul class="timeline alert-detail-timeline">
              ${steps.join('')}
            </ul>
          </section>
  
          <div class="alert-detail-grid">
  
            ${!isResolved ? `
              <section class="alert-detail-section">
                <div class="alert-detail-section-head">
                  <h2>경고 조치</h2>
                </div>
  
                <p class="alert-detail-help">
                  실제 안전 상태를 확인한 뒤 적절한 방식으로
                  경고를 처리해 주세요.
                </p>
  
                <div class="actions alert-detail-actions">
                  ${a.status === 'OPEN' ? `
                    <button
                      type="button"
                      class="btn alert"
                      data-act="ack"
                      data-id="${a.id}"
                    >
                      경고 확인
                    </button>
                  ` : ''}
  
                  ${resolveForm}
                </div>
              </section>
            ` : ''}
  
            ${a.event ? `
              <section class="alert-detail-section">
                <div class="alert-detail-section-head">
                  <div>
                    <h2>근거 사진 및 검출 기록</h2>
                    <p>경고 발생 시 수집된 검출 정보입니다.</p>
                  </div>
                  <span class="alert-detail-section-tag">
                    1건
                  </span>
                </div>
  
                ${eventList([a.event])}
              </section>
            ` : ''}
  
          </div>
        </div>
      `,
      after: fixImages
    };
  }

  // ======================================================================
  // 페이지: 연결 요청 (학부모가 신청한 자녀를 관리자가 확인해 학생에 연결)
  // ======================================================================
  async function pageRequests() {
    const p = new URLSearchParams({
      limit: REQ_LIMIT,
      offset: reqState.offset
    });
    if (reqState.status) p.set('status', reqState.status);

    const needStudents = !reqState.status || reqState.status === 'PENDING';

    const [data, students] = await Promise.all([
      api('/web/child-requests?' + p),
      needStudents ? api('/web/students') : Promise.resolve([])
    ]);

    const studentLabel = (s) =>
      `${s.name}${s.class_name ? ` (${s.class_name})` : ' (반 없음)'}`;

    const counts = {
      pending: data.items.filter(r => r.status === 'PENDING').length,
      approved: data.items.filter(r => r.status === 'APPROVED').length,
      rejected: data.items.filter(r => r.status === 'REJECTED').length
    };

    const card = (r) => {
      const who = `${esc(r.user.name)} (${esc(r.user.email)})`;

      const head = `
        <div class="req-card-header">
          <div class="req-avatar" aria-hidden="true">
            ${esc((r.child_name || '?').slice(0, 1))}
          </div>
          <div class="req-card-info">
            <div class="req-card-title">
              <h3>${esc(r.child_name)}</h3>
              ${badge(REQ_STATUS, r.status)}
            </div>
            <p class="req-child-meta">
              ${esc([r.class_name, r.relation].filter(Boolean).join(' · ') || '추가 정보 없음')}
            </p>
            <p class="req-applicant">신청자: ${who}</p>
            <p class="req-date">${fmtTime(r.created_at)} 신청</p>
          </div>
        </div>
      `;

      if (r.status !== 'PENDING') {
        const tail = r.status === 'APPROVED'
          ? `연결된 학생: ${r.student ? esc(studentLabel(r.student)) : '-'}`
          : `거절 사유: ${esc(r.reject_reason || '(없음)')}`;

        return `
          <li class="req req-v2 done">
            ${head}
            <div class="req-completed">
              <span class="req-completed-label">처리 결과</span>
              <p>${tail}</p>
              <span class="req-date">${fmtTime(r.decided_at)} 처리</span>
            </div>
          </li>
        `;
      }

      const exact = r.candidates.filter(c => c.class_match);
      const preselect = exact.length === 1 ? exact[0].id : '';

      const hint = r.candidates.length
        ? `이름이 같은 학생: ${r.candidates.map(c => esc(studentLabel(c))).join(', ')}`
        : '이름이 같은 학생이 없습니다. 학생·보호자 화면에서 학생을 먼저 등록하세요.';

      const confirmText = esc(
        `${r.child_name}${r.class_name ? `(${r.class_name})` : ''} 학생을 ${r.user.name}님의 자녀로 연결합니다.\n신청자의 신원을 확인하셨나요?`
      );

      return `
        <li class="req req-v2 pending">
          ${head}
  
          <div class="req-review">
            <div class="req-review-heading">
              <strong>학생 연결 검토</strong>
              <span>신청자 신원 확인 후 승인해 주세요.</span>
            </div>
  
            <form data-form="approve"
                  class="req-actions req-approve-form"
                  data-confirm="${confirmText}">
              <input type="hidden" name="id" value="${r.id}">
  
              <label>연결할 학생
                <select name="student_id" required>
                  <option value="">학생 선택</option>
                  ${options(students, s => s.id, studentLabel, preselect)}
                </select>
                <span class="hint">${hint}</span>
              </label>
  
              <button type="submit" class="btn primary small">
                연결 승인
              </button>
            </form>
  
            <form data-form="reject"
                  class="req-actions req-reject-form">
              <input type="hidden" name="id" value="${r.id}">
  
              <label>거절 사유 (학부모에게 보입니다)
                <input name="reason"
                       maxlength="200"
                       placeholder="예: 학교 명단에서 찾을 수 없습니다">
              </label>
  
              <button type="submit" class="btn small">
                요청 거절
              </button>
            </form>
          </div>
        </li>
      `;
    };

    return {
      html: `
        <div class="requests-page">
          <header class="pagehead">
            <div>
              <h1>연결 요청 관리</h1>
              <p class="sub">
                학부모의 자녀 연결 신청을 검토하고 승인 또는 거절합니다.
              </p>
            </div>
          </header>
  
          <div class="request-summary-grid">
            <div class="request-summary-card total">
              <span class="request-summary-label">조회된 요청</span>
              <div class="request-summary-value">
                ${num(data.total)}<small>건</small>
              </div>
              <p>선택한 상태 필터 기준</p>
            </div>
  
            <div class="request-summary-card pending">
              <span class="request-summary-label">승인 대기</span>
              <div class="request-summary-value">
                ${num(counts.pending)}<small>건</small>
              </div>
              <p>현재 페이지의 승인 대기 요청</p>
            </div>
  
            <div class="request-summary-card approved">
              <span class="request-summary-label">승인 완료</span>
              <div class="request-summary-value">
                ${num(counts.approved)}<small>건</small>
              </div>
              <p>현재 페이지의 승인 완료 요청</p>
            </div>
  
            <div class="request-summary-card rejected">
              <span class="request-summary-label">거절 처리</span>
              <div class="request-summary-value">
                ${num(counts.rejected)}<small>건</small>
              </div>
              <p>현재 페이지의 거절된 요청</p>
            </div>
          </div>
  
          <section class="request-list-section">
            <div class="request-list-heading">
              <div>
                <h2>자녀 연결 요청 목록</h2>
                <p>선택한 조건에 맞는 요청 ${num(data.total)}건</p>
              </div>
            </div>
  
            <div class="filters request-filters">
              <label>처리 상태
                <select data-filter="requests-status">
                  <option value="">전체 상태</option>
                  ${options(
                    Object.entries(REQ_STATUS),
                    e => e[0],
                    e => e[1][0],
                    reqState.status
                  )}
                </select>
              </label>
            </div>
  
            ${data.items.length
              ? `<ul class="reqs request-cards">
                  ${data.items.map(card).join('')}
                </ul>`
              : `<div class="request-empty">
                  <span class="request-empty-icon" aria-hidden="true">✓</span>
                  <h3>표시할 연결 요청이 없습니다</h3>
                  <p>${reqState.status === 'PENDING'
                    ? '현재 승인을 기다리는 요청이 없습니다.'
                    : '선택한 조건에 맞는 요청이 없습니다.'}</p>
                </div>`
            }
  
            ${pager(data.total, reqState.offset, REQ_LIMIT, 'requests')}
          </section>
        </div>
      `
    };
  }


  // ======================================================================
  // 페이지: 학생·보호자 (학생 등록 · 일괄 등록 · 수정 · 삭제/졸업 처리 + 보호자 계정/연결)
  // ======================================================================
  async function pageStudents() {
    const p = new URLSearchParams({ limit: ROSTER_LIMIT, offset: rosterState.offset });
    if (rosterState.q) p.set('q', rosterState.q);
    if (rosterState.class_name) p.set('class_name', rosterState.class_name);
    const [data, users, allStudents] = await Promise.all([
      api('/web/student-roster?' + p), api('/web/users'), api('/web/students'),
    ]);
    const parents = users.filter((u) => u.role === 'PARENT');
    const totalStudents = allStudents.length;
    const unlinkedStudents = null;
    if (!data.items.length && rosterState.offset > 0) { rosterState.offset = 0; return pageStudents(); }
    const editing = rosterState.edit ? data.items.find((s) => s.id === rosterState.edit) : null;
    if (rosterState.edit && !editing) rosterState.edit = null;

    const rows = data.items.map((s) => `<tr>
      <td><input type="checkbox" data-sel value="${s.id}" aria-label="${esc(s.name)} 선택"></td>
      <td><strong class="student-name">${esc(s.name)}</strong></td><td>${esc(s.class_name || '-')}</td>
      <td class="wrap">${s.guardians.length ? s.guardians.map((g) => `${esc(g.name)}${g.relation ? `(${esc(g.relation)})` : ''}`).join(', ') : '<span class="muted-text">연결된 보호자 없음</span>'}</td>
      <td>${fmtDate(s.consent_at)}</td>
      <td class="rowact"><button type="button" class="btn small" data-act="roster-edit" data-id="${s.id}">수정</button>
        <button type="button" class="btn small danger" data-act="roster-del" data-id="${s.id}" data-name="${esc(s.name)}">삭제</button></td></tr>`).join('');

    const classBtn = rosterState.class_name && rosterState.class_name !== '-' && !rosterState.q
      ? `<button type="button" class="btn danger" data-act="roster-del-class" data-class="${esc(rosterState.class_name)}" data-total="${data.total}">${esc(rosterState.class_name)} 반 전체 삭제 (졸업 처리, ${data.total}명)</button>` : '';

    const editBox = editing ? `<form data-form="rosterEdit" class="editbox">
        <h2>학생 정보 수정</h2><input type="hidden" name="id" value="${editing.id}">
        <div class="inline">
          <label>이름<input name="name" required maxlength="100" value="${esc(editing.name)}"></label>
          <label>반<input name="class_name" maxlength="50" value="${esc(editing.class_name || '')}" placeholder="예: 1-2"></label>
        </div>
        <button type="submit" class="btn primary">저장</button>
        <button type="button" class="btn" data-act="roster-cancel">취소</button></form>` : '';

    return {
      html: `
        <header class="pagehead">
          <div>
            <h1>학생·보호자 관리</h1>
            <p class="sub">학생 명부와 보호자 계정 및 연결 정보를 관리합니다.</p>
          </div>
        </header>
      
        <div class="student-summary-grid">
          <div class="student-summary-card total">
            <div class="student-summary-label">
              <span>전체 학생</span>
              <span class="student-summary-icon">
                <svg viewBox="0 0 24 24" width="20" height="20"
                  fill="none" stroke="currentColor" stroke-width="2"
                  stroke-linecap="round" stroke-linejoin="round">
                  <circle cx="12" cy="8" r="4"/>
                  <path d="M5 21v-2a7 7 0 0 1 14 0v2"/>
                </svg>
              </span>
            </div>
            <div class="student-summary-value">
              ${num(totalStudents)}<small>명</small>
            </div>
            <p>전체 등록 학생 기준</p>
          </div>
        
          <div class="student-summary-card parents">
            <div class="student-summary-label">
              <span>보호자 계정</span>
              <span class="student-summary-icon">
                <svg viewBox="0 0 24 24" width="20" height="20"
                  fill="none" stroke="currentColor" stroke-width="2"
                  stroke-linecap="round" stroke-linejoin="round">
                  <path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/>
                  <circle cx="9" cy="7" r="4"/>
                  <path d="M22 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/>
                </svg>
              </span>
            </div>
            <div class="student-summary-value">
              ${num(parents.length)}<small>명</small>
            </div>
            <p>등록된 학부모 계정</p>
          </div>
        
          <div class="student-summary-card filtered">
            <div class="student-summary-label">
              <span>현재 조회 학생</span>
              <span class="student-summary-icon">
                <svg viewBox="0 0 24 24" width="20" height="20"
                  fill="none" stroke="currentColor" stroke-width="2"
                  stroke-linecap="round" stroke-linejoin="round">
                  <circle cx="11" cy="11" r="7"/>
                  <path d="m20 20-4-4"/>
                </svg>
              </span>
            </div>
            <div class="student-summary-value">
              ${num(data.total)}<small>명</small>
            </div>
            <p>검색 및 반 필터 기준</p>
          </div>
        </div>
        
        <p class="sub-lead">학생을 등록하고 졸업한 학생을 삭제합니다. 삭제하면 그 학생의 보호자 연결, 버스 배정, 출결 기록도 함께 지워집니다. 학부모 계정과 운행·경고 기록은 남습니다.</p>
        <div class="student-list-heading">
          <div>
            <h2>학생 명부</h2>
            <p>등록된 학생과 보호자 연결 현황을 확인합니다.</p>
          </div>
          <span class="student-list-count">
            조회 결과 ${num(data.total)}명
          </span>
        </div>
        <form data-form="rosterSearch" class="filters">
          <label>이름 검색<input name="q" value="${esc(rosterState.q)}" placeholder="이름" maxlength="100"></label>
          <label>반<select data-filter="roster-class"><option value="">전체</option><option value="-" ${rosterState.class_name === '-' ? 'selected' : ''}>반 없음</option>${options(data.classes, (c) => c, (c) => c, rosterState.class_name)}</select></label>
          <button type="submit" class="btn">검색</button>
        </form>
        ${editBox}
        <div class="bulkbar">
          <button type="button" class="btn danger" data-act="roster-del-sel">선택한 학생 삭제</button>
          ${classBtn}
        </div>
        <div class="tablewrap"><table><thead><tr><th><input type="checkbox" data-act="roster-selall" aria-label="이 페이지 전체 선택"></th><th>이름</th><th>반</th><th>보호자</th><th>보호자 동의일</th><th></th></tr></thead>
        <tbody>${rows || '<tr><td colspan="6" class="empty">조건에 맞는 학생이 없습니다. 아래에서 학생을 등록하세요.</td></tr>'}</tbody></table></div>
        ${pager(data.total, rosterState.offset, ROSTER_LIMIT, 'roster')}
        <section class="student-registration" aria-label="학생·보호자 등록">
          <details class="registration-toggle">
            <summary>학생·보호자 등록 및 연결 <span>클릭해서 펼치기</span></summary>
            <div class="forms">
          <form data-form="rosterAdd"><h2>학생 한 명 등록</h2>
            <label>이름<input name="name" required maxlength="100"></label>
            <label>반<input name="class_name" placeholder="예: 1-2" maxlength="50"></label>
            <label>보호자 동의일<input name="consent_at" type="date"></label>
            <button type="submit" class="btn primary">학생 등록</button></form>
          <form data-form="rosterBulk"><h2>여러 명 한꺼번에 등록</h2>
            <label>한 줄에 한 명씩 <span class="hint">이름, 반 (쉼표나 탭으로 구분). 엑셀에서 복사해 붙여넣어도 됩니다. 최대 500명</span>
              <textarea name="lines" rows="8" placeholder="김하늘, 1-2&#10;이도윤, 3-1&#10;박서연, 2-3"></textarea></label>
            <button type="submit" class="btn primary">일괄 등록</button></form>
          <form data-form="parent"><h2>보호자 계정 등록</h2>
            <label>이름<input name="name" required maxlength="100"></label>
            <label>이메일<input name="email" type="email" required></label>
            <label>임시 비밀번호<input name="password" type="password" required minlength="4" autocomplete="new-password"><span class="hint">4자 이상, 영문 대문자와 특수문자를 포함하세요</span></label>
            <button type="submit" class="btn primary">보호자 계정 등록</button></form>
          <form data-form="guardian"><h2>보호자 연결</h2>
            <label>학생<select name="student_id" required><option value="">선택</option>${options(allStudents, (s) => s.id, (s) => `${s.name}${s.class_name ? ` (${s.class_name})` : ''}`)}</select></label>
            <label>보호자<select name="user_id" required><option value="">선택</option>${options(parents, (u) => u.id, (u) => `${u.name} ${u.email}`)}</select></label>
            <label>관계<input name="relation" placeholder="예: 모" maxlength="30"></label>
            <button type="submit" class="btn primary">보호자 연결</button></form>
     </div>
    </details>
  </section>`,
    };
  }

  // ======================================================================
  // 페이지: 버스·배정
  // ======================================================================
  async function pageBuses() {
    const [buses, users, students] = await Promise.all([api('/web/buses'), api('/web/users'), api('/web/students')]);
    const admins = users.filter((u) => u.role === 'ADMIN');
    const driverName = (id) => (admins.find((u) => u.id === id) || {}).name || '-';
    const assigned = await Promise.all(buses.map((b) => api(`/web/buses/${b.id}/students`)));
    const assignedIds = new Set(
      assigned.flat().map((s) => s.id)
    );

    const totalBuses = buses.length;
    const assignedStudents = assignedIds.size;
    const unassignedStudents = students.filter(
      (s) => !assignedIds.has(s.id)
    ).length;
    const rows = buses.map((b, i) => `<tr><td>${esc(b.name)}</td><td>${esc(b.plate_no)}</td><td>${esc(driverName(b.driver_id))}</td>
      <td class="num">${b.student_count}</td>
      <td class="wrap">${assigned[i].length ? assigned[i].map((s) => esc(s.name)).join(', ') : '<span class="muted-text">배정된 학생 없음</span>'}</td></tr>`).join('');
    return {
      html: `
        <header class="pagehead">
          <div>
            <h1>버스·배정 관리</h1>
            <p class="sub">등록된 차량과 학생 탑승 배정 현황을 관리합니다.</p>
          </div>
        </header>
      
        <div class="bus-summary-grid">
          <div class="bus-summary-card total">
            <div class="bus-summary-label">
              <span>전체 버스</span>
              <span class="bus-summary-icon" aria-hidden="true">🚌</span>
            </div>
            <div class="bus-summary-value">
              ${num(totalBuses)}<small>대</small>
            </div>
            <p>등록된 전체 차량</p>
          </div>
        
          <div class="bus-summary-card assigned">
            <div class="bus-summary-label">
              <span>배정된 학생</span>
              <span class="bus-summary-icon" aria-hidden="true">✓</span>
            </div>
            <div class="bus-summary-value">
              ${num(assignedStudents)}<small>명</small>
            </div>
            <p>버스에 배정된 학생</p>
          </div>
        
          <div class="bus-summary-card unassigned">
            <div class="bus-summary-label">
              <span>미배정 학생</span>
              <span class="bus-summary-icon" aria-hidden="true">!</span>
            </div>
            <div class="bus-summary-value">
              ${num(unassignedStudents)}<small>명</small>
            </div>
            <p>버스 배정이 필요한 학생</p>
          </div>
        </div>
        <div class="bus-list-heading">
          <div>
            <h2>버스별 학생 배정 현황</h2>
            <p>등록된 차량과 탑승 예정 학생을 확인합니다.</p>
          </div>
          <span class="bus-list-count">
            전체 ${num(totalBuses)}대
          </span>
        </div>
        <div class="tablewrap"><table><thead><tr><th>버스</th><th>차량번호</th><th>기사</th><th class="num">학생 수</th><th>탑승 예정 학생</th></tr></thead>
        <tbody>${rows || '<tr><td colspan="5" class="empty">등록된 버스가 없습니다. 아래에서 버스를 등록하세요.</td></tr>'}</tbody></table></div>
        <section class="student-registration" aria-label="버스 등록 및 학생 배정">
          <details class="registration-toggle">
            <summary>버스 등록 및 학생 배정 <span>클릭해서 펼치기</span></summary>
            <div class="forms">
          <form data-form="bus"><h2>버스 등록</h2>
            <label>이름<input name="name" required placeholder="예: 1호차" maxlength="50"></label>
            <label>차량번호<input name="plate_no" required placeholder="예: 12가3456" maxlength="30"></label>
            <label>기사<select name="driver_id"><option value="">지정 안 함</option>${options(admins, (u) => u.id, (u) => u.name)}</select></label>
            <button type="submit" class="btn primary">버스 등록</button></form>
          <form data-form="assign"><h2>탑승 학생 배정</h2>
            <label>버스<select name="bus_id" required><option value="">선택</option>${options(buses, (b) => b.id, (b) => `${b.name} ${b.plate_no}`)}</select></label>
            <label>학생<select name="student_id" required><option value="">선택</option>${options(students, (s) => s.id, (s) => `${s.name}${s.class_name ? ` (${s.class_name})` : ''}`)}</select></label>
            <button type="submit" class="btn primary">학생 배정</button></form>
                </div>
              </details>
            </section>`,
    };
  }

  // ======================================================================
  // 폼 처리
  // ======================================================================
  const forms = {
    async student(f) {
      await api('/web/students', { method: 'POST', body: { name: f.name, class_name: f.class_name || null, consent_at: f.consent_at ? `${f.consent_at}T00:00:00` : null } });
      return '학생을 등록했습니다';
    },
    async parent(f) {
      await api('/web/users', { method: 'POST', body: { name: f.name, email: f.email, password: f.password, role: 'PARENT' } });
      return '보호자 계정을 등록했습니다';
    },
    async guardian(f) {
      await api(`/web/students/${f.student_id}/guardians`, { method: 'POST', body: { user_id: Number(f.user_id), relation: f.relation || null } });
      return '보호자를 연결했습니다';
    },
    async bus(f) {
      await api('/web/buses', { method: 'POST', body: { name: f.name, plate_no: f.plate_no, driver_id: f.driver_id ? Number(f.driver_id) : null } });
      return '버스를 등록했습니다';
    },
    async assign(f) {
      await api(`/web/buses/${f.bus_id}/students`, { method: 'POST', body: { student_id: Number(f.student_id) } });
      return '학생을 배정했습니다';
    },
    async rosterSearch(f) {
      rosterState.q = (f.q || '').trim(); rosterState.offset = 0;
      return null;
    },
    async rosterAdd(f) {
      const r = await api('/web/students/bulk', { method: 'POST', body: { students: [{ name: f.name, class_name: f.class_name || null, consent_at: f.consent_at ? `${f.consent_at}T00:00:00` : null }] } });
      if (r.created === 0) throw new Error(r.skipped[0] ? r.skipped[0].reason : '등록하지 못했습니다');
      return '학생을 등록했습니다';
    },
    async rosterBulk(f) {
      const students = String(f.lines || '').split(/\r?\n/).map((l) => l.trim()).filter(Boolean).map((l) => {
        const i = l.search(/[,\t]/);
        return i < 0 ? { name: l, class_name: null } : { name: l.slice(0, i).trim(), class_name: l.slice(i + 1).trim() || null };
      });
      if (!students.length) throw new Error('등록할 학생을 입력하세요');
      if (students.length > 500) throw new Error('한 번에 500명까지 등록할 수 있습니다');
      const r = await api('/web/students/bulk', { method: 'POST', body: { students } });
      return `${r.created}명 등록` + (r.skipped.length ? `, ${r.skipped.length}명은 이미 있어 건너뜀 (${r.skipped.slice(0, 3).map((x) => x.name).join(', ')}${r.skipped.length > 3 ? ' 외' : ''})` : '');
    },
    async rosterEdit(f) {
      await api(`/web/students/${f.id}`, { method: 'PUT', body: { name: f.name, class_name: f.class_name || null } });
      rosterState.edit = null;
      return '학생 정보를 수정했습니다';
    },
    async approve(f) {
      await api(`/web/child-requests/${f.id}/approve`, { method: 'POST', body: { student_id: Number(f.student_id) } });
      return '자녀를 연결했습니다';
    },
    async reject(f) {
      await api(`/web/child-requests/${f.id}/reject`, { method: 'POST', body: { reason: f.reason || null } });
      return '요청을 거절했습니다';
    },
    async resolve(f) {
      if (!['safety', 'simulation'].includes(f.resolve_mode)) {
        throw new Error('해결 방식을 선택해 주세요.');
      }

      const simulation = f.resolve_mode === 'simulation';

      const note = simulation
        ? `[시뮬레이션] ${f.note || 'AI 생성 이미지 기반 테스트. 실제 차량 내부 안전 확인 없음.'}`
        : f.note || '실제 차량 내부 안전 확인 완료';

      await api(`/alerts/${f.id}/resolve`, {
        method: 'POST',
        body: {
          note: note,
          safety_checked: !simulation,
          simulation_mode: simulation
        }
      });

      return '경고를 해결 처리했습니다';
    },
  };

  document.addEventListener('submit', async (e) => {
    const form = e.target.closest('form[data-form]');
    if (!form) return;
    e.preventDefault();
    if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) return;   // 되돌리기 어려운 처리는 한 번 더 확인
    const btn = form.querySelector('button[type=submit]');
    btn.disabled = true;
    try {
      const msg = await forms[form.dataset.form](Object.fromEntries(new FormData(form)));
      if (msg) toast(msg);
      await route();
      refreshBadge();
    } catch (err) {
      if (err.message !== 'unauthorized') toast(err.message, 'error');
    } finally {
      btn.disabled = false;
    }
  });

  // ---------- 클릭 / 선택 위임 ----------
  document.addEventListener('click', async (e) => {
    const el = e.target.closest('[data-act], [data-dash-sim]');
    if (!el) return;
    const act = el.dataset.dashSim !== undefined ? 'dash-sim' : el.dataset.act;
    if (act === 'period') {
      dashPeriod = el.dataset.value;
      route();
    } else if (act === 'dash-sim') {
      dashSimulation = el.dataset.dashSim === 'true';
      route();
    } else if (act === 'alerts-simulation') {
      alertsState.simulation = el.dataset.simulation === 'true';
      alertsState.offset = 0;
      route();
    } else if (act === 'trips-simulation') {
      tripsState.simulation = el.dataset.simulation === 'true';
      tripsState.offset = 0;
      route();
    } else if (act === 'page') {
      ({ trips: tripsState, alerts: alertsState, requests: reqState, roster: rosterState })[el.dataset.key].offset = Number(el.dataset.offset);
      route();
    } else if (act === 'ack') {
      el.disabled = true;
      try { await api(`/alerts/${el.dataset.id}/ack`, { method: 'POST' }); toast('경고를 확인했습니다'); await route(); refreshBadge(); }
      catch (err) { if (err.message !== 'unauthorized') toast(err.message, 'error'); el.disabled = false; }
    } else if (act === 'close-banner') { $('#banner').hidden = true; }
    else if (act === 'roster-edit') { rosterState.edit = Number(el.dataset.id); route(); }
    else if (act === 'roster-cancel') { rosterState.edit = null; route(); }
    else if (act === 'roster-selall') {
      document.querySelectorAll('input[data-sel]').forEach((c) => { c.checked = el.checked; });
    } else if (act === 'roster-del' || act === 'roster-del-sel' || act === 'roster-del-class') {
      let body, msg;
      if (act === 'roster-del') {
        body = { ids: [Number(el.dataset.id)] };
        msg = `${el.dataset.name} 학생을 삭제할까요?`;
      } else if (act === 'roster-del-sel') {
        const ids = [...document.querySelectorAll('input[data-sel]:checked')].map((c) => Number(c.value));
        if (!ids.length) { toast('삭제할 학생을 먼저 선택하세요', 'error'); return; }
        body = { ids };
        msg = `선택한 ${ids.length}명을 삭제할까요?`;
      } else {
        body = { class_name: el.dataset.class };
        msg = `${el.dataset.class} 반 학생 ${el.dataset.total}명을 모두 삭제(졸업 처리)할까요?`;
      }
      if (!window.confirm(`${msg}\n보호자 연결, 버스 배정, 출결 기록도 함께 삭제되며 되돌릴 수 없습니다.`)) return;
      el.disabled = true;
      try {
        const r = await api('/web/students/bulk-delete', { method: 'POST', body });
        toast(`${r.deleted}명을 삭제했습니다`);
        rosterState.edit = null;
        await route();
      } catch (err) { if (err.message !== 'unauthorized') toast(err.message, 'error'); el.disabled = false; }
    }
  });

  document.addEventListener('change', (e) => {
    const k = e.target.dataset && e.target.dataset.filter;
    if (!k) return;
    if (k === 'trips-result') { tripsState.result = e.target.value; tripsState.offset = 0; }
    if (k === 'trips-status') { tripsState.status = e.target.value; tripsState.offset = 0; }
    if (k === 'alerts-status') { alertsState.status = e.target.value; alertsState.offset = 0; }
    if (k === 'roster-class') { rosterState.class_name = e.target.value; rosterState.offset = 0; }
    if (k === 'requests-status') { reqState.status = e.target.value; reqState.offset = 0; }
    route();
  });

  // ======================================================================
  // 라우터
  // ======================================================================
  const routes = [
    [/^#\/dashboard$/, pageDashboard, 'dashboard'],
    [/^#\/trips$/, pageTrips, 'trips'],
    [/^#\/trips\/(\d+)$/, pageTripDetail, 'trips'],
    [/^#\/alerts$/, pageAlerts, 'alerts'],
    [/^#\/alerts\/(\d+)$/, pageAlertDetail, 'alerts'],
    [/^#\/requests$/, pageRequests, 'requests'],
    [/^#\/students$/, pageStudents, 'students'],
    [/^#\/buses$/, pageBuses, 'buses'],
  ];
  let renderId = 0;

  async function route(navigated = false) {
    if (!token) return;
    const hash = location.hash || '#/dashboard';
    const view = $('#view');
    for (const [re, fn, nav] of routes) {
      const m = hash.match(re);
      if (!m) continue;
      document.querySelectorAll('.rail nav a').forEach((a) => {
        if (a.dataset.nav === nav) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
      });
      const rid = ++renderId;
      try {
        const page = await fn(...m.slice(1));
        if (rid !== renderId) return;           // 그 사이 다른 화면으로 이동함
        view.innerHTML = page.html;
        if (page.after) page.after(view);
        if (navigated) { window.scrollTo(0, 0); view.focus({ preventScroll: true }); }
      } catch (err) {
        if (rid === renderId && err.message !== 'unauthorized') view.innerHTML = errorBox(err.message);
      }
      return;
    }
    location.hash = '#/dashboard';
  }
  window.addEventListener('hashchange', () => { $('#banner').hidden = true; route(true); });

  // ======================================================================
  // 실시간 (SSE)
  // ======================================================================
  let es = null;
  let retryTimer = null;
  let refreshTimer = null;
  let badgeTimer = null;

  function setLive(on) {
    const el = $('#live');
    el.classList.toggle('on', on);
    el.classList.toggle('off', !on);
    $('#live-text').textContent = on ? '실시간 연결됨' : '연결 끊김, 다시 연결 중';
  }

  async function refreshBadge() {
    try {
      const r = await api('/alerts?status=OPEN&limit=1');
      const el = $('#nav-alert-count');
      el.textContent = r.total;
      el.hidden = r.total === 0;
      document.title = (r.total ? `(${r.total}) ` : '') + '통학버스 안전관리';
    } catch { /* 배지는 실패해도 화면 사용에 지장 없음 */ }
    try {
      const q = await api('/web/child-requests?status=PENDING&limit=1');
      const rel = $('#nav-request-count');
      rel.textContent = q.total;
      rel.hidden = q.total === 0;
    } catch { /* 무시 */ }
  }

  function userIsTyping() {
    const a = document.activeElement;
    return a && $('#view').contains(a) && /INPUT|TEXTAREA|SELECT/.test(a.tagName) && (a.value || '').length > 0;
  }
  function softRefresh() {                       // 입력 중인 내용이 지워지지 않게 건너뜀
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(() => { if (!userIsTyping()) route(); }, 300);
  }

  function onAlert(a) {
    refreshBadge();
    if (a.status === 'OPEN' && a.level === 'URGENT' && location.hash !== `#/alerts/${a.id}`) {
      const b = $('#banner');
      b.innerHTML = `<span>새 경고: ${esc(a.bus_name || '')} ${esc(a.message)}</span>
        <span><a href="#/alerts/${a.id}">경고 열기</a> <button type="button" data-act="close-banner">닫기</button></span>`;
      b.hidden = false;
    }
    if (/^#\/(dashboard|alerts)/.test(location.hash || '#/dashboard')) softRefresh();
  }
  function onEvent() {
    if (/^#\/(dashboard|trips)/.test(location.hash || '#/dashboard')) softRefresh();
  }

  function connectLive() {
    clearTimeout(retryTimer);
    if (es) es.close();
    es = new EventSource(`/stream/events?token=${encodeURIComponent(token)}`);
    es.addEventListener('connected', () => setLive(true));
    es.addEventListener('alert', (ev) => { try { onAlert(JSON.parse(ev.data)); } catch { /* 무시 */ } });
    es.addEventListener('event', (ev) => { try { onEvent(JSON.parse(ev.data)); } catch { /* 무시 */ } });
    es.onerror = () => {
      setLive(false);
      if (es.readyState === EventSource.CLOSED && token) retryTimer = setTimeout(connectLive, 5000);
    };
  }

  // ======================================================================
  // 로그인 / 로그아웃 / 시작
  // ======================================================================
  function showLogin(message) {
    $('#app').hidden = true;
    $('#login').hidden = false;
    const err = $('#login-error');
    err.hidden = !message;
    err.textContent = message || '';
  }

  function logout(message) {
    token = ''; me = null;
    store.del(TOKEN_KEY); store.del(USER_KEY);
    if (es) { es.close(); es = null; }
    clearTimeout(retryTimer);
    clearInterval(badgeTimer);
    showLogin(message);
  }

  function showApp() {
    decorateSidebar();
    $('#login').hidden = true;
    $('#app').hidden = false;
    $('#me-name').textContent = me ? me.name : '';
    connectLive();
    refreshBadge();
    clearInterval(badgeTimer);
    badgeTimer = setInterval(refreshBadge, 60000);     // 새 연결 요청은 실시간 알림이 없으므로 1분마다 확인
    route(true);
  }

  $('#login-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const btn = e.target.querySelector('button[type=submit]');
    const f = Object.fromEntries(new FormData(e.target));
    btn.disabled = true;
    try {
      const r = await api('/web/login', { method: 'POST', body: { email: f.email, password: f.password } });
      if (r.role !== 'ADMIN') throw new Error('관리자 계정으로 로그인하세요. 학부모 계정은 이 화면을 쓸 수 없습니다.');
      token = r.access_token; me = r.user;
      store.set(TOKEN_KEY, token); store.set(USER_KEY, JSON.stringify(me));
      e.target.reset();
      showApp();
    } catch (err) {
      showLogin(err.message);
    } finally {
      btn.disabled = false;
    }
  });
  $('#logout').addEventListener('click', () => logout());

  (async function start() {
    if (!token) return showLogin();
    try {
      me = await api('/me');
      if (me.role !== 'ADMIN') throw new Error('관리자 계정으로 로그인하세요.');
      showApp();
    } catch (err) {
      if (err.message !== 'unauthorized') logout(err.message);
    }
  })();
})();
