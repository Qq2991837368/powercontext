(function () {
  'use strict';

  /* ===== 常量与工具 ===== */

  var PREVIEW_COUNT = 4;
  var READ_LABELS = {
    read: '已读',
    skim: '略读',
    todo: '待读'
  };

  var paperIndex = new Map();
  var caseHits = new Map();
  var copyResetTimer = null;

  function byId(id) {
    return document.getElementById(id);
  }

  function asArray(value) {
    return Array.isArray(value) ? value : [];
  }

  function escapeHtml(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, function (char) {
      return {
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#39;'
      }[char];
    });
  }

  function setText(id, value) {
    var target = byId(id);
    if (target) {
      target.textContent = value == null ? '—' : String(value);
    }
  }

  function getPaperTitle(id) {
    if (id == null) {
      return '';
    }
    var paper = paperIndex.get(String(id));
    return paper && paper.title ? paper.title : String(id);
  }

  function formatScore(score) {
    var number = Number(score);
    return Number.isFinite(number) ? number.toFixed(4) : String(score == null ? '' : score);
  }

  function formatLocalTime(value) {
    var date = value ? new Date(value) : null;
    if (!date || Number.isNaN(date.getTime())) {
      return null;
    }
    return date.toLocaleString('zh-CN', {
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hour12: false
    });
  }

  function renderTags(values) {
    return asArray(values).map(function (value) {
      return '<span class="tag">' + escapeHtml(value) + '</span>';
    }).join('');
  }

  /* ===== 演示标签与面板 ===== */

  function renderTabs(cases) {
    var container = byId('demo-tabs');
    if (!container) {
      return;
    }

    container.innerHTML = cases.map(function (item, index) {
      var active = index === 0;
      var caseId = escapeHtml(item && item.id);
      var title = item && item.title != null ? item.title : (item && item.id);
      return '<button class="tab" role="tab" type="button" id="tab-' + caseId +
        '" aria-controls="panel-' + caseId + '" aria-selected="' + String(active) +
        '" tabindex="' + (active ? '0' : '-1') + '" data-case-id="' + caseId + '">' +
        escapeHtml(title) + '</button>';
    }).join('');
  }

  function renderPlan(plan) {
    var value = plan || {};
    return '<div class="plan">' +
      '<div class="plan-group"><span class="plan-label">检索词</span>' +
      renderTags(value.terms) + '</div>' +
      '<div class="plan-group"><span class="plan-label">扩展</span>' +
      renderTags(value.expansions) + '</div>' +
      '</div>';
  }

  function renderCoverage(coverage) {
    var value = coverage || {};
    var ratio = Number(value.ratio);
    var percent = Number.isFinite(ratio) ? Math.round(Math.max(0, Math.min(1, ratio)) * 100) : 0;
    var missing = asArray(value.terms_missing);
    var missingHtml = missing.length
      ? '<div class="coverage-missing">' + missing.map(function (term) {
        return '<span class="chip chip-missing">' + escapeHtml(term) + '</span>';
      }).join('') + '</div>'
      : '';

    return '<div class="coverage">' +
      '<span class="coverage-label">证据覆盖</span>' +
      '<div class="progress"><span class="progress-bar" style="width:' + percent + '%"></span></div>' +
      '<span class="coverage-value">' + percent + '%</span>' +
      missingHtml +
      '</div>';
  }

  function renderEvidenceCard(hit, index) {
    var item = hit || {};
    var paper = item.paper || {};
    var title = paper.title || getPaperTitle(paper.id);
    var rank = item.rank == null ? index + 1 : item.rank;
    var provenance = [paper.year, paper.venue, item.section].filter(function (value) {
      return value != null && value !== '';
    }).map(function (value) {
      return escapeHtml(value);
    }).join(' · ');
    var matchedBy = asArray(item.matched_by).map(function (name) {
      return '<span class="tag">' + escapeHtml(name) + '</span>';
    }).join('');
    var evidence = asArray(item.evidence);
    var quotes = evidence.length
      ? evidence
      : (item.snippet != null && item.snippet !== '' ? [item.snippet] : []);
    var quotesHtml = quotes.map(function (line) {
      return '<blockquote class="evidence-quote">' + escapeHtml(line) + '</blockquote>';
    }).join('');

    return '<article class="evidence-card">' +
      '<div class="evidence-head">' +
      '<span class="evidence-ordinal">[P' + escapeHtml(rank) + ']</span>' +
      '<h4 class="evidence-title">' + escapeHtml(title) + '</h4>' +
      '</div>' +
      '<p class="evidence-prov">' + provenance + '</p>' +
      '<div class="evidence-meta">' +
      '<span class="chip chip-score">score ' + escapeHtml(formatScore(item.score)) + '</span>' +
      matchedBy +
      '</div>' +
      quotesHtml +
      '</article>';
  }

  function renderEvidenceCards(hits, expanded) {
    var visible = expanded ? hits : hits.slice(0, PREVIEW_COUNT);
    return visible.map(function (hit, index) {
      return renderEvidenceCard(hit, index);
    }).join('');
  }

  function renderGaps(gaps) {
    var value = gaps || {};
    var papers = asArray(value.uncovered_papers).map(function (id) {
      return '<span class="chip chip-missing">' + escapeHtml(getPaperTitle(id)) + '</span>';
    }).join('');

    return '<div class="gaps">' +
      '<h3>缺口</h3>' +
      '<div class="gaps-list">' + papers + '</div>' +
      '<p class="gaps-note">' + escapeHtml(value.note) + '</p>' +
      '</div>';
  }

  function renderPanel(item, index) {
    var caseData = item || {};
    var pack = caseData.pack || {};
    var hits = asArray(pack.hits);
    var caseId = escapeHtml(caseData.id);
    var active = index === 0;
    var question = caseData.question != null ? caseData.question : pack.question;
    var hasMore = hits.length > PREVIEW_COUNT;
    var toggle = hasMore
      ? '<button class="toggle-btn" type="button" data-case-id="' + caseId +
        '" data-expanded="false" aria-expanded="false">展开全部 ' + hits.length + ' 条</button>'
      : '';

    caseHits.set(String(caseData.id), hits);

    return '<div class="tab-panel" role="tabpanel" id="panel-' + caseId +
      '" aria-labelledby="tab-' + caseId + '" tabindex="0"' + (active ? '' : ' hidden') + '>' +
      '<p class="case-question">' + escapeHtml(question) + '</p>' +
      renderPlan(pack.plan) +
      renderCoverage(pack.coverage) +
      '<div class="evidence-list">' + renderEvidenceCards(hits, false) + '</div>' +
      toggle +
      renderGaps(pack.gaps) +
      '</div>';
  }

  function renderPanels(cases) {
    var container = byId('demo-panels');
    if (!container) {
      return;
    }

    caseHits.clear();
    container.innerHTML = cases.map(function (item, index) {
      return renderPanel(item, index);
    }).join('');
  }

  function activateTab(tab, shouldFocus) {
    var tablist = tab.closest('[role="tablist"]');
    if (!tablist) {
      return;
    }

    var tabs = Array.prototype.slice.call(tablist.querySelectorAll('[role="tab"]'));
    tabs.forEach(function (candidate) {
      var selected = candidate === tab;
      candidate.setAttribute('aria-selected', String(selected));
      candidate.setAttribute('tabindex', selected ? '0' : '-1');

      var panel = byId(candidate.getAttribute('aria-controls'));
      if (panel) {
        panel.hidden = !selected;
      }
    });

    if (shouldFocus) {
      tab.focus();
    }
  }

  function handleTabKeydown(event) {
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight' &&
      event.key !== 'Home' && event.key !== 'End') {
      return;
    }

    var tab = event.target.closest('[role="tab"]');
    if (!tab) {
      return;
    }

    var tablist = tab.closest('[role="tablist"]');
    var tabs = tablist ? Array.prototype.slice.call(tablist.querySelectorAll('[role="tab"]')) : [];
    if (!tabs.length) {
      return;
    }

    var current = tabs.indexOf(tab);
    var nextIndex = current;

    if (event.key === 'ArrowRight') {
      nextIndex = (current + 1) % tabs.length;
    } else if (event.key === 'ArrowLeft') {
      nextIndex = (current - 1 + tabs.length) % tabs.length;
    } else if (event.key === 'Home') {
      nextIndex = 0;
    } else if (event.key === 'End') {
      nextIndex = tabs.length - 1;
    }

    event.preventDefault();
    activateTab(tabs[nextIndex], true);
  }

  function toggleEvidence(button) {
    var panel = button.closest('.tab-panel');
    var list = panel ? panel.querySelector('.evidence-list') : null;
    var hits = caseHits.get(button.getAttribute('data-case-id'));
    if (!list || !hits) {
      return;
    }

    var expanded = button.getAttribute('data-expanded') !== 'true';
    list.innerHTML = renderEvidenceCards(hits, expanded);
    button.setAttribute('data-expanded', String(expanded));
    button.setAttribute('aria-expanded', String(expanded));
    button.textContent = expanded ? '收起' : '展开全部 ' + hits.length + ' 条';
  }

  /* ===== 语料、记录与研究记忆 ===== */

  function renderCorpus(corpus) {
    var body = byId('corpus-body');
    var value = corpus || {};
    var papers = asArray(value.paper_list);

    paperIndex = new Map(papers.map(function (paper) {
      return [String(paper.id), paper];
    }));

    if (!body) {
      return;
    }

    body.innerHTML = papers.map(function (paper) {
      return '<tr>' +
        '<td data-label="论文">' + escapeHtml(paper.title) + '</td>' +
        '<td data-label="年份">' + escapeHtml(paper.year) + '</td>' +
        '<td data-label="发表">' + escapeHtml(paper.venue) + '</td>' +
        '<td data-label="标签"><div class="tag-row">' + renderTags(paper.tags) + '</div></td>' +
        '<td data-label="证据块">' + escapeHtml(paper.chunks) + '</td>' +
        '</tr>';
    }).join('');
  }

  function fillMemoryList(id, items, renderItem) {
    var target = byId(id);
    if (!target) {
      return;
    }

    if (!items.length) {
      target.innerHTML = '<p class="memory-empty">暂无记录</p>';
      return;
    }

    target.innerHTML = items.map(renderItem).join('');
  }

  function renderMemory(memory) {
    var value = memory || {};
    setText('memory-question', value.question != null ? value.question : '—');

    var confirmed = asArray(value.confirmed);
    fillMemoryList('memory-confirmed', confirmed, function (text) {
      return '<li>' + escapeHtml(text) + '</li>';
    });

    var excluded = asArray(value.excluded);
    fillMemoryList('memory-excluded', excluded, function (entry) {
      var text = entry && typeof entry === 'object' ? entry.text : entry;
      var paper = entry && typeof entry === 'object' ? entry.paper : null;
      var paperLabel = paper ? '（' + escapeHtml(getPaperTitle(paper)) + '）' : '';
      return '<li>' + escapeHtml(text) + paperLabel + '</li>';
    });

    var reading = value.reading && typeof value.reading === 'object'
      ? Object.keys(value.reading).map(function (id) {
        return [id, value.reading[id]];
      })
      : [];
    fillMemoryList('memory-reading', reading, function (entry) {
      var id = entry[0];
      var status = READ_LABELS[entry[1]] ? entry[1] : 'todo';
      return '<li>' +
        '<span class="paper-name">' + escapeHtml(getPaperTitle(id)) + '</span>' +
        '<span class="read-state" data-state="' + status + '">' + READ_LABELS[status] + '</span>' +
        '</li>';
    });
  }

  function renderRecords(records) {
    var value = records || {};
    setText('record-db-queries', value.db_queries == null ? '—' : value.db_queries);
    setText('record-evidence-packs', value.evidence_packs == null ? '—' : value.evidence_packs);
    setText('record-qoder-sessions', value.qoder_sessions == null ? '—' : value.qoder_sessions);
  }

  function renderStats(corpus, cases) {
    setText('stat-papers', corpus.papers == null ? asArray(corpus.paper_list).length : corpus.papers);
    setText('stat-chunks', corpus.chunks == null ? '—' : corpus.chunks);
    setText('stat-cases', cases.length);
    setText('stat-api', '0');
  }

  function renderGeneratedAt(value) {
    var target = byId('generated-at');
    if (!target) {
      return;
    }

    var localTime = formatLocalTime(value);
    target.textContent = localTime || '—';
    if (localTime && value) {
      target.setAttribute('datetime', String(value));
    } else {
      target.removeAttribute('datetime');
    }
  }

  function renderSite(data) {
    var value = data || {};
    var corpus = value.corpus || {};
    var cases = asArray(value.cases);

    renderCorpus(corpus);
    renderTabs(cases);
    renderPanels(cases);
    renderMemory(value.memory || {});
    renderRecords(value.records || {});
    renderStats(corpus, cases);
    renderGeneratedAt(value.generated_at);

    var status = byId('demo-status');
    if (status) {
      status.hidden = true;
      status.textContent = '';
    }
  }

  /* ===== 复制快速开始命令 ===== */

  function clearCopyTimer() {
    if (copyResetTimer) {
      window.clearTimeout(copyResetTimer);
      copyResetTimer = null;
    }
  }

  function fallbackCopy(text) {
    var textarea = document.createElement('textarea');
    textarea.value = text;
    textarea.setAttribute('readonly', '');
    textarea.style.position = 'fixed';
    textarea.style.top = '0';
    textarea.style.left = '-9999px';
    document.body.appendChild(textarea);
    textarea.focus();
    textarea.select();
    textarea.setSelectionRange(0, textarea.value.length);

    var copied = false;
    try {
      copied = document.execCommand('copy');
    } catch (error) {
      copied = false;
    }

    document.body.removeChild(textarea);
    return copied;
  }

  function showCopySuccess() {
    var button = byId('copy-btn');
    var status = byId('copy-status');
    if (button) {
      button.textContent = '已复制';
    }
    if (status) {
      status.textContent = '已复制快速开始命令';
    }

    clearCopyTimer();
    copyResetTimer = window.setTimeout(function () {
      var currentButton = byId('copy-btn');
      var currentStatus = byId('copy-status');
      if (currentButton) {
        currentButton.textContent = '复制';
      }
      if (currentStatus) {
        currentStatus.textContent = '';
      }
      copyResetTimer = null;
    }, 1500);
  }

  function showCopyFailure() {
    var button = byId('copy-btn');
    var status = byId('copy-status');
    clearCopyTimer();
    if (button) {
      button.textContent = '请手动复制';
    }
    if (status) {
      status.textContent = '复制失败，请手动复制';
    }
  }

  async function copyQuickstart() {
    var code = byId('quickstart-code');
    var button = byId('copy-btn');
    if (!code || !button) {
      return;
    }

    var text = code.textContent || '';
    var copied = false;

    if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
      try {
        await navigator.clipboard.writeText(text);
        copied = true;
      } catch (error) {
        copied = false;
      }
    }

    if (!copied) {
      copied = fallbackCopy(text);
    }

    if (copied) {
      showCopySuccess();
    } else {
      showCopyFailure();
    }
  }

  /* ===== 事件委托与启动 ===== */

  function handleDocumentClick(event) {
    var target = event.target && event.target.closest
      ? event.target.closest('.tab, .toggle-btn, #copy-btn')
      : null;
    if (!target) {
      return;
    }

    if (target.classList.contains('tab')) {
      activateTab(target, false);
    } else if (target.classList.contains('toggle-btn')) {
      toggleEvidence(target);
    } else if (target.id === 'copy-btn') {
      void copyQuickstart();
    }
  }

  function showLoadError() {
    var status = byId('demo-status');
    if (status) {
      status.hidden = false;
      status.textContent = '无法加载 demo.json，请通过 HTTP 服务打开站点。';
    }
  }

  async function loadSiteData() {
    try {
      var response = await fetch('./data/demo.json');
      if (!response.ok) {
        throw new Error('Request failed');
      }
      var data = await response.json();
      renderSite(data);
    } catch (error) {
      showLoadError();
    }
  }

  function init() {
    document.addEventListener('click', handleDocumentClick);

    var tabs = byId('demo-tabs');
    if (tabs) {
      tabs.addEventListener('keydown', handleTabKeydown);
    }

    void loadSiteData();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
