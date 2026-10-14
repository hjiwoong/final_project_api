/* 통학버스 안전관리 - 관리자 웹 (빌드 없는 단일 파일)
   FastAPI 가 /web 으로 서빙하므로 API 와 같은 출처에서 동작한다. */
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
  const TRIP_STATUS = { IN_PROGRESS: '진행 중', CLOSED: '종료' };
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
    if (res.status === 401 && token && !path.startsWith('/auth/')) {
      logout('로그인이 만료되었습니다. 다시 로그인하세요.');
      throw new Error('unauthorized');
    }
    if (res.status === 204) return null;
    let data = null;
    try { data = await res.json(); } catch { /* 본문 없음 */ }
    if (!res.ok) {
      const d = data && data.detail;
      const msg = typeof d === 'string' ? d
        : Array.isArray(d) ? d.map((x) => x.msg).join(', ')
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
  const tripsState = { result: '', status: '', offset: 0 };
  const alertsState = { status: 'OPEN', offset: 0 };
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
    if (!daily.length) return '<p class="empty">이 기간에 운행 기록이 없습니다.</p>';
    const H = 190, top = 26, bottom = 34, side = 8, bw = 18;
    const W = Math.max(520, daily.length * 70);
    const max = Math.max(1, ...daily.flatMap((d) => [d.trips, d.alerts]));
    const plotH = H - top - bottom;
    const slot = (W - side * 2) / daily.length;
    let out = '';
    daily.forEach((d, i) => {
      const cx = side + slot * i + slot / 2;
      const bar = (v, x, cls) => {
        const h = (v / max) * plotH;
        const y = H - bottom - h;
        return `${v > 0 ? `<rect class="${cls}" x="${x}" y="${y}" width="${bw}" height="${h}" rx="2"/>` : ''}
          <text x="${x + bw / 2}" y="${y - 5}" text-anchor="middle">${v}</text>`;
      };
      out += bar(d.trips, cx - bw - 2, 'bar-trip') + bar(d.alerts, cx + 2, 'bar-alert');
      out += `<text x="${cx}" y="${H - 12}" text-anchor="middle">${esc(d.date.slice(5).replace('-', '.'))}</text>`;
    });
    return `<div class="legend"><span><i style="background:var(--slate)"></i>운행</span><span><i style="background:var(--red)"></i>경고</span></div>
      <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="일별 운행 수와 경고 수 막대 그래프" style="width:${W}px;max-width:none">${out}
      <line x1="${side}" x2="${W - side}" y1="${H - bottom}" y2="${H - bottom}" stroke="#d8dee4"/></svg>`;
  }

  async function pageDashboard() {
    const { from } = periodRange(dashPeriod);
    const [s, open] = await Promise.all([
      api('/stats/summary' + (from ? `?from=${encodeURIComponent(from)}` : '')),
      api('/alerts?status=OPEN&limit=5'),
    ]);
    const unresolved = (s.alert_by_status.OPEN || 0) + (s.alert_by_status.ACKED || 0);
    const nd = s.notify_delay;
    const delayNote = nd.avg_ms == null
      ? '경고가 생기면 측정됩니다'
      : `목표 ${nd.target_ms / 1000}초 이내, ${nd.within_target ? '달성' : '초과'} (${nd.sample_count}건 기준)`;
    const periods = [['today', '오늘'], ['week', '최근 7일'], ['all', '전체']];

    const band = open.total > 0
      ? `<section class="alertband" aria-labelledby="ab-h"><h2 id="ab-h">확인이 필요한 경고 ${open.total}건</h2><ul>
          ${open.items.map((a) => `<li><a href="#/alerts/${a.id}"><strong>${esc(a.message)}</strong><span>${esc(a.bus_name || '-')}, ${fmtTime(a.created_at)}</span></a></li>`).join('')}
        </ul></section>`
      : '<p class="calm">확인이 필요한 경고가 없습니다.</p>';

    return {
      html: `<header class="pagehead"><h1>대시보드</h1>
        <div class="seg" role="group" aria-label="기간">${periods.map(([k, t]) =>
          `<button type="button" data-act="period" data-value="${k}" aria-pressed="${dashPeriod === k}">${t}</button>`).join('')}</div></header>
        ${band}
        <section aria-label="요약"><dl class="strip">
          <div><dt>운행</dt><dd><div class="val">${s.trip_count}<small>건</small></div><div class="note">정상 종료 ${s.closed_count}건, 진행 중 ${s.in_progress_count}건</div></dd></div>
          <div><dt>미하차 의심 운행</dt><dd><div class="val ${s.result_counts.MISSED ? 'bad' : ''}">${s.result_counts.MISSED}<small>건</small></div><div class="note">확인 필요 ${s.result_counts.NEEDS_CHECK}건</div></dd></div>
          <div><dt>경고</dt><dd><div class="val">${s.alert_count}<small>건</small></div><div class="note">아직 해결되지 않은 경고 ${unresolved}건</div></dd></div>
          <div><dt>사진 수신 후 알림까지</dt><dd><div class="val">${fmtMs(nd.avg_ms)}</div><div class="note">${esc(delayNote)}</div></dd></div>
        </dl></section>
        <section><h2>일별 운행과 경고</h2><div class="chartwrap">${chartSvg(s.daily)}</div></section>`,
    };
  }

  // ======================================================================
  // 페이지: 운행
  // ======================================================================
  async function pageTrips() {
    const p = new URLSearchParams({ limit: TRIP_LIMIT, offset: tripsState.offset });
    if (tripsState.result) p.set('result', tripsState.result);
    if (tripsState.status) p.set('status', tripsState.status);
    const data = await api('/trips?' + p);
    const rows = data.items.map((t) => `<tr>
      <td><a href="#/trips/${t.id}">${t.id}</a></td><td>${esc(t.bus_name || t.bus_id)}</td><td>${fmtTime(t.started_at)}</td>
      <td class="num">${num(t.boarding_count)}</td><td class="num">${num(t.remaining_count)}</td><td class="num">${num(t.alighted_count)}</td>
      <td>${badge(RESULT, t.result)}</td><td>${esc(TRIP_STATUS[t.status] || t.status)}</td></tr>`).join('');
    return {
      html: `<header class="pagehead"><h1>운행</h1></header>
        <div class="filters">
          <label>결과<select data-filter="trips-result"><option value="">전체</option>${options(Object.entries(RESULT), (e) => e[0], (e) => e[1][0], tripsState.result)}</select></label>
          <label>상태<select data-filter="trips-status"><option value="">전체</option>${options(Object.entries(TRIP_STATUS), (e) => e[0], (e) => e[1], tripsState.status)}</select></label>
        </div>
        <div class="tablewrap"><table><thead><tr><th>번호</th><th>버스</th><th>시작</th><th class="num">승차</th><th class="num">잔류</th><th class="num">하차</th><th>결과</th><th>상태</th></tr></thead>
        <tbody>${rows || '<tr><td colspan="8" class="empty">조건에 맞는 운행이 없습니다.</td></tr>'}</tbody></table></div>
        ${pager(data.total, tripsState.offset, TRIP_LIMIT, 'trips')}`,
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
    return {
      html: `<a class="back" href="#/trips">운행 목록</a>
        <header class="pagehead"><div><h1>${esc(t.bus_name || '버스')} 운행 #${t.id}</h1>
          <p class="sub">시작 ${fmtTime(t.started_at)}${t.ended_at ? `, 종료 ${fmtTime(t.ended_at)}` : ''}</p></div>
          <div>${badge(RESULT, t.result)}</div></header>
        <dl class="facts">
          <div><dt>탑승 예정</dt><dd>${num(t.expected_count)}</dd></div><div><dt>승차</dt><dd>${num(t.boarding_count)}</dd></div>
          <div><dt>종점 잔류</dt><dd>${num(t.remaining_count)}</dd></div><div><dt>하차</dt><dd>${num(t.alighted_count)}</dd></div>
          <div><dt>종료 확인 방식</dt><dd>${t.confirm_method ? esc(t.confirm_method) : '-'}</dd></div>
        </dl>
        ${t.open_alerts.length ? `<section><h2>열린 경고</h2><div class="tablewrap"><table><tbody>${t.open_alerts.map((a) =>
          `<tr><td><a href="#/alerts/${a.id}">${esc(a.message)}</a></td><td>${badge(LEVEL, a.level)}</td><td>${badge(ALERT_STATUS, a.status)}</td><td>${fmtTime(a.created_at)}</td></tr>`).join('')}</tbody></table></div></section>` : ''}
        <section><h2>사진 기록</h2>${eventList(t.events)}</section>`,
      after: fixImages,
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
    const p = new URLSearchParams({ limit: ALERT_LIMIT, offset: alertsState.offset });
    if (alertsState.status) p.set('status', alertsState.status);
    const data = await api('/alerts?' + p);
    const rows = data.items.map((a) => `<tr>
      <td>${badge(LEVEL, a.level)}</td><td><a href="#/alerts/${a.id}">${esc(a.message)}</a></td><td>${esc(a.bus_name || '-')}</td>
      <td>${badge(ALERT_STATUS, a.status)}</td><td>${fmtTime(a.created_at)}</td></tr>`).join('');
    return {
      html: `<header class="pagehead"><h1>경고</h1></header>
        <div class="filters"><label>상태<select data-filter="alerts-status"><option value="">전체</option>${options(Object.entries(ALERT_STATUS), (e) => e[0], (e) => e[1][0], alertsState.status)}</select></label></div>
        <div class="tablewrap"><table><thead><tr><th>수준</th><th>내용</th><th>버스</th><th>상태</th><th>발생</th></tr></thead>
        <tbody>${rows || '<tr><td colspan="5" class="empty">조건에 맞는 경고가 없습니다.</td></tr>'}</tbody></table></div>
        ${pager(data.total, alertsState.offset, ALERT_LIMIT, 'alerts')}`,
    };
  }

  async function pageAlertDetail(id) {
    const a = await api(`/alerts/${id}`);
    const t = a.trip;
    const steps = [`<li><span class="when">${fmtTime(a.created_at)}</span>경고 발생</li>`];
    if (a.acked_at) steps.push(`<li><span class="when">${fmtTime(a.acked_at)}</span>경고 확인</li>`);
    if (a.resolved_at) steps.push(`<li><span class="when">${fmtTime(a.resolved_at)}</span>해결 처리${a.note ? `<br><span class="muted-text">${esc(a.note)}</span>` : ''}</li>`);

    const resolveForm = `<form data-form="resolve" class="resolve">
      <input type="hidden" name="id" value="${a.id}">
      <label>조치 메모<textarea name="note" id="note" placeholder="예: 차량 확인 후 아이를 발견해 보호자에게 인계했습니다"></textarea></label>
      <button type="submit" class="btn primary">해결 처리</button></form>`;

    return {
      html: `<a class="back" href="#/alerts">경고 목록</a>
        <header class="pagehead"><div><h1>${esc(a.message)}</h1><p class="sub">${esc(a.bus_name || '-')}, ${fmtTime(a.created_at)}</p></div>
          <div>${badge(LEVEL, a.level)} ${badge(ALERT_STATUS, a.status)}</div></header>
        ${a.llm_summary ? `<p class="summary">${esc(a.llm_summary)}</p>` : ''}
        <dl class="facts">
          <div><dt>승차</dt><dd>${num(t.boarding_count)}</dd></div><div><dt>종점 잔류</dt><dd>${num(t.remaining_count)}</dd></div>
          <div><dt>하차</dt><dd>${num(t.alighted_count)}</dd></div>
          <div><dt>검출 신뢰도 낮음</dt><dd>${a.event ? a.event.low_conf_count : '-'}</dd></div>
          <div><dt>운행</dt><dd><a href="#/trips/${t.id}">#${t.id}</a></dd></div>
        </dl>
        <section><h2>처리 이력</h2><ul class="timeline">${steps.join('')}</ul></section>
        ${a.status === 'RESOLVED' ? '' : `<section><h2>조치</h2><div class="actions">
          ${a.status === 'OPEN' ? `<button type="button" class="btn alert" data-act="ack" data-id="${a.id}">경고 확인</button>` : ''}
          ${resolveForm}</div></section>`}
        ${a.event ? `<section><h2>근거 사진</h2>${eventList([a.event])}</section>` : ''}`,
      after: fixImages,
    };
  }

  // ======================================================================
  // 페이지: 학생·보호자
  // ======================================================================
  async function pageStudents() {
    const [students, users] = await Promise.all([api('/web/students'), api('/web/users')]);
    const parents = users.filter((u) => u.role === 'PARENT');
    const guardians = await Promise.all(students.map((s) => api(`/admin/students/${s.id}/guardians`)));
    const rows = students.map((s, i) => `<tr><td>${esc(s.name)}</td><td>${esc(s.class_name || '-')}</td>
      <td class="wrap">${guardians[i].length ? guardians[i].map((g) => `${esc(g.name)}${g.relation ? `(${esc(g.relation)})` : ''}`).join(', ') : '<span class="muted-text">연결된 보호자 없음</span>'}</td>
      <td>${fmtDate(s.consent_at)}</td></tr>`).join('');
    return {
      html: `<header class="pagehead"><h1>학생·보호자</h1></header>
        <div class="tablewrap"><table><thead><tr><th>이름</th><th>반</th><th>보호자</th><th>보호자 동의일</th></tr></thead>
        <tbody>${rows || '<tr><td colspan="4" class="empty">등록된 학생이 없습니다. 아래에서 학생을 등록하세요.</td></tr>'}</tbody></table></div>
        <section class="forms" aria-label="등록">
          <form data-form="student"><h2>학생 등록</h2>
            <label>이름<input name="name" required maxlength="100"></label>
            <label>반<input name="class_name" placeholder="예: 1-2" maxlength="50"></label>
            <label>보호자 동의일<input name="consent_at" type="date"></label>
            <button type="submit" class="btn primary">학생 등록</button></form>
          <form data-form="parent"><h2>보호자 계정 등록</h2>
            <label>이름<input name="name" required maxlength="100"></label>
            <label>이메일<input name="email" type="email" required></label>
            <label>임시 비밀번호<input name="password" type="password" required minlength="4" autocomplete="new-password"></label>
            <button type="submit" class="btn primary">보호자 계정 등록</button></form>
          <form data-form="guardian"><h2>보호자 연결</h2>
            <label>학생<select name="student_id" required><option value="">선택</option>${options(students, (s) => s.id, (s) => `${s.name}${s.class_name ? ` (${s.class_name})` : ''}`)}</select></label>
            <label>보호자<select name="user_id" required><option value="">선택</option>${options(parents, (u) => u.id, (u) => `${u.name} ${u.email}`)}</select></label>
            <label>관계<input name="relation" placeholder="예: 모" maxlength="30"></label>
            <button type="submit" class="btn primary">보호자 연결</button></form>
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
    const assigned = await Promise.all(buses.map((b) => api(`/admin/buses/${b.id}/students`)));
    const rows = buses.map((b, i) => `<tr><td>${esc(b.name)}</td><td>${esc(b.plate_no)}</td><td>${esc(driverName(b.driver_id))}</td>
      <td class="num">${b.student_count}</td>
      <td class="wrap">${assigned[i].length ? assigned[i].map((s) => esc(s.name)).join(', ') : '<span class="muted-text">배정된 학생 없음</span>'}</td></tr>`).join('');
    return {
      html: `<header class="pagehead"><h1>버스·배정</h1></header>
        <div class="tablewrap"><table><thead><tr><th>버스</th><th>차량번호</th><th>기사</th><th class="num">학생 수</th><th>탑승 예정 학생</th></tr></thead>
        <tbody>${rows || '<tr><td colspan="5" class="empty">등록된 버스가 없습니다. 아래에서 버스를 등록하세요.</td></tr>'}</tbody></table></div>
        <section class="forms" aria-label="등록">
          <form data-form="bus"><h2>버스 등록</h2>
            <label>이름<input name="name" required placeholder="예: 1호차" maxlength="50"></label>
            <label>차량번호<input name="plate_no" required placeholder="예: 12가3456" maxlength="30"></label>
            <label>기사<select name="driver_id"><option value="">지정 안 함</option>${options(admins, (u) => u.id, (u) => u.name)}</select></label>
            <button type="submit" class="btn primary">버스 등록</button></form>
          <form data-form="assign"><h2>탑승 학생 배정</h2>
            <label>버스<select name="bus_id" required><option value="">선택</option>${options(buses, (b) => b.id, (b) => `${b.name} ${b.plate_no}`)}</select></label>
            <label>학생<select name="student_id" required><option value="">선택</option>${options(students, (s) => s.id, (s) => `${s.name}${s.class_name ? ` (${s.class_name})` : ''}`)}</select></label>
            <button type="submit" class="btn primary">학생 배정</button></form>
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
      await api(`/admin/students/${f.student_id}/guardians`, { method: 'POST', body: { user_id: Number(f.user_id), relation: f.relation || null } });
      return '보호자를 연결했습니다';
    },
    async bus(f) {
      await api('/web/buses', { method: 'POST', body: { name: f.name, plate_no: f.plate_no, driver_id: f.driver_id ? Number(f.driver_id) : null } });
      return '버스를 등록했습니다';
    },
    async assign(f) {
      await api(`/admin/buses/${f.bus_id}/students`, { method: 'POST', body: { student_id: Number(f.student_id) } });
      return '학생을 배정했습니다';
    },
    async resolve(f) {
      await api(`/alerts/${f.id}/resolve`, { method: 'POST', body: { note: f.note || null } });
      return '경고를 해결 처리했습니다';
    },
  };

  document.addEventListener('submit', async (e) => {
    const form = e.target.closest('form[data-form]');
    if (!form) return;
    e.preventDefault();
    const btn = form.querySelector('button[type=submit]');
    btn.disabled = true;
    try {
      const msg = await forms[form.dataset.form](Object.fromEntries(new FormData(form)));
      toast(msg);
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
    const el = e.target.closest('[data-act]');
    if (!el) return;
    const act = el.dataset.act;
    if (act === 'period') { dashPeriod = el.dataset.value; route(); }
    else if (act === 'page') {
      ({ trips: tripsState, alerts: alertsState })[el.dataset.key].offset = Number(el.dataset.offset);
      route();
    } else if (act === 'ack') {
      el.disabled = true;
      try { await api(`/alerts/${el.dataset.id}/ack`, { method: 'POST' }); toast('경고를 확인했습니다'); await route(); refreshBadge(); }
      catch (err) { if (err.message !== 'unauthorized') toast(err.message, 'error'); el.disabled = false; }
    } else if (act === 'close-banner') { $('#banner').hidden = true; }
  });

  document.addEventListener('change', (e) => {
    const k = e.target.dataset && e.target.dataset.filter;
    if (!k) return;
    if (k === 'trips-result') { tripsState.result = e.target.value; tripsState.offset = 0; }
    if (k === 'trips-status') { tripsState.status = e.target.value; tripsState.offset = 0; }
    if (k === 'alerts-status') { alertsState.status = e.target.value; alertsState.offset = 0; }
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
    showLogin(message);
  }

  function showApp() {
    $('#login').hidden = true;
    $('#app').hidden = false;
    $('#me-name').textContent = me ? me.name : '';
    connectLive();
    refreshBadge();
    route(true);
  }

  $('#login-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const btn = e.target.querySelector('button[type=submit]');
    const f = Object.fromEntries(new FormData(e.target));
    btn.disabled = true;
    try {
      const r = await api('/auth/login', { method: 'POST', body: { email: f.email, password: f.password } });
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
