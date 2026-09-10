const LABELS = ['相关', '感兴趣', '可做', '组会报告', '不相关'];
let dates = [];
let readDatesSet = new Set();
let currentDateStr = '';
let currentMode = 'date'; // 'date', 'filter', 'search'
let currentLabel = 'all';
let currentSearch = '';
let currentView = 'rss';
let manageMode = false;
let managementRefreshTimer = null;
let currentPage = 1; // Used for date view if needed, actually date view doesn't use it.
let currentJournalsPage = 1;
let currentArxivsPage = 1;
let isJournalsLoading = false;
let isArxivsLoading = false;
let isLoading = false; // Kept for compatibility just in case
let papersList = []; // For rendering cards

const mainEl = document.getElementById('main');
const btnOpenCal = document.getElementById('btn-open-cal');
const currentDateText = document.getElementById('current-date-text');
const calModal = document.getElementById('calendar-modal');
const calYearEl = document.getElementById('cal-year');
const calBody = document.getElementById('cal-body');
let currentCalYear = new Date().getFullYear();
const btnPrev = document.getElementById('btn-prev');
const btnNext = document.getElementById('btn-next');
const searchInput = document.getElementById('search-input');
const btnSearch = document.getElementById('btn-search');
const btnManagement = document.getElementById('btn-management');
const btnManualPapers = document.getElementById('btn-manual-papers');
const manualUploadInput = document.getElementById('manual-upload-input');

function showSkeleton(count = 4) {
  mainEl.innerHTML = '<div class="grid-container">' +
    Array.from({ length: count }).map(() => `
      <div class="skeleton-card">
        <div class="sk-title"></div>
        <div class="sk-line"></div>
        <div class="sk-line"></div>
        <div class="sk-line short"></div>
      </div>
    `).join('') + '</div>';
}

async function fetchReadDates() {
  const r = await fetch('/read_dates').then(r => r.json());
  readDatesSet = new Set(r.dates || []);
}

async function fetchDates() {
  const r = await fetch('/dates').then(r => r.json());
  dates = r.dates || [];
  await fetchReadDates();

  if (dates.length) {
    currentDateStr = dates[0];
    currentDateText.textContent = currentDateStr;
    await loadDatePapers();
  } else {
    mainEl.innerHTML = '<div id="empty">暂无数据</div>';
  }
}

async function loadDatePapers() {
  if (!currentDateStr) return;
  currentDateText.textContent = currentDateStr;

  let prevValid = null;
  let nextValid = null;
  for (let d of dates) {
    if (d > currentDateStr) nextValid = d;
    if (d < currentDateStr && !prevValid) prevValid = d;
  }

  btnPrev.disabled = !prevValid;
  btnNext.disabled = !nextValid;
  btnPrev.dataset.target = prevValid || '';
  btnNext.dataset.target = nextValid || '';

  currentMode = 'date';
  showSkeleton();

  const { papers } = await fetch(`/papers?date=${currentDateStr}`).then(r => r.json());
  mainEl.innerHTML = '';
  if (!papers.length) {
    mainEl.innerHTML = '<div id="empty">当天没有抓取到新论文 ☕</div>';
    return;
  }

  // "已浏览完" toggle button
  const isRead = readDatesSet.has(currentDateStr);
  const createReadBar = (id) => {
    const bar = document.createElement('div');
    bar.className = 'read-bar';
    bar.innerHTML = `<button id="${id}" class="read-btn mark-read-btn${isRead ? ' read' : ''}">${isRead ? '✅ 已浏览完' : '📋 标记为已浏览'}</button>`;
    return bar;
  };

  mainEl.appendChild(createReadBar('btn-mark-read-top'));

  const journals = papers.filter(p => !p.is_arxiv);
  const arxivs = papers.filter(p => p.is_arxiv);

  if (journals.length) {
    mainEl.insertAdjacentHTML('beforeend', '<div class="section-title">期刊论文</div>');
    const grid = document.createElement('div'); grid.className = 'grid-container';
    journals.forEach(p => grid.appendChild(makeCard(p)));
    mainEl.appendChild(grid);
  }
  if (arxivs.length) {
    mainEl.insertAdjacentHTML('beforeend', '<div class="section-title">arXiv 预印本</div>');
    const grid = document.createElement('div'); grid.className = 'grid-container';
    arxivs.forEach(p => grid.appendChild(makeCard(p)));
    mainEl.appendChild(grid);
  }

  mainEl.appendChild(createReadBar('btn-mark-read-bottom'));

  document.querySelectorAll('.mark-read-btn').forEach(btn => {
    btn.addEventListener('click', async (e) => {
      const allBtns = document.querySelectorAll('.mark-read-btn');
      allBtns.forEach(b => b.disabled = true);
      const r = await fetch('/mark_read', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ date: currentDateStr })
      }).then(r => r.json());
      if (r.ok) {
        if (r.read) {
          readDatesSet.add(currentDateStr);
          allBtns.forEach(b => {
            b.className = 'read-btn mark-read-btn read';
            b.textContent = '✅ 已浏览完';
          });
        } else {
          readDatesSet.delete(currentDateStr);
          allBtns.forEach(b => {
            b.className = 'read-btn mark-read-btn';
            b.textContent = '📋 标记为已浏览';
          });
        }
      }
      allBtns.forEach(b => b.disabled = false);
    });
  });
}

async function loadPapersByType(type, reset = false) {
  if (type === 'journal' && isJournalsLoading) return;
  if (type === 'arxiv' && isArxivsLoading) return;
  
  if (type === 'journal') isJournalsLoading = true;
  if (type === 'arxiv') isArxivsLoading = true;

  if (reset) {
    if (type === 'journal') currentJournalsPage = 1;
    if (type === 'arxiv') currentArxivsPage = 1;
  } else {
    const btn = document.getElementById(`load-more-btn-${type}`);
    if (btn) btn.innerHTML = '<span class="spinner"></span> 加载中...';
  }

  let url = '';
  let page = type === 'journal' ? currentJournalsPage : currentArxivsPage;

  if (currentMode === 'filter') {
    url = `/filter?label=${encodeURIComponent(currentLabel)}&page=${page}&paper_type=${type}`;
  } else if (currentMode === 'search') {
    url = `/search?q=${encodeURIComponent(currentSearch)}&page=${page}&paper_type=${type}`;
  }

  try {
    const res = await fetch(url).then(r => r.json());
    const papers = res.papers || [];
    const hasNextFlag = res.has_next || false;

    let section = document.getElementById(`${type}s-section`);
    let grid = document.getElementById(`${type}s-grid`);

    if (reset && !papers.length) {
      if (section) section.style.display = 'none';
      if (grid) grid.innerHTML = '';
      const oldLoadCtn = document.getElementById(`load-more-ctn-${type}`);
      if (oldLoadCtn) oldLoadCtn.remove();
    } else if (papers.length) {
      if (section) {
         section.style.display = 'block';
         if (reset) grid.innerHTML = '';
      }
      
      papers.forEach(p => {
        const dateStr = p.pushed_at ? p.pushed_at.split('T')[0] : '';
        const originalJournal = p.journal;
        p.journal = dateStr ? `[${dateStr}] ${originalJournal}` : originalJournal;
        
        const card = makeCard(p);
        grid.appendChild(card);
      });
    }

    const oldLoadCtn = document.getElementById(`load-more-ctn-${type}`);
    if (oldLoadCtn) oldLoadCtn.remove();

    if (hasNextFlag) {
      const loadCtn = document.createElement('div');
      loadCtn.id = `load-more-ctn-${type}`;
      loadCtn.className = 'load-more-ctn';
      loadCtn.innerHTML = `<button id="load-more-btn-${type}" style="width:200px;">加载下一页</button>`;
      section.appendChild(loadCtn);
      document.getElementById(`load-more-btn-${type}`).addEventListener('click', () => {
        if (type === 'journal') currentJournalsPage++;
        else currentArxivsPage++;
        loadPapersByType(type, false);
      });
    }
  } catch (e) {
     console.error(e);
  }
  
  if (type === 'journal') isJournalsLoading = false;
  if (type === 'arxiv') isArxivsLoading = false;
}

async function loadFilterOrSearch(reset = true) {
  if (reset) {
    currentJournalsPage = 1;
    currentArxivsPage = 1;
    showSkeleton();
    
    mainEl.innerHTML = '';
    if (currentMode === 'search') mainEl.insertAdjacentHTML('beforeend', '<div class="section-title">搜索结果 (按时间倒序)</div>');
    else if (currentMode === 'filter') mainEl.insertAdjacentHTML('beforeend', `<div class="section-title">筛选: ${currentLabel} (按时间倒序)</div>`);

    const journalsSection = document.createElement('div');
    journalsSection.id = 'journals-section';
    journalsSection.style.display = 'none';
    journalsSection.innerHTML = '<div class="section-title">期刊论文</div>';
    const journalsGrid = document.createElement('div');
    journalsGrid.id = 'journals-grid';
    journalsGrid.className = 'grid-container';
    journalsSection.appendChild(journalsGrid);
    mainEl.appendChild(journalsSection);

    const arxivsSection = document.createElement('div');
    arxivsSection.id = 'arxivs-section';
    arxivsSection.style.display = 'none';
    arxivsSection.innerHTML = '<div class="section-title">arXiv 预印本</div>';
    const arxivsGrid = document.createElement('div');
    arxivsGrid.id = 'arxivs-grid';
    arxivsGrid.className = 'grid-container';
    arxivsSection.appendChild(arxivsGrid);
    mainEl.appendChild(arxivsSection);

    await Promise.all([
      loadPapersByType('journal', true),
      loadPapersByType('arxiv', true)
    ]);
    
    if (journalsSection.style.display === 'none' && arxivsSection.style.display === 'none') {
       mainEl.innerHTML = currentMode === 'search'
        ? '<div id="empty">未找到匹配的结果 🍃</div>'
        : '<div id="empty">暂未找到该标签下的论文 🍃</div>';
    }
  } else {
    // If not reset, we don't call this globally anymore.
    // Pagination handles itself via button listeners calling loadPapersByType.
  }
}

function _clearManagementRefresh() {
  if (managementRefreshTimer) {
    clearTimeout(managementRefreshTimer);
    managementRefreshTimer = null;
  }
}

function _setCategoryActive(filter) {
  document.querySelectorAll('.f-btn').forEach(btn => btn.classList.toggle('active', btn.dataset.filter === filter));
}

function _setRssControls(visible) {
  document.querySelector('.filter-bar').style.display = visible ? 'flex' : 'none';
  document.querySelector('.date-nav-controls').style.display = visible ? 'inline-flex' : 'none';
}

function _managementStatusText(item) {
  const metadata = item.source_type === 'manual'
    ? `元数据：${item.metadata_status || '未开始'}${item.metadata_stage ? `（${item.metadata_stage}）` : ''}`
    : '已有论文附件';
  const full = `全文翻译：${item.status || '未开始'}`;
  const error = item.metadata_error || item.error;
  return `${metadata}；${full}${error ? `；错误：${esc(error)}` : ''}`;
}

function _renderManagementRows(uploads) {
  if (!uploads.length) return '<div id="empty">暂无上传记录</div>';
  return `<div class="management-list">${uploads.map(item => {
    const manual = item.source_type === 'manual';
    const originalHref = manual
      ? `/download_original_pdf?job_id=${encodeURIComponent(item.job_id)}`
      : `/download_original_pdf?url=${encodeURIComponent(item.paper_url)}`;
    const translatedHref = manual
      ? `/download_uploaded_pdf?job_id=${encodeURIComponent(item.job_id)}`
      : `/download_uploaded_pdf?url=${encodeURIComponent(item.paper_url)}`;
    const retry = item.can_retry_metadata
      ? `<button class="management-retry-btn" data-job-id="${escAttr(item.job_id)}">重试识别</button>` : '';
    const translated = item.translated_exists
      ? `<a class="pdf-link" href="${escAttr(translatedHref)}" target="_blank">中文PDF</a>` : '';
    const deleteDisabled = item.can_delete ? '' : ' disabled';
    const deleteTitle = item.can_delete ? '' : '处理中，完成后才能删除';
    const deleteButton = `<button class="management-delete-btn" data-job-id="${escAttr(item.job_id)}" data-source="${manual ? 'manual' : 'rss'}"${deleteDisabled} title="${escAttr(deleteTitle)}">删除</button>`;
    return `<div class="management-row">
      <div class="management-main"><strong>${esc(item.title || item.filename)}</strong>${item.title_zh ? `<span>${esc(item.title_zh)}</span>` : ''}<small>${esc(item.filename)} · ${manual ? '手动论文' : '已有论文附件'} · ${esc(item.created_at || '')}</small><small>${_managementStatusText(item)}</small></div>
      <div class="management-actions"><a class="pdf-link" href="${escAttr(originalHref)}" target="_blank">原PDF</a>${translated}${retry}${deleteButton}</div>
    </div>`;
  }).join('')}</div>`;
}

async function loadManagement() {
  currentView = 'management';
  _setCategoryActive('');
  _clearManagementRefresh();
  _setRssControls(false);
  mainEl.innerHTML = '<div class="management-panel"><div class="manual-upload-toolbar"><h2>上传管理</h2><button id="management-upload-btn">手动上传PDF</button></div><span class="spinner"></span> 加载中...</div>';
  document.getElementById('management-upload-btn').addEventListener('click', () => manualUploadInput.click());
  try {
    const response = await fetch('/api/admin/uploads');
    const data = await response.json();
    const active = (data.uploads || []).some(item => !item.can_delete);
    mainEl.innerHTML = `<div class="management-panel"><div class="manual-upload-toolbar"><h2>上传管理</h2><button id="management-upload-btn">手动上传PDF</button><button id="management-refresh-btn">刷新</button></div>${_renderManagementRows(data.uploads || [])}</div>`;
    document.getElementById('management-upload-btn').addEventListener('click', () => manualUploadInput.click());
    document.getElementById('management-refresh-btn').addEventListener('click', loadManagement);
    document.querySelectorAll('.management-delete-btn').forEach(btn => btn.addEventListener('click', onManagementDelete));
    document.querySelectorAll('.management-retry-btn').forEach(btn => btn.addEventListener('click', onManagementRetry));
    if (active && currentView === 'management') {
      managementRefreshTimer = setTimeout(loadManagement, 5000);
    }
  } catch (error) {
    mainEl.innerHTML = `<div class="management-panel"><div class="error-box">管理列表加载失败：${esc(error.message || error)}<button id="management-refresh-btn">重试</button></div></div>`;
    document.getElementById('management-refresh-btn').addEventListener('click', loadManagement);
  }
}

async function onManagementDelete(event) {
  const btn = event.currentTarget;
  const manual = btn.dataset.source === 'manual';
  const message = manual
    ? '删除手动论文及其全部相关文件？此操作不可恢复。'
    : '仅删除这个PDF附件，保留原论文和标签？';
  if (!window.confirm(message)) return;
  btn.disabled = true;
  try {
    const response = await fetch(`/api/admin/uploads/${encodeURIComponent(btn.dataset.jobId)}`, { method: 'DELETE' });
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.error || '删除失败');
    await loadManagement();
  } catch (error) {
    window.alert(`删除失败：${error.message || error}`);
    btn.disabled = false;
  }
}

async function onManagementRetry(event) {
  const btn = event.currentTarget;
  btn.disabled = true;
  try {
    const response = await fetch(`/api/admin/uploads/${encodeURIComponent(btn.dataset.jobId)}/process`, { method: 'POST' });
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.error || '重试失败');
    await loadManagement();
  } catch (error) {
    window.alert(`重试失败：${error.message || error}`);
    btn.disabled = false;
  }
}

async function loadManualPapers() {
  currentView = 'manual';
  _setCategoryActive('manual');
  _clearManagementRefresh();
  _setRssControls(false);
  mainEl.innerHTML = '<div class="manual-upload-toolbar"><h2>手动上传论文</h2><button id="manual-upload-btn">上传PDF</button></div><span class="spinner"></span> 加载中...';
  document.getElementById('manual-upload-btn').addEventListener('click', () => manualUploadInput.click());
  try {
    const response = await fetch('/api/manual-papers');
    const data = await response.json();
    mainEl.innerHTML = `<div class="manual-upload-toolbar"><h2>手动上传论文</h2><button id="manual-upload-btn">上传PDF</button><button id="manual-refresh-btn">刷新</button></div>${data.papers && data.papers.length ? `<div class="grid-container">${data.papers.map(p => '').join('')}</div>` : '<div id="empty">暂无已识别的手动论文</div>'}`;
    document.getElementById('manual-upload-btn').addEventListener('click', () => manualUploadInput.click());
    document.getElementById('manual-refresh-btn').addEventListener('click', loadManualPapers);
    if (data.papers && data.papers.length) {
      const grid = mainEl.querySelector('.grid-container');
      data.papers.forEach(p => grid.appendChild(makeCard(p)));
    }
  } catch (error) {
    mainEl.innerHTML = `<div class="error-box">手动论文加载失败：${esc(error.message || error)}<button id="manual-refresh-btn">重试</button></div>`;
    document.getElementById('manual-refresh-btn').addEventListener('click', loadManualPapers);
  }
}

function returnToRss() {
  currentView = 'rss';
  _setCategoryActive('all');
  _clearManagementRefresh();
  _setRssControls(true);
  if (currentMode === 'date' && currentDateStr) loadDatePapers();
  else if (currentMode === 'filter' || currentMode === 'search') loadFilterOrSearch(true);
  else fetchDates();
}

function _reloadCurrentView() {
  if (currentView === 'manual') return loadManualPapers();
  if (currentMode === 'filter' || currentMode === 'search') return loadFilterOrSearch(true);
  return loadDatePapers();
}

function toggleManageMode() {
  manageMode = !manageMode;
  btnManagement.textContent = manageMode ? '退出管理' : '管理';
  btnManagement.classList.toggle('active', manageMode);
  if (currentView === 'management') currentView = 'rss';
  _reloadCurrentView();
}

async function onCardDeleteUpload(event) {
  const btn = event.currentTarget;
  if (!window.confirm('仅删除这个PDF附件，保留原论文和标签？')) return;
  btn.disabled = true;
  try {
    const response = await fetch(`/api/admin/uploads/${encodeURIComponent(btn.dataset.jobId)}`, { method: 'DELETE' });
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.error || '删除失败');
    await _reloadCurrentView();
  } catch (error) {
    window.alert(`删除失败：${error.message || error}`);
    btn.disabled = false;
  }
}

function makeCard(p) {
  const div = document.createElement('div');
  div.className = 'card';
  const isManual = p.source_type === 'manual';
  const arxivIdMatch = !isManual && p.is_arxiv && p.url.includes('/abs/') ? p.url.match(/abs\/([^/?v]+)/) : null;
  const arxivId = arxivIdMatch ? arxivIdMatch[1] : null;
  const uploadJobId = p.upload_job_id || p.job_id || '';
  const canManageUpload = manageMode && Boolean(uploadJobId && p.upload_translation_status);
  let pdfLink = '';
  let zhPdfBtn = '';

  if (arxivId) {
    pdfLink = `<a class="pdf-link" href="${escAttr(p.url.replace('/abs/', '/pdf/') + '.pdf')}" target="_blank">PDF</a>`;
    if (p.translation_status === 'done') {
      zhPdfBtn = `<a href="/download_translated_pdf/${encodeURIComponent(arxivId)}" target="_blank" class="pdf-link" style="margin-left: 6px; background: #d97706;">中文PDF</a>`;
    } else if (p.translation_status && p.translation_status !== 'error') {
      zhPdfBtn = '<button class="zh-pdf-btn" disabled>翻译中</button>';
    } else {
      const text = p.translation_status === 'error' ? '翻译失败(重试)' : '翻译PDF';
      zhPdfBtn = `<button class="zh-pdf-btn" data-url="${escAttr(p.url)}" data-id="${escAttr(arxivId)}">${text}</button>`;
    }
  } else if (isManual) {
    const upStatus = p.upload_translation_status;
    if (canManageUpload) {
      pdfLink = `<button class="upload-delete-btn" data-job-id="${escAttr(uploadJobId)}">删除原PDF</button>`;
    } else {
      pdfLink = `<a class="pdf-link orig-pdf-link" href="${escAttr(p.url)}" target="_blank">PDF</a>`;
      if (upStatus === 'done') {
        zhPdfBtn = `<a href="/download_uploaded_pdf?job_id=${encodeURIComponent(uploadJobId)}" target="_blank" class="pdf-link" style="margin-left: 6px; background: #d97706;">中文PDF</a>`;
      } else if (upStatus === 'pending' || upStatus === 'running') {
        zhPdfBtn = `<button class="upload-zh-btn" data-job-id="${escAttr(uploadJobId)}" disabled>翻译中</button>`;
      } else {
        const text = upStatus === 'error' || upStatus === 'interrupted' ? '翻译失败(重试)' : '翻译PDF';
        zhPdfBtn = `<button class="upload-zh-btn" data-job-id="${escAttr(uploadJobId)}">${text}</button>`;
      }
    }
  } else {
    const upStatus = p.upload_translation_status;
    if (upStatus) {
      if (canManageUpload) {
        pdfLink = `<button class="upload-delete-btn" data-job-id="${escAttr(uploadJobId)}">删除原PDF</button>`;
      } else {
        pdfLink = `<a class="pdf-link orig-pdf-link" href="/download_original_pdf?url=${encodeURIComponent(p.url)}" target="_blank">PDF</a>`;
        if (upStatus === 'done') {
          zhPdfBtn = `<a href="/download_uploaded_pdf?url=${encodeURIComponent(p.url)}" target="_blank" class="pdf-link" style="margin-left: 6px; background: #d97706;">中文PDF</a>`;
        } else if (upStatus === 'pending' || upStatus === 'running') {
          zhPdfBtn = '<button class="upload-zh-btn" disabled>翻译中</button>';
        } else {
          const text = upStatus === 'error' || upStatus === 'interrupted' ? '翻译失败(重试)' : '翻译PDF';
          zhPdfBtn = `<button class="upload-zh-btn" data-url="${escAttr(p.url)}">${text}</button>`;
        }
      }
    } else {
      pdfLink = `<button class="upload-orig-btn" data-url="${escAttr(p.url)}">上传PDF</button>`;
    }
  }

  let contentHtml = '';
  const originalAbstract = p.abstract_original || p.abstract_en || '';
  if (isManual) {
    if (p.summary_zh) contentHtml += `<div class="abstract"><strong>总结：</strong>${escLatex(p.summary_zh)}</div>`;
    if (p.abstract_zh) contentHtml += `<div class="abstract"><strong>摘要翻译：</strong>${escLatex(p.abstract_zh)}</div>`;
    if (originalAbstract) {
      contentHtml += `<details class="arxiv-details"><summary>原摘要（${esc(p.source_language || '原文')}）</summary><p class="en-content">${escLatex(originalAbstract)}</p></details>`;
    } else if (p.metadata_incomplete) {
      contentHtml += '<div class="abstract metadata-warning"><strong>提示：</strong>未识别到完整摘要。</div>';
    }
  } else if (p.summary_zh && originalAbstract) {
    contentHtml = `
      <div class="abstract"><strong>总结：</strong>${escLatex(p.summary_zh)}</div>
      <div class="abstract"><strong>原摘要翻译：</strong>${escLatex(p.abstract_zh)}</div>
      <details class="arxiv-details"><summary>原摘要(英文)</summary><p class="en-content">${escLatex(originalAbstract)}</p></details>
    `;
  } else {
    if (p.abstract_zh) {
      contentHtml += `<div class="abstract"><strong>总结：</strong>${escLatex(p.abstract_zh)}</div>`;
      contentHtml += `<div class="abstract"><strong>原摘要翻译：</strong>${escLatex(p.abstract_zh)}</div>`;
    }
    if (p.is_arxiv && p.url.includes('/abs/')) {
      const lazyArxivId = p.url.match(/abs\/([^/?]+)/);
      if (lazyArxivId) contentHtml += `<details class="arxiv-details" data-id="${escAttr(lazyArxivId[1])}" ontoggle="fetchEnAbstract(this)"><summary>原摘要(英文)</summary><p class="en-content">点击加载原文...</p></details>`;
    }
  }

  const labelKey = p.label_key || p.url;
  div.innerHTML = `
    <div class="card-title"><a href="${escAttr(p.url)}" target="_blank">${escLatex(p.title)}</a>${pdfLink}${zhPdfBtn}</div>
    ${p.title_zh ? `<div class="card-title-zh">${escLatex(p.title_zh)}</div>` : ''}
    <div class="card-meta">${esc(p.journal || (isManual ? '手动上传' : ''))}</div>
    ${contentHtml}
    <div class="labels">${LABELS.map(l => {
    const labels = (p.label || '不相关').split(',');
    const isActive = labels.includes(l);
    return `<button class="lbl${isActive ? ' active-' + l : ''}" data-url="${escAttr(p.url)}" data-key="${escAttr(labelKey)}" data-label="${l}">${l}</button>`;
  }).join('')}</div>`;

  div.querySelectorAll('.lbl').forEach(btn => btn.addEventListener('click', onLabel));
  const deleteBtn = div.querySelector('.upload-delete-btn');
  if (deleteBtn) deleteBtn.addEventListener('click', onCardDeleteUpload);
  if (arxivId) {
    const zhBtn = div.querySelector('.zh-pdf-btn');
    if (zhBtn) {
      if (zhBtn.disabled && zhBtn.textContent === '翻译中') startPolling(arxivId, zhBtn);
      else zhBtn.addEventListener('click', () => onZhPdf(zhBtn));
    }
  } else if (isManual) {
    const transBtn = div.querySelector('.upload-zh-btn');
    if (transBtn) {
      if (transBtn.disabled && transBtn.textContent === '翻译中') startUploadPollingCard('', transBtn, uploadJobId);
      else transBtn.addEventListener('click', () => onUploadZhPdf(transBtn));
    }
  } else {
    const uploadBtn = div.querySelector('.upload-orig-btn');
    if (uploadBtn) uploadBtn.addEventListener('click', () => triggerUpload(p.url, uploadBtn));
    const transBtn = div.querySelector('.upload-zh-btn');
    if (transBtn) {
      if (transBtn.disabled && transBtn.textContent === '翻译中') startUploadPollingCard(p.url, transBtn);
      else transBtn.addEventListener('click', () => onUploadZhPdf(transBtn));
    }
  }
  renderMath(div);
  return div;
}

function startPolling(arxivId, btn) {
  let stopped = false;
  const cleanup = () => { clearInterval(timer); document.removeEventListener('visibilitychange', onVisible); };
  const checkStatus = async () => {
    if (stopped) return;
    const s = await fetch(`/translate_status/${arxivId}`).then(r => r.json());
    if (s.status === 'done') {
      stopped = true; cleanup();
      btn.outerHTML = `<a href="/download_translated_pdf/${arxivId}" target="_blank" class="pdf-link" style="margin-left: 6px; background: #d97706;">中文PDF</a>`;
    } else if (s.status === 'error') {
      stopped = true; cleanup();
      btn.textContent = '翻译失败(重试)';
      btn.disabled = false;
      btn.addEventListener('click', () => onZhPdf(btn), { once: true });
    }
  };
  const timer = setInterval(checkStatus, 5000);
  const onVisible = () => { if (document.visibilityState === 'visible' && !stopped) checkStatus(); };
  document.addEventListener('visibilitychange', onVisible);
}

async function onZhPdf(btn) {
  const url = btn.dataset.url;
  const arxivId = btn.dataset.id;
  btn.disabled = true;
  btn.textContent = '提交中...';

  const r = await fetch('/translate_pdf', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ url })
  }).then(r => r.json());

  if (!r.ok) { 
    btn.textContent = '翻译失败(重试)'; 
    btn.disabled = false; 
    return; 
  }
  
  if (r.status === 'done') { 
    btn.outerHTML = `<a href="/download_translated_pdf/${arxivId}" target="_blank" class="pdf-link" style="margin-left: 6px; background: #d97706;">中文PDF</a>`;
    return; 
  }

  btn.textContent = '翻译中';
  startPolling(arxivId, btn);
}

async function onLabel(e) {
  const btn = e.currentTarget;
  const url = btn.dataset.url;
  const labelKey = btn.dataset.key || url;
  const clickedLabel = btn.dataset.label;
  const card = btn.closest('.card');
  
  let currentLabels = Array.from(card.querySelectorAll('.lbl'))
                           .filter(b => b.className.includes('active-'))
                           .map(b => b.dataset.label);
  
  if (currentLabels.includes(clickedLabel)) {
    currentLabels = currentLabels.filter(l => l !== clickedLabel);
    if (clickedLabel === '感兴趣') {
       currentLabels = currentLabels.filter(l => l !== '组会报告' && l !== '可做');
    }
  } else {
    currentLabels.push(clickedLabel);
    if ((clickedLabel === '组会报告' || clickedLabel === '可做') && !currentLabels.includes('感兴趣')) {
      currentLabels.push('感兴趣');
    }
    if (clickedLabel === '不相关') {
      currentLabels = ['不相关'];
    } else {
      currentLabels = currentLabels.filter(l => l !== '不相关');
    }
  }
  
  if (currentLabels.length === 0) {
    currentLabels = ['不相关'];
  }

  const r = await fetch('/label', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ url: labelKey, labels: currentLabels })
  }).then(r => r.json());

  if (r.ok) {
    card.querySelectorAll('.lbl').forEach(b => {
      if (currentLabels.includes(b.dataset.label)) {
        b.className = `lbl active-${b.dataset.label}`;
      } else {
        b.className = 'lbl';
      }
    });
  }
}

function esc(s) {
  return (s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function escAttr(s) {
  return esc(s).replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

/**
 * Escape HTML but preserve LaTeX $...$ delimiters for KaTeX rendering.
 * Splits text on $...$ boundaries, escapes the non-math parts, and
 * reassembles so that KaTeX auto-render can process the math segments.
 */
function escLatex(s) {
  if (!s) return '';
  // Split on $...$ while keeping the delimiters
  const parts = s.split(/(\$[^$]+\$)/g);
  return parts.map(part => {
    if (part.startsWith('$') && part.endsWith('$')) {
      // Math segment: keep as-is (no HTML escaping inside math)
      return part;
    }
    // Normal text: escape HTML
    return part.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }).join('');
}

/**
 * Call KaTeX auto-render on a DOM element to render any $...$ math.
 */
function renderMath(el) {
  if (typeof renderMathInElement === 'function') {
    renderMathInElement(el, {
      delimiters: [
        { left: '$$', right: '$$', display: true },
        { left: '$', right: '$', display: false },
      ],
      throwOnError: false,
    });
  }
}

async function fetchEnAbstract(detailsEl) {
  if (detailsEl.open && !detailsEl.dataset.loaded) {
    const arxivId = detailsEl.dataset.id;
    const contentP = detailsEl.querySelector('.en-content');
    contentP.innerHTML = '<span class="spinner"></span> 获取外网原文中...';

    // Add AbortController to handle timeout
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 8000); // 8s timeout

    try {
      const res = await fetch(`https://export.arxiv.org/api/query?id_list=${arxivId}`, {
        signal: controller.signal
      });
      clearTimeout(timeoutId);
      const text = await res.text();
      const parser = new DOMParser();
      const xmlDoc = parser.parseFromString(text, 'text/xml');
      const summary = xmlDoc.getElementsByTagName('summary')[0].textContent;
      contentP.textContent = summary.trim();
      detailsEl.dataset.loaded = 'true';
    } catch (e) {
      if (e.name === 'AbortError') {
        contentP.innerHTML = '加载超时 ⌛，请稍后重试。';
      } else {
        contentP.innerHTML = '加载失败: ' + e;
      }
    }
  }
}

// Event Listeners
btnPrev.addEventListener('click', () => {
  const target = btnPrev.dataset.target;
  if (target) { currentDateStr = target; loadDatePapers(); }
});
btnNext.addEventListener('click', () => {
  const target = btnNext.dataset.target;
  if (target) { currentDateStr = target; loadDatePapers(); }
});
btnOpenCal.addEventListener('click', () => {
  currentCalYear = parseInt(currentDateStr.split('-')[0], 10) || new Date().getFullYear();
  renderCalendarYear(currentCalYear);
  calModal.style.display = 'flex';
});
document.getElementById('cal-close').addEventListener('click', () => calModal.style.display = 'none');
calModal.addEventListener('click', (e) => { if (e.target === calModal) calModal.style.display = 'none'; });
document.getElementById('cal-prev-year').addEventListener('click', () => renderCalendarYear(--currentCalYear));
document.getElementById('cal-next-year').addEventListener('click', () => renderCalendarYear(++currentCalYear));

function renderCalendarYear(year) {
  calYearEl.textContent = year;
  calBody.innerHTML = '';
  const datesSet = new Set(dates);
  const monthNames = ['一月', '二月', '三月', '四月', '五月', '六月', '七月', '八月', '九月', '十月', '十一月', '十二月'];
  const dayNames = ['日', '一', '二', '三', '四', '五', '六'];

  for (let m = 0; m < 12; m++) {
    const monthCard = document.createElement('div');
    monthCard.className = 'month-card';
    const title = document.createElement('div'); title.className = 'month-title'; title.textContent = monthNames[m];
    monthCard.appendChild(title);
    const grid = document.createElement('div'); grid.className = 'days-grid';
    dayNames.forEach(d => {
      const dh = document.createElement('div'); dh.className = 'day-head'; dh.textContent = d; grid.appendChild(dh);
    });
    const firstDay = new Date(year, m, 1).getDay();
    const daysInMonth = new Date(year, m + 1, 0).getDate();
    for (let i = 0; i < firstDay; i++) {
      const empty = document.createElement('div'); empty.className = 'day-cell empty'; grid.appendChild(empty);
    }
    for (let d = 1; d <= daysInMonth; d++) {
      const cell = document.createElement('div'); cell.className = 'day-cell'; cell.textContent = d;
      const dateStr = `${year}-${String(m + 1).padStart(2, '0')}-${String(d).padStart(2, '0')}`;
      if (datesSet.has(dateStr)) {
        cell.classList.add('active');
        if (readDatesSet.has(dateStr)) {
          cell.classList.add('day-read');
        } else {
          cell.classList.add('day-unread');
        }
        cell.addEventListener('click', () => {
          currentDateStr = dateStr;
          calModal.style.display = 'none';
          loadDatePapers();
        });
      }
      grid.appendChild(cell);
    }
    monthCard.appendChild(grid);
    calBody.appendChild(monthCard);
  }
}

btnSearch.addEventListener('click', () => {
  const q = searchInput.value.trim();
  if (!q) return;
  currentMode = 'search';
  currentSearch = q;

  // Reset filters
  document.querySelectorAll('.f-btn').forEach(b => b.classList.remove('active'));

  // Hide date controls
  document.querySelector('.date-nav-controls').style.display = 'none';

  loadFilterOrSearch(true);
});

searchInput.addEventListener('keypress', (e) => {
  if (e.key === 'Enter') btnSearch.click();
});

document.querySelectorAll('.f-btn').forEach(btn => {
  btn.addEventListener('click', (e) => {
    document.querySelectorAll('.f-btn').forEach(b => b.classList.remove('active'));
    const targetBtn = e.currentTarget;
    targetBtn.classList.add('active');

    const filter = targetBtn.dataset.filter;
    if (filter === 'all') {
      currentMode = 'date';
      document.querySelector('.date-nav-controls').style.display = 'inline-flex';
      searchInput.value = '';
      if (dates.length) loadDatePapers();
    } else if (filter === 'manual') {
      loadManualPapers();
    } else {
      currentMode = 'filter';
      currentLabel = filter;
      document.querySelector('.date-nav-controls').style.display = 'none';
      searchInput.value = '';
      loadFilterOrSearch(true);
    }
  });
});

// Initialization
fetchDates();

// ── Upload Paper ──────────────────────────────────────────────────────────

const cardUploadInput = document.getElementById('card-upload-input');
let currentUploadUrl = null;
let currentUploadBtn = null;

cardUploadInput.addEventListener('change', async () => {
  const file = cardUploadInput.files[0];
  if (!file || !currentUploadUrl || !currentUploadBtn) return;
  
  const url = currentUploadUrl;
  const btn = currentUploadBtn;
  currentUploadUrl = null;
  currentUploadBtn = null;
  cardUploadInput.value = '';

  btn.disabled = true;
  btn.textContent = '上传中...';

  const formData = new FormData();
  formData.append('file', file);
  formData.append('url', url);

  try {
    const res = await fetch('/upload_paper', { method: 'POST', body: formData });
    const data = await res.json();
    if (data.ok) {
      const parent = btn.parentNode;
      const pdfLinkHtml = `<a class="pdf-link orig-pdf-link" href="/download_original_pdf?url=${encodeURIComponent(url)}" target="_blank">PDF</a>`;
      const transBtnHtml = `<button class="upload-zh-btn" data-url="${url}">翻译PDF</button>`;
      btn.outerHTML = pdfLinkHtml + transBtnHtml;
      
      const newTransBtn = parent.querySelector(`.upload-zh-btn[data-url="${url}"]`);
      if (newTransBtn) {
        newTransBtn.addEventListener('click', () => onUploadZhPdf(newTransBtn));
      }
    } else {
      btn.textContent = '上传失败(重试)';
      btn.disabled = false;
    }
  } catch (e) {
    btn.textContent = '上传失败(重试)';
    btn.disabled = false;
  }
});

async function onUploadZhPdf(btn) {
  const url = btn.dataset.url || '';
  const jobId = btn.dataset.jobId || '';
  btn.disabled = true;
  btn.textContent = '提交中...';

  try {
    const payload = jobId ? { job_id: jobId } : { url };
    const res = await fetch('/translate_uploaded_pdf', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload)
    });
    const data = await res.json();
    if (data.ok) {
      if (data.status === 'done') {
        btn.outerHTML = jobId
          ? `<a href="/download_uploaded_pdf?job_id=${encodeURIComponent(jobId)}" target="_blank" class="pdf-link" style="margin-left: 6px; background: #d97706;">中文PDF</a>`
          : `<a href="/download_uploaded_pdf?url=${encodeURIComponent(url)}" target="_blank" class="pdf-link" style="margin-left: 6px; background: #d97706;">中文PDF</a>`;
        return;
      }
      btn.textContent = '翻译中';
      startUploadPollingCard(url, btn, jobId);
    } else {
      btn.textContent = '翻译失败(重试)';
      btn.disabled = false;
    }
  } catch (e) {
    btn.textContent = '翻译失败(重试)';
    btn.disabled = false;
  }
}

function triggerUpload(url, btn) {
  currentUploadUrl = url;
  currentUploadBtn = btn;
  cardUploadInput.click();
}


function startUploadPollingCard(url, btn, jobId = '') {
  let stopped = false;
  const cleanup = () => { clearInterval(timer); document.removeEventListener('visibilitychange', onVisible); };
  const checkStatus = async () => {
    if (stopped) return;
    const query = jobId ? `job_id=${encodeURIComponent(jobId)}` : `url=${encodeURIComponent(url)}`;
    const s = await fetch(`/upload_translate_status?${query}`).then(r => r.json());
    if (s.status === 'done') {
      stopped = true; cleanup();
      const cardTitle = btn.closest('.card-title');
      const hasOrig = cardTitle && cardTitle.querySelector('.orig-pdf-link') !== null;
      const originalHref = jobId
        ? `/download_original_pdf?job_id=${encodeURIComponent(jobId)}`
        : `/download_original_pdf?url=${encodeURIComponent(url)}`;
      const translatedHref = jobId
        ? `/download_uploaded_pdf?job_id=${encodeURIComponent(jobId)}`
        : `/download_uploaded_pdf?url=${encodeURIComponent(url)}`;
      const origLink = hasOrig ? '' : `<a href="${originalHref}" target="_blank" class="pdf-link orig-pdf-link">PDF</a>`;
      btn.outerHTML = `${origLink}<a href="${translatedHref}" target="_blank" class="pdf-link" style="margin-left: 6px; background: #d97706;">中文PDF</a>`;
    } else if (s.status === 'error' || s.status === 'interrupted') {
      stopped = true; cleanup();
      btn.textContent = '翻译失败(重试)';
      btn.disabled = false;
    }
  };
  const timer = setInterval(checkStatus, 5000);
  const onVisible = () => { if (document.visibilityState === 'visible' && !stopped) checkStatus(); };
  document.addEventListener('visibilitychange', onVisible);
}

async function uploadManualPdf(file) {
  const formData = new FormData();
  formData.append('file', file);
  try {
    const response = await fetch('/api/admin/uploads', { method: 'POST', body: formData });
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.error || '上传失败');
    if (currentView === 'management') await loadManagement();
    else await loadManualPapers();
  } catch (error) {
    window.alert(`手动上传失败：${error.message || error}`);
  }
}

manualUploadInput.addEventListener('change', async () => {
  const file = manualUploadInput.files[0];
  manualUploadInput.value = '';
  if (file) await uploadManualPdf(file);
});

btnManagement.addEventListener('click', toggleManageMode);
btnManualPapers.addEventListener('click', loadManualPapers);

