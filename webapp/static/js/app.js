// TradingAgents Web UI - HTMX Configuration & Keyboard Shortcuts

// HTMX Configuration
document.body.addEventListener('htmx:configRequest', function(evt) {
  // Add common headers if needed
});

document.body.addEventListener('htmx:afterSwap', function(evt) {
  // Re-initialize any components after HTMX swap
});

document.body.addEventListener('htmx:responseError', function(evt) {
  console.error('HTMX Error:', evt.detail);
  alert('Request failed. Please try again.');
});

// Keyboard Shortcuts
document.addEventListener('keydown', function(e) {
  // Ignore if typing in input/textarea
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') {
    return;
  }
  
  // "/" - Focus search
  if (e.key === '/') {
    e.preventDefault();
    const search = document.querySelector('.search-input');
    if (search) search.focus();
  }
  
  // "g" followed by key for navigation
  if (e.key === 'g' && !window._gPressed) {
    window._gPressed = true;
    setTimeout(() => { window._gPressed = false; }, 500);
    return;
  }
  
  if (window._gPressed) {
    window._gPressed = false;
    const routes = {
      'd': '/dashboard',
      'a': '/analyze',
      'b': '/batch',
      'h': '/history',
      'c': '/compare',
      's': '/snapshots',
      'q': '/qa',
      't': '/settings',
      'r': '/screener',
      'l': '/alerts',
      'k': '/backtest'
    };
    if (routes[e.key]) {
      window.location.href = routes[e.key];
    }
  }
  
  // "n" - New analysis (on dashboard)
  if (e.key === 'n' && !e.metaKey && !e.ctrlKey) {
    const newBtn = document.querySelector('[data-action="new-analysis"]');
    if (newBtn) newBtn.click();
  }
  
  // "e" - Export (when available)
  if (e.key === 'e' && !e.metaKey && !e.ctrlKey) {
    const exportBtn = document.querySelector('[data-action="export"]');
    if (exportBtn) exportBtn.click();
  }
  
  // "Escape" - Close modals
  if (e.key === 'Escape') {
    const modal = document.querySelector('.modal.active');
    if (modal) modal.classList.remove('active');
  }
});

// Helper: Format duration
function formatDuration(seconds) {
  if (!seconds) return '-';
  if (seconds < 60) return seconds.toFixed(1) + 's';
  const mins = Math.floor(seconds / 60);
  const secs = (seconds % 60).toFixed(0);
  return mins + 'm ' + secs + 's';
}

// Helper: Format date
function formatDate(dateStr) {
  if (!dateStr) return '-';
  const d = new Date(dateStr);
  return d.toLocaleDateString();
}

// Helper: Get decision badge class
function getDecisionClass(decision) {
  if (!decision) return 'badge';
  const upper = decision.toUpperCase();
  if (upper.includes('BUY')) return 'badge badge-buy';
  if (upper.includes('SELL')) return 'badge badge-sell';
  if (upper.includes('HOLD')) return 'badge badge-hold';
  return 'badge';
}

function parsePositionAction(analysis) {
  if (!analysis) return '';
  if (analysis.position_action) return String(analysis.position_action).toUpperCase();
  if (!analysis.decision_json) return '';
  try {
    const dj = typeof analysis.decision_json === 'string'
      ? JSON.parse(analysis.decision_json)
      : analysis.decision_json;
    return (dj && dj.position_action) ? String(dj.position_action).toUpperCase() : '';
  } catch {
    return '';
  }
}

function formatIndexRegime(ss) {
  if (!ss) return { label: '', color: 'var(--text-muted)', border: '' };
  const label = ss.index_regime_label
    || (ss.market_regime ? String(ss.market_regime).toUpperCase() : '');
  const trend = String(ss.index_trend || ss.market_regime || '').toLowerCase();
  const stressed = String(ss.index_stress || '').toLowerCase() === 'stressed';
  let color = 'var(--text-muted)';
  if (trend === 'bull') color = 'var(--qa-green)';
  else if (trend === 'bear') color = 'var(--qa-red)';
  const border = stressed ? '2px solid var(--qa-amber)' : '';
  return { label, color, border };
}

function formatDecisionLabel(decision, positionAction) {
  const base = (decision || '-').toUpperCase();
  const action = (positionAction || '').toUpperCase();
  if (base === 'SELL') {
    if (action === 'REDUCE') return 'SELL (Reduce)';
    if (action === 'FULL_SELL') return 'SELL (Full Exit)';
    if (action === 'AVOID') return 'SELL (Avoid)';
  }
  if (base === 'BUY') {
    if (action === 'ADD') return 'BUY (Add)';
    if (action === 'FULL_BUY') return 'BUY (Full)';
    if (action === 'AVOID') return 'BUY (Avoid)';
  }
  if (base === 'HOLD') {
    if (action === 'REDUCE') return 'HOLD (Trim)';
    if (action === 'AVOID') return 'HOLD (Avoid)';
    if (action === 'HOLD') return 'HOLD (Maintain)';
  }
  return base;
}

function formatQAWarningGroups(analysis) {
  const groups = analysis && analysis.qa_warning_groups;
  if (groups) {
    const integrity = groups.integrity ? groups.integrity.length : 0;
    const context = groups.context ? groups.context.length : 0;
    if (integrity === 0 && context === 0) {
      return '<span class="badge badge-success">Clean</span>';
    }
    const bits = [];
    if (integrity > 0) {
      bits.push(`<span class="badge badge-error" title="Integrity issues">${integrity} integrity</span>`);
    }
    if (context > 0) {
      bits.push(`<span class="badge badge-warning" title="Contextual tensions">${context} context</span>`);
    }
    return bits.join(' ');
  }
  const count = analysis && analysis.qa_warnings ? analysis.qa_warnings.length : 0;
  return `<span class="${getQAClass(count)}">${count} warnings</span>`;
}

// Helper: Get QA badge class — severity based on integrity warnings only
function getQAClass(warningCount, integrityCount) {
  const integrity = typeof integrityCount === 'number' ? integrityCount : warningCount;
  if (integrity === 0) return 'badge badge-success';
  if (integrity <= 2) return 'badge badge-warning';
  return 'badge badge-error';
}

// =============================================================================
// ACTIVE JOB TRACKING (persists across page navigation)
// =============================================================================
const ActiveJobs = {
  STORAGE_KEY: 'tradingagents_active_jobs',

  // Get all tracked jobs from localStorage
  getAll() {
    try {
      return JSON.parse(localStorage.getItem(this.STORAGE_KEY)) || [];
    } catch {
      return [];
    }
  },

  // Save a job to tracking
  save(jobId, metadata) {
    const jobs = this.getAll().filter(j => j.job_id !== jobId);
    jobs.push({ job_id: jobId, ...metadata, tracked_at: new Date().toISOString() });
    localStorage.setItem(this.STORAGE_KEY, JSON.stringify(jobs));
  },

  // Remove a job from tracking (completed or failed)
  remove(jobId) {
    const jobs = this.getAll().filter(j => j.job_id !== jobId);
    localStorage.setItem(this.STORAGE_KEY, JSON.stringify(jobs));
  },

  // Clear all tracked jobs
  clear() {
    localStorage.removeItem(this.STORAGE_KEY);
  },

  // Verify jobs against server and clean up stale ones
  async sync() {
    const tracked = this.getAll();
    if (tracked.length === 0) return [];

    try {
      const res = await fetch('/api/jobs?active_only=false');
      const data = await res.json();
      const serverJobs = data.jobs || [];

      // Remove tracked jobs that are no longer active on server
      const stillActive = [];
      for (const t of tracked) {
        const serverJob = serverJobs.find(j => j.job_id === t.job_id);
        if (serverJob && (serverJob.status === 'queued' || serverJob.status === 'running')) {
          stillActive.push({ ...t, ...serverJob });
        } else {
          // Job completed or not found, remove from tracking
          this.remove(t.job_id);
        }
      }
      return stillActive;
    } catch (err) {
      console.error('Failed to sync active jobs:', err);
      return tracked; // Return cached on error
    }
  }
};

// Poll job status with localStorage tracking
function pollJobStatus(jobId, callback, interval = 2000) {
  const poll = () => {
    fetch(`/api/jobs/${jobId}`)
      .then(async (res) => {
        const payload = await res.json().catch(() => ({}));
        if (!res.ok) {
          // Stale job ID (common after backend restart): stop polling and
          // remove local tracking so we don't spam /api/jobs forever.
          if (res.status === 404) {
            ActiveJobs.remove(jobId);
            return { job_id: jobId, status: 'failed', error: payload.detail || 'Job not found' };
          }
          throw new Error(payload.detail || `Polling failed (${res.status})`);
        }
        return payload;
      })
      .then(job => {
        if (!job) return;
        callback(job);
        if (job.status === 'queued' || job.status === 'running') {
          setTimeout(poll, interval);
        } else {
          // Job finished - remove from tracking
          ActiveJobs.remove(jobId);
        }
      })
      .catch(err => {
        console.error('Poll error:', err);
        setTimeout(poll, interval * 2);
      });
  };
  poll();
}

// Start tracking a new job (call after submitting analysis)
function trackJob(jobId, metadata) {
  ActiveJobs.save(jobId, metadata);
}

// Theme Management - Fully Manual (no system preference detection)
const THEMES = ['light', 'ivory', 'dark', 'midnight', 'nord', 'dusk', 'ember'];
const DEFAULT_THEME = 'light';

function setTheme(theme) {
  // Validate theme
  if (!THEMES.includes(theme)) {
    theme = DEFAULT_THEME;
  }
  
  // Apply to document
  document.documentElement.setAttribute('data-theme', theme);
  
  // Persist choice
  localStorage.setItem('theme', theme);
  
  // Sync dropdown if it exists
  const select = document.getElementById('theme-select');
  if (select && select.value !== theme) {
    select.value = theme;
  }
}

function getStoredTheme() {
  const stored = localStorage.getItem('theme');
  return THEMES.includes(stored) ? stored : DEFAULT_THEME;
}

// Apply theme IMMEDIATELY to prevent flash of unstyled content
(function() {
  const theme = localStorage.getItem('theme');
  document.documentElement.setAttribute('data-theme', THEMES.includes(theme) ? theme : DEFAULT_THEME);
})();

// Initialize on load
document.addEventListener('DOMContentLoaded', function() {
  // Set active nav item based on current path
  const path = window.location.pathname;
  document.querySelectorAll('.nav-item').forEach(item => {
    if (item.getAttribute('href') === path) {
      item.classList.add('active');
    }
  });
  
  // Ensure theme is applied (may already be set by IIFE above)
  const savedTheme = getStoredTheme();
  setTheme(savedTheme);
  
  // Theme selector dropdown
  const themeSelect = document.getElementById('theme-select');
  if (themeSelect) {
    themeSelect.value = savedTheme;
    themeSelect.addEventListener('change', function() {
      setTheme(this.value);
    });
  }
});
