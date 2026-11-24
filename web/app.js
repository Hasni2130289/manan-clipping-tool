/* Manan Clipping Tool — frontend logic.
   Plain JS, no frameworks. Talks to the FastAPI backend at /api/*.
   All server/user text is inserted with textContent (never innerHTML
   with untrusted strings), so titles, URLs and captions cannot inject HTML.
*/

// ---------------------------------------------------------------------------
// API BASE URL — the ONE place the backend URL lives.
// Set this to the backend URL (e.g. "https://manan-clipping-tool.onrender.com")
// when this frontend is hosted elsewhere (for example as a Muse Space link,
// which is a different origin from the API). Leave it as "" to use the same
// origin (frontend served by the backend itself, e.g. standalone on Render).
// Every API call goes through apiUrl() / fetchJson() below, so no other
// backend URL appears anywhere in this file.
// ---------------------------------------------------------------------------
const API_BASE_URL = "";

(function () {
  'use strict';

  // ---------- Constants ----------

  var MAX_CLIP_SECONDS = 60;
  var POLL_INTERVAL_MS = 2000;
  var POLL_STATUSES = ['queued', 'downloading', 'transcribing', 'rendering'];
  var KNOWN_STATUSES = ['queued', 'downloading', 'transcribing', 'ready', 'rendering', 'done', 'error'];
  var RENDERABLE_STATUSES = ['ready', 'done'];
  var MAX_HIGHLIGHTS = 8;

  // Hosts the fetch form accepts; the backend re-validates, this is only
  // for instant feedback before the request is sent.
  var SUPPORTED_HOSTS = [
    'youtube.com', 'www.youtube.com', 'm.youtube.com', 'youtu.be',
    'facebook.com', 'www.facebook.com', 'm.facebook.com',
    'fb.watch', 'www.fb.watch'
  ];

  // Hosts the downloader accepts: the clipper's hosts PLUS TikTok
  // (the Termux Manan Downloader's menu is TikTok / Facebook / YouTube).
  var DOWNLOAD_SUPPORTED_HOSTS = SUPPORTED_HOSTS.concat([
    'tiktok.com', 'www.tiktok.com', 'm.tiktok.com',
    'vt.tiktok.com', 'vm.tiktok.com'
  ]);
  var MAX_DOWNLOAD_ITEMS = 10;

  // ---------- DOM references ----------

  var fetchForm = document.getElementById('fetch-form');
  var urlInput = document.getElementById('video-url');
  var fetchBtn = document.getElementById('fetch-btn');
  var fetchError = document.getElementById('fetch-error');

  var previousJobsList = document.getElementById('previous-jobs');
  var previousJobsEmpty = document.getElementById('previous-jobs-empty');

  var jobPanel = document.getElementById('job-panel');
  var jobTitle = document.getElementById('job-title');
  var jobUrl = document.getElementById('job-url');
  var jobThumbnail = document.getElementById('job-thumbnail');
  var jobStatus = document.getElementById('job-status');
  var jobDuration = document.getElementById('job-duration');
  var progressBar = document.getElementById('progress-bar');
  var progressFill = document.getElementById('progress-fill');
  var progressPercent = document.getElementById('progress-percent');
  var progressMessage = document.getElementById('progress-message');

  var transcriptPanel = document.getElementById('transcript-panel');
  var transcriptList = document.getElementById('transcript-list');
  var transcriptEmpty = document.getElementById('transcript-empty');

  var highlightsPanel = document.getElementById('highlights-panel');
  var highlightsGrid = document.getElementById('highlights-grid');
  var highlightsEmpty = document.getElementById('highlights-empty');

  var editorPanel = document.getElementById('editor-panel');
  var startRange = document.getElementById('start-range');
  var startNumber = document.getElementById('start-number');
  var endRange = document.getElementById('end-range');
  var endNumber = document.getElementById('end-number');
  var clipLengthEl = document.getElementById('clip-length');
  var hookInput = document.getElementById('hook-text');
  var ctaInput = document.getElementById('cta-text');
  var renderBtn = document.getElementById('render-btn');
  var renderStatus = document.getElementById('render-status');
  var renderError = document.getElementById('render-error');

  var clipsPanel = document.getElementById('clips-panel');
  var clipsList = document.getElementById('clips-list');
  var clipsEmpty = document.getElementById('clips-empty');

  // Tool tabs + Video Downloader view.
  var clipperView = document.getElementById('clipper-view');
  var downloaderView = document.getElementById('downloader-view');
  var tabClipper = document.getElementById('tab-clipper');
  var tabDownloader = document.getElementById('tab-downloader');
  var downloadForm = document.getElementById('download-form');
  var downloadUrlInput = document.getElementById('download-url');
  var downloadBtn = document.getElementById('download-btn');
  var downloadLimitInput = document.getElementById('download-limit');
  var downloadPlatform = document.getElementById('download-platform');
  var downloadError = document.getElementById('download-error');
  var downloadStatus = document.getElementById('download-status');
  var downloadResult = document.getElementById('download-result');
  var downloadFilename = document.getElementById('download-filename');
  var downloadMeta = document.getElementById('download-meta');
  var downloadLink = document.getElementById('download-link');

  var socialLinksSection = document.getElementById('social-links');
  var socialBrandLine = document.getElementById('social-brand-line');
  var socialTagline = document.getElementById('social-tagline');
  var socialContact = document.getElementById('social-contact');
  var socialContactLink = document.getElementById('social-contact-link');
  var socialLinksList = document.getElementById('social-links-list');

  // ---------- Auth / login view ----------

  var appView = document.getElementById('app-view');
  var loginView = document.getElementById('login-view');
  var loginForm = document.getElementById('login-form');
  var loginPassword = document.getElementById('login-password');
  var showLoginPassword = document.getElementById('show-login-password');
  var loginBtn = document.getElementById('login-btn');
  var loginError = document.getElementById('login-error');
  var logoutBtn = document.getElementById('logout-btn');
  var serverStatusEl = document.getElementById('server-status');

  // ---------- State ----------

  var currentJobId = null;
  var currentJob = null; // last full job detail fetched
  var videoDuration = 0; // seconds, 0 until known
  var pollTimer = null;
  var inMemoryToken = ''; // fallback if localStorage is unavailable

  // ---------- Helpers ----------

  // mm:ss throughout. Minutes may exceed 59 for long videos (e.g. 65:30).
  function formatTime(seconds) {
    var total = Math.max(0, Math.floor(Number(seconds) || 0));
    var minutes = Math.floor(total / 60);
    var secs = total % 60;
    return minutes + ':' + (secs < 10 ? '0' : '') + secs;
  }

  function formatTimeRange(start, end) {
    return formatTime(start) + ' – ' + formatTime(end);
  }

  // "1.5 MB" / "320 KB" for file sizes.
  function formatBytes(bytes) {
    var n = Number(bytes) || 0;
    if (n < 1024) { return n + ' B'; }
    if (n < 1024 * 1024) { return (n / 1024).toFixed(1) + ' KB'; }
    return (n / (1024 * 1024)).toFixed(1) + ' MB';
  }

  function clampNumber(value, min, max) {
    var n = Number(value);
    if (!isFinite(n)) { return min; }
    return Math.min(max, Math.max(min, n));
  }

  function setText(el, text) {
    el.textContent = (text === null || text === undefined) ? '' : String(text);
  }

  function show(el) { el.classList.remove('hidden'); }
  function hide(el) { el.classList.add('hidden'); }

  // Read the API's {"detail": "..."} message when present, else a fallback.
  function apiErrorMessage(data, fallback) {
    if (data && typeof data.detail === 'string' && data.detail) { return data.detail; }
    if (data && typeof data.message === 'string' && data.message) { return data.message; }
    return fallback;
  }

  // ---------- Auth (token-based, origin-independent) ----------

  // The token lives in localStorage so it survives reloads and works
  // cross-origin (cookies would not, on a Muse Space link). The password
  // itself is never stored anywhere.
  var TOKEN_KEY = 'manan_token';

  function getToken() {
    if (inMemoryToken) { return inMemoryToken; }
    try {
      return window.localStorage.getItem(TOKEN_KEY) || '';
    } catch (err) {
      return '';
    }
  }

  function setToken(token) {
    inMemoryToken = token;
    try {
      window.localStorage.setItem(TOKEN_KEY, token);
    } catch (err) {
      // Storage unavailable (private mode, etc.): the session just won't
      // survive a reload; the in-memory token is still used meanwhile.
    }
  }

  function clearToken() {
    inMemoryToken = '';
    try {
      window.localStorage.removeItem(TOKEN_KEY);
    } catch (err) {
      // Nothing to do; the in-memory token is already cleared.
    }
  }

  function showLoginView() {
    stopPolling();
    hide(appView);
    setText(loginError, '');
    loginPassword.value = '';
    show(loginView);
    loginPassword.focus();
  }

  function showAppView() {
    hide(loginView);
    show(appView);
    show(logoutBtn);
  }

  // A 401 means the token is missing, expired or revoked: drop it and
  // show the login view. Called from fetchJson, so every API call is
  // covered.
  function handleUnauthorized() {
    clearToken();
    showLoginView();
  }

  // ---------- Server waking-up notice ----------

  // Render's free tier sleeps after inactivity, so the first API call can
  // take 30–60 s. The request is never aborted; if it runs longer than
  // 5 s a notice appears until it settles. Several slow requests can
  // overlap, so a counter keeps the notice up until the last one settles.
  var WAKE_NOTICE_DELAY_MS = 5000;
  var slowRequestCount = 0;

  function armWakeNotice() {
    var state = { shown: false, timer: null };
    state.timer = setTimeout(function () {
      state.shown = true;
      slowRequestCount += 1;
      show(serverStatusEl);
    }, WAKE_NOTICE_DELAY_MS);
    return state;
  }

  function disarmWakeNotice(state) {
    if (state.timer !== null) {
      clearTimeout(state.timer);
      state.timer = null;
    }
    if (state.shown) {
      state.shown = false;
      slowRequestCount = Math.max(0, slowRequestCount - 1);
      if (slowRequestCount === 0) { hide(serverStatusEl); }
    }
  }

  // Build an API URL: same-origin when API_BASE_URL is "", otherwise the
  // configured backend origin. Every API call goes through here.
  function apiUrl(path) {
    return API_BASE_URL + path;
  }

  // Fetch JSON, throwing an Error with the API detail (or a network
  // message). Every call sends `Authorization: Bearer <token>` (except
  // the login call itself, passed as { auth: false }), and a 401 anywhere
  // clears the token and shows the login view again.
  async function fetchJson(path, options) {
    var opts = options || {};
    var headers = {};
    Object.keys(opts.headers || {}).forEach(function (key) {
      headers[key] = opts.headers[key];
    });
    if (opts.auth !== false) {
      var token = getToken();
      if (token) { headers['Authorization'] = 'Bearer ' + token; }
    }
    var request = { method: opts.method || 'GET', headers: headers };
    if (opts.body !== undefined) { request.body = opts.body; }

    var wakeState = armWakeNotice();
    var response;
    try {
      response = await fetch(apiUrl(path), request);
    } catch (err) {
      throw new Error('Network error: could not reach the server. Check your connection and try again.');
    } finally {
      disarmWakeNotice(wakeState);
    }
    var data = null;
    try {
      data = await response.json();
    } catch (err) {
      data = null; // e.g. empty body on a 204 / non-JSON error page
    }
    if (response.status === 401) {
      handleUnauthorized();
      var authError = new Error(apiErrorMessage(data, 'Your session has expired. Please log in again.'));
      authError.status = 401;
      throw authError;
    }
    if (!response.ok) {
      var httpError = new Error(apiErrorMessage(data, 'Request failed (HTTP ' + response.status + ').'));
      httpError.status = response.status;
      throw httpError;
    }
    return data;
  }

  // ---------- Login / logout ----------

  showLoginPassword.addEventListener('change', function () {
    loginPassword.type = showLoginPassword.checked ? 'text' : 'password';
    loginPassword.focus();
  });

  loginForm.addEventListener('submit', async function (event) {
    event.preventDefault();
    var password = loginPassword.value;
    if (!password) {
      setText(loginError, 'Enter your password.');
      loginPassword.focus();
      return;
    }
    setText(loginError, '');
    loginBtn.disabled = true;
    setText(loginBtn, 'Logging in…');
    try {
      var data = await fetchJson('/api/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password: password }),
        auth: false
      });
      if (data && typeof data.token === 'string' && data.token) {
        setToken(data.token);
        showAppView();
        loadPreviousJobs();
        loadSocialLinks();
      } else {
        setText(loginError, 'Login succeeded but no token was returned. Please try again.');
      }
    } catch (err) {
      if (err.status === 401) {
        // The API detail is "Wrong password"; fall back to the same
        // wording if the detail is missing.
        setText(loginError, err.message || 'Wrong password — try again.');
      } else {
        setText(loginError, err.message);
      }
      loginPassword.select();
    } finally {
      loginBtn.disabled = false;
      setText(loginBtn, 'Log in');
    }
  });

  logoutBtn.addEventListener('click', async function () {
    logoutBtn.disabled = true;
    try {
      await fetchJson('/api/logout', { method: 'POST' });
    } catch (err) {
      // Best effort: even if the call fails (network, token already
      // invalid), drop the local session anyway.
    } finally {
      clearToken();
      logoutBtn.disabled = false;
      showLoginView();
    }
  });

  // ---------- Polling ----------

  function stopPolling() {
    if (pollTimer !== null) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  function startPolling() {
    stopPolling();
    pollTimer = setInterval(function () {
      if (currentJobId) { refreshCurrentJob(); }
    }, POLL_INTERVAL_MS);
  }

  function shouldPoll(status) {
    return POLL_STATUSES.indexOf(status) !== -1;
  }

  // ---------- Previous jobs ----------

  async function loadPreviousJobs() {
    var jobs;
    try {
      jobs = await fetchJson('/api/jobs');
    } catch (err) {
      // A failed list must not break the page; the fetch form still works.
      previousJobsEmpty.textContent = 'Could not load previous videos: ' + err.message;
      show(previousJobsEmpty);
      return;
    }
    renderPreviousJobs(Array.isArray(jobs) ? jobs : []);
  }

  function renderPreviousJobs(jobs) {
    previousJobsList.textContent = '';
    if (jobs.length === 0) {
      setText(previousJobsEmpty, 'No videos yet. Fetch a video above to get started.');
      show(previousJobsEmpty);
      return;
    }
    hide(previousJobsEmpty);
    jobs.forEach(function (job) {
      var li = document.createElement('li');
      var button = document.createElement('button');
      button.type = 'button';
      button.className = 'job-link';

      var titleSpan = document.createElement('span');
      titleSpan.className = 'job-list-title';
      setText(titleSpan, job.title || job.url || job.id);

      var metaSpan = document.createElement('span');
      metaSpan.className = 'job-list-meta';
      var metaParts = [];
      if (job.duration) { metaParts.push(formatTime(job.duration)); }
      if (job.status) { metaParts.push(job.status); }
      if (job.created_at) { metaParts.push(new Date(job.created_at).toLocaleString()); }
      setText(metaSpan, metaParts.join(' · '));

      button.appendChild(titleSpan);
      button.appendChild(metaSpan);
      button.addEventListener('click', function () { openJob(job.id); });
      li.appendChild(button);
      previousJobsList.appendChild(li);
    });
  }

  // ---------- Social links ----------

  // Minimal brand glyphs (24x24), built from basic SVG shapes so no icon
  // font or CDN is needed. Unknown names fall back to a generic link icon.
  var SVG_NS = 'http://www.w3.org/2000/svg';

  var SOCIAL_ICONS = {
    tiktok: [
      { tag: 'path', attrs: { d: 'M12.525.02c1.31-.02 2.61-.01 3.91-.02.08 1.53.63 3.09 1.75 4.17 1.12 1.11 2.7 1.62 4.24 1.79v4.03c-1.44-.05-2.89-.35-4.23-.97-.57-.26-1.1-.59-1.62-.93-.01 2.92.01 5.84-.02 8.75-.08 1.4-.54 2.79-1.35 3.94-1.31 1.92-3.58 3.17-5.91 3.21-1.43.08-2.86-.31-4.08-1.03-2.02-1.19-3.44-3.37-3.65-5.71-.02-.5-.03-1-.01-1.49.18-1.9 1.12-3.72 2.58-4.96 1.66-1.44 3.98-2.13 6.15-1.72.02 1.48-.04 2.96-.04 4.44-.99-.32-2.15-.23-3.02.37-.63.41-1.11 1.04-1.36 1.75-.21.51-.15 1.07-.14 1.61.24 1.64 1.82 3.02 3.5 2.87 1.12-.01 2.19-.66 2.77-1.61.19-.33.4-.67.41-1.06.1-1.79.06-3.57.07-5.36.01-4.03-.01-8.05.02-12.07z', fill: 'currentColor' } }
    ],
    youtube: [
      { tag: 'rect', attrs: { x: '2.5', y: '5.5', width: '19', height: '13', rx: '4', fill: 'none', stroke: 'currentColor', 'stroke-width': '1.8' } },
      { tag: 'path', attrs: { d: 'M10 9.3v5.4l4.8-2.7z', fill: 'currentColor' } }
    ],
    facebook: [
      { tag: 'path', attrs: { d: 'M14 8.6V6.9c0-.8.5-1.3 1.3-1.3H17V2.6h-2.6c-2.9 0-4.4 1.9-4.4 4.5v1.5H7.5V12H10v9.5h4V12h2.7l.4-3.4H14z', fill: 'currentColor' } }
    ],
    instagram: [
      { tag: 'rect', attrs: { x: '3', y: '3', width: '18', height: '18', rx: '5', fill: 'none', stroke: 'currentColor', 'stroke-width': '1.8' } },
      { tag: 'circle', attrs: { cx: '12', cy: '12', r: '4', fill: 'none', stroke: 'currentColor', 'stroke-width': '1.8' } },
      { tag: 'circle', attrs: { cx: '17.2', cy: '6.8', r: '1.1', fill: 'currentColor' } }
    ],
    whatsapp: [
      { tag: 'path', attrs: { d: 'M17.472 14.382c-.297-.149-1.758-.867-2.03-.967-.273-.099-.471-.148-.67.15-.197.297-.767.966-.94 1.164-.173.199-.347.223-.644.075-.297-.15-1.255-.463-2.39-1.475-.883-.788-1.48-1.761-1.653-2.059-.173-.297-.018-.458.13-.606.134-.133.298-.347.446-.52.149-.174.198-.298.298-.497.099-.198.05-.371-.025-.52-.075-.149-.669-1.612-.916-2.207-.242-.579-.487-.5-.669-.51-.173-.008-.371-.01-.57-.01-.198 0-.52.074-.792.372-.272.297-1.04 1.016-1.04 2.479 0 1.462 1.065 2.875 1.213 3.074.149.198 2.096 3.2 5.077 4.487.709.306 1.262.489 1.694.625.712.227 1.36.195 1.871.118.571-.085 1.758-.719 2.006-1.413.248-.694.248-1.289.173-1.413-.074-.124-.272-.198-.57-.347m-5.421 7.403h-.004a9.87 9.87 0 01-5.031-1.378l-.361-.214-3.741.982.998-3.648-.235-.374a9.86 9.86 0 01-1.51-5.26c.001-5.45 4.436-9.884 9.888-9.884 2.64 0 5.122 1.03 6.988 2.898a9.825 9.825 0 012.893 6.994c-.003 5.45-4.437 9.884-9.885 9.884m8.413-18.297A11.815 11.815 0 0012.05 0C5.495 0 .16 5.335.157 11.892c0 2.096.547 4.142 1.588 5.945L.057 24l6.305-1.654a11.882 11.882 0 005.683 1.448h.005c6.554 0 11.89-5.335 11.893-11.893a11.821 11.821 0 00-3.48-8.413z', fill: 'currentColor' } }
    ],
    website: [
      { tag: 'circle', attrs: { cx: '12', cy: '12', r: '9', fill: 'none', stroke: 'currentColor', 'stroke-width': '1.8' } },
      { tag: 'path', attrs: { d: 'M3 12h18', fill: 'none', stroke: 'currentColor', 'stroke-width': '1.8' } },
      { tag: 'path', attrs: { d: 'M12 3c2.5 2.6 3.9 5.7 3.9 9s-1.4 6.4-3.9 9c-2.5-2.6-3.9-5.7-3.9-9s1.4-6.4 3.9-9z', fill: 'none', stroke: 'currentColor', 'stroke-width': '1.8' } }
    ]
  };

  var GENERIC_LINK_ICON = [
    { tag: 'path', attrs: { d: 'M10 14a5 5 0 0 0 7.07 0l2.83-2.83a5 5 0 0 0-7.07-7.07L11.5 5.43M14 10a5 5 0 0 0-7.07 0L4.1 12.83a5 5 0 0 0 7.07 7.07l1.33-1.33', fill: 'none', stroke: 'currentColor', 'stroke-width': '1.8', 'stroke-linecap': 'round', 'stroke-linejoin': 'round' } }
  ];

  function makeIcon(shapes) {
    var svg = document.createElementNS(SVG_NS, 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('class', 'social-icon');
    svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('focusable', 'false');
    shapes.forEach(function (shape) {
      var node = document.createElementNS(SVG_NS, shape.tag);
      Object.keys(shape.attrs).forEach(function (key) {
        node.setAttribute(key, shape.attrs[key]);
      });
      svg.appendChild(node);
    });
    return svg;
  }

  // Exact match first, then a keyword match so names like
  // "WhatsApp Channel" or "SMM site" still get a fitting icon.
  function iconForName(name) {
    var key = name.toLowerCase();
    if (SOCIAL_ICONS[key]) { return SOCIAL_ICONS[key]; }
    if (key.indexOf('whatsapp') !== -1) { return SOCIAL_ICONS.whatsapp; }
    if (key.indexOf('website') !== -1 || key.indexOf('site') !== -1 ||
        key.indexOf('store') !== -1 || key.indexOf('smm') !== -1) { return SOCIAL_ICONS.website; }
    return GENERIC_LINK_ICON;
  }

  function renderSocialLinks(links) {
    socialLinksList.textContent = '';
    links.forEach(function (entry) {
      if (!entry || typeof entry.name !== 'string' || typeof entry.url !== 'string') { return; }
      var name = entry.name.trim();
      if (!name || !entry.url) { return; }
      var li = document.createElement('li');
      var a = document.createElement('a');
      a.href = entry.url;
      a.target = '_blank';
      a.rel = 'noopener noreferrer';
      a.appendChild(makeIcon(iconForName(name)));
      var label = document.createElement('span');
      setText(label, name);
      a.appendChild(label);
      li.appendChild(a);
      socialLinksList.appendChild(li);
    });
  }

  // Renders GET /api/social-links, which returns an object:
  // {"brand_line", "tagline", "contact": {"phone", "url"}, "links": [...]}.
  // (A bare links array is still accepted, for backwards compatibility.)
  // Everything comes from the API, so the backend config stays the source
  // of truth. A 401 shows the login view inside fetchJson; any other
  // failure hides the section quietly.
  function renderSocialSection(data) {
    var links = Array.isArray(data) ? data : (data && Array.isArray(data.links) ? data.links : []);

    if (data && !Array.isArray(data)) {
      if (typeof data.brand_line === 'string' && data.brand_line.trim()) {
        setText(socialBrandLine, data.brand_line.trim());
        show(socialBrandLine);
      }
      if (typeof data.tagline === 'string' && data.tagline.trim()) {
        setText(socialTagline, data.tagline.trim());
        show(socialTagline);
      }
      if (data.contact && typeof data.contact.phone === 'string' &&
          typeof data.contact.url === 'string' && data.contact.url) {
        socialContactLink.href = data.contact.url;
        socialContactLink.textContent = '';
        socialContactLink.appendChild(makeIcon(SOCIAL_ICONS.whatsapp));
        var label = document.createElement('span');
        setText(label, 'WhatsApp ' + data.contact.phone.trim());
        socialContactLink.appendChild(label);
        show(socialContact);
      }
    }

    renderSocialLinks(links);
    if (socialLinksList.childElementCount > 0) { show(socialLinksSection); }
  }

  async function loadSocialLinks() {
    var data;
    try {
      data = await fetchJson('/api/social-links');
    } catch (err) {
      return;
    }
    renderSocialSection(data);
  }

  // ---------- Job detail ----------

  function openJob(jobId) {
    currentJobId = jobId;
    setText(fetchError, '');
    setText(renderError, '');
    setText(renderStatus, '');
    progressFill.style.width = '0%';
    progressBar.setAttribute('aria-valuenow', '0');
    progressBar.removeAttribute('data-status');
    setText(progressPercent, '0%');
    setText(progressMessage, 'Loading…');
    show(jobPanel);
    refreshCurrentJob();
  }

  async function refreshCurrentJob() {
    if (!currentJobId) { return; }
    var job;
    try {
      job = await fetchJson('/api/jobs/' + encodeURIComponent(currentJobId));
    } catch (err) {
      setText(progressMessage, 'Could not refresh job: ' + err.message);
      return;
    }
    currentJob = job;
    renderJob(job);
    if (shouldPoll(job.status)) {
      startPolling();
    } else {
      stopPolling();
    }
  }

  function renderJob(job) {
    show(jobPanel);

    setText(jobTitle, job.title || 'Untitled video');
    setText(jobUrl, job.url || '');

    if (job.thumbnail) {
      jobThumbnail.src = job.thumbnail;
      jobThumbnail.alt = 'Thumbnail for ' + (job.title || 'video');
      show(jobThumbnail);
    } else {
      hide(jobThumbnail);
      jobThumbnail.removeAttribute('src');
    }

    var status = KNOWN_STATUSES.indexOf(job.status) !== -1 ? job.status : 'queued';
    setText(jobStatus, status);
    jobStatus.className = 'badge badge-' + status;
    progressBar.setAttribute('data-status', status);

    videoDuration = Number(job.duration) || 0;
    setText(jobDuration, videoDuration ? formatTime(videoDuration) : '');

    var progress = clampNumber(job.progress, 0, 100);
    progressFill.style.width = progress + '%';
    progressBar.setAttribute('aria-valuenow', String(Math.round(progress)));
    setText(progressPercent, Math.round(progress) + '%');
    setText(progressMessage, job.message || '');

    // Editor limits follow the video duration; keep values in range.
    startRange.max = String(videoDuration);
    startNumber.max = String(videoDuration);
    endRange.max = String(videoDuration);
    endNumber.max = String(videoDuration);

    // Panels appear once the job is ready/done; the editor is usable then.
    var isReady = RENDERABLE_STATUSES.indexOf(status) !== -1;
    if (isReady) {
      show(transcriptPanel);
      show(highlightsPanel);
      show(editorPanel);
      show(clipsPanel);
      renderTranscript(job.transcript || []);
      renderHighlights(job.highlights || []);
      renderClips(job.clips || []);
      // Default the editor to the first 45 seconds if it has never been set.
      if (Number(endNumber.value) === 0 && videoDuration > 0) {
        setEditorWindow(0, Math.min(MAX_CLIP_SECONDS, videoDuration));
      }
    } else if (status === 'error') {
      show(editorPanel);
      show(clipsPanel);
      renderClips(job.clips || []);
    }
    renderBtn.disabled = !isReady;
    updateClipLength();
  }

  // ---------- Transcript ----------

  function renderTranscript(segments) {
    transcriptList.textContent = '';
    if (!segments || segments.length === 0) {
      hide(transcriptList);
      show(transcriptEmpty);
      return;
    }
    hide(transcriptEmpty);
    show(transcriptList);
    segments.forEach(function (segment) {
      var button = document.createElement('button');
      button.type = 'button';
      button.className = 'segment';

      var timeSpan = document.createElement('span');
      timeSpan.className = 'segment-time';
      setText(timeSpan, formatTime(segment.start));

      var textSpan = document.createElement('span');
      textSpan.className = 'segment-text';
      setText(textSpan, segment.text || '');

      button.appendChild(timeSpan);
      button.appendChild(textSpan);
      button.addEventListener('click', function () {
        var start = Number(segment.start) || 0;
        var segEnd = Number(segment.end);
        var endLimit = isFinite(segEnd) ? segEnd : videoDuration;
        var end = Math.min(start + 45, endLimit, videoDuration || start + 45);
        if (end <= start) { end = Math.min(start + 45, videoDuration || start + 45); }
        setEditorWindow(start, end);
        editorPanel.scrollIntoView({ behavior: 'smooth', block: 'start' });
      });
      transcriptList.appendChild(button);
    });
  }

  // ---------- Highlights ----------

  function renderHighlights(highlights) {
    highlightsGrid.textContent = '';
    var shown = (highlights || []).slice(0, MAX_HIGHLIGHTS);
    if (shown.length === 0) {
      show(highlightsEmpty);
      return;
    }
    hide(highlightsEmpty);
    shown.forEach(function (item) {
      var card = document.createElement('div');
      card.className = 'highlight-card';

      var times = document.createElement('div');
      times.className = 'highlight-times';
      setText(times, formatTimeRange(item.start, item.end));

      var score = document.createElement('div');
      score.className = 'highlight-score';
      var scoreLabel = document.createTextNode('Score ');
      var scoreValue = document.createElement('strong');
      setText(scoreValue, String(Math.round(Number(item.score) || 0)));
      score.appendChild(scoreLabel);
      score.appendChild(scoreValue);

      card.appendChild(times);
      card.appendChild(score);

      if (Array.isArray(item.reasons) && item.reasons.length > 0) {
        var list = document.createElement('ul');
        list.className = 'highlight-reasons';
        item.reasons.forEach(function (reason) {
          var li = document.createElement('li');
          setText(li, reason);
          list.appendChild(li);
        });
        card.appendChild(list);
      }

      var useBtn = document.createElement('button');
      useBtn.type = 'button';
      useBtn.className = 'btn btn-secondary btn-small';
      setText(useBtn, 'Use this clip');
      useBtn.addEventListener('click', function () {
        setEditorWindow(Number(item.start) || 0, Number(item.end) || 0);
        editorPanel.scrollIntoView({ behavior: 'smooth', block: 'start' });
      });
      card.appendChild(useBtn);

      highlightsGrid.appendChild(card);
    });
  }

  // ---------- Clip editor ----------

  function getEditorWindow() {
    var start = clampNumber(startNumber.value, 0, videoDuration || 0);
    var end = clampNumber(endNumber.value, 0, videoDuration || 0);
    return { start: start, end: end };
  }

  function setEditorWindow(start, end) {
    start = clampNumber(start, 0, videoDuration || 0);
    end = clampNumber(end, 0, videoDuration || 0);
    if (end < start) { end = start; }
    startRange.value = String(start);
    startNumber.value = String(start);
    endRange.value = String(end);
    endNumber.value = String(end);
    updateClipLength();
  }

  function updateClipLength() {
    var window = getEditorWindow();
    var length = Math.max(0, window.end - window.start);
    clipLengthEl.textContent = '';
    var label = document.createTextNode('Clip length: ' + formatTime(length));
    clipLengthEl.appendChild(label);
    if (length > MAX_CLIP_SECONDS) {
      clipLengthEl.classList.add('over');
      var note = document.createElement('span');
      note.className = 'clip-length-note';
      setText(note, ' — over 60 s, will be clamped to 60 s on render.');
      clipLengthEl.appendChild(note);
    } else {
      clipLengthEl.classList.remove('over');
    }
  }

  function syncStartFrom(value) {
    var start = clampNumber(value, 0, videoDuration || 0);
    startRange.value = String(start);
    startNumber.value = String(start);
    // Keep end at or after start.
    if (Number(endNumber.value) < start) {
      endRange.value = String(start);
      endNumber.value = String(start);
    }
    updateClipLength();
  }

  function syncEndFrom(value) {
    var end = clampNumber(value, 0, videoDuration || 0);
    endRange.value = String(end);
    endNumber.value = String(end);
    if (Number(startNumber.value) > end) {
      startRange.value = String(end);
      startNumber.value = String(end);
    }
    updateClipLength();
  }

  startRange.addEventListener('input', function () { syncStartFrom(startRange.value); });
  startNumber.addEventListener('input', function () { syncStartFrom(startNumber.value); });
  startNumber.addEventListener('change', function () { syncStartFrom(startNumber.value); });
  endRange.addEventListener('input', function () { syncEndFrom(endRange.value); });
  endNumber.addEventListener('input', function () { syncEndFrom(endNumber.value); });
  endNumber.addEventListener('change', function () { syncEndFrom(endNumber.value); });

  // ---------- Render ----------

  renderBtn.addEventListener('click', async function () {
    if (!currentJobId || !currentJob) { return; }
    setText(renderError, '');
    setText(renderStatus, 'Rendering…');

    var window = getEditorWindow();
    var start = window.start;
    var end = window.end;

    if (end <= start) {
      setText(renderError, 'End must be after start.');
      setText(renderStatus, '');
      return;
    }
    // Clamp on render: never send a window longer than 60 s.
    if (end - start > MAX_CLIP_SECONDS) {
      end = start + MAX_CLIP_SECONDS;
      setEditorWindow(start, end);
    }

    renderBtn.disabled = true;
    try {
      await fetchJson('/api/jobs/' + encodeURIComponent(currentJobId) + '/render', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          start: start,
          end: end,
          hook_text: hookInput.value.trim(),
          cta_text: ctaInput.value.trim()
        })
      });
      // The job goes to "rendering"; poll until it is done, then refresh.
      setText(renderStatus, 'Rendering… the clip will appear below when it is done.');
      startPolling();
      await refreshCurrentJob();
    } catch (err) {
      setText(renderError, err.message);
      setText(renderStatus, '');
      renderBtn.disabled = RENDERABLE_STATUSES.indexOf(currentJob.status) === -1;
    }
  });

  // ---------- Clips list ----------

  function renderClips(clips) {
    clipsList.textContent = '';
    if (!clips || clips.length === 0) {
      show(clipsEmpty);
      return;
    }
    hide(clipsEmpty);
    clips.forEach(function (clip) {
      var item = document.createElement('div');
      item.className = 'clip-item';

      var video = document.createElement('video');
      video.controls = true;
      video.preload = 'metadata';
      video.src = apiUrl('/api/clips/' + encodeURIComponent(clip.id));

      var info = document.createElement('div');
      info.className = 'clip-info';

      var rangeP = document.createElement('p');
      var rangeStrong = document.createElement('strong');
      setText(rangeStrong, formatTimeRange(clip.start, clip.end));
      rangeP.appendChild(rangeStrong);
      if (clip.duration) {
        rangeP.appendChild(document.createTextNode(' · ' + formatTime(clip.duration)));
      }
      info.appendChild(rangeP);

      if (clip.hook_text) {
        var hookP = document.createElement('p');
        hookP.className = 'clip-hook';
        setText(hookP, clip.hook_text);
        info.appendChild(hookP);
      }
      if (clip.cta_text) {
        var ctaP = document.createElement('p');
        setText(ctaP, clip.cta_text);
        info.appendChild(ctaP);
      }
      if (clip.filename) {
        var fileP = document.createElement('p');
        fileP.className = 'muted small';
        setText(fileP, clip.filename);
        info.appendChild(fileP);
      }

      var actions = document.createElement('div');
      actions.className = 'clip-actions';

      var download = document.createElement('a');
      download.className = 'btn btn-small';
      download.href = apiUrl('/api/clips/' + encodeURIComponent(clip.id));
      download.setAttribute('download', clip.filename || 'clip.mp4');
      setText(download, 'Download');

      var deleteBtn = document.createElement('button');
      deleteBtn.type = 'button';
      deleteBtn.className = 'btn btn-danger btn-small';
      setText(deleteBtn, 'Delete');
      deleteBtn.addEventListener('click', async function () {
        if (!window.confirm('Delete this clip? This cannot be undone.')) { return; }
        deleteBtn.disabled = true;
        try {
          await fetchJson('/api/clips/' + encodeURIComponent(clip.id), { method: 'DELETE' });
          await refreshCurrentJob();
        } catch (err) {
          setText(renderError, 'Could not delete clip: ' + err.message);
          deleteBtn.disabled = false;
        }
      });

      actions.appendChild(download);
      actions.appendChild(deleteBtn);
      info.appendChild(actions);

      item.appendChild(video);
      item.appendChild(info);
      clipsList.appendChild(item);
    });
  }

  // ---------- Tool tabs ----------

  function switchTab(which) {
    var clipperActive = which === 'clipper';
    tabClipper.classList.toggle('active', clipperActive);
    tabDownloader.classList.toggle('active', !clipperActive);
    tabClipper.setAttribute('aria-selected', clipperActive ? 'true' : 'false');
    tabDownloader.setAttribute('aria-selected', clipperActive ? 'false' : 'true');
    if (clipperActive) {
      show(clipperView);
      hide(downloaderView);
    } else {
      hide(clipperView);
      show(downloaderView);
    }
  }

  tabClipper.addEventListener('click', function () { switchTab('clipper'); });
  tabDownloader.addEventListener('click', function () { switchTab('downloader'); });

  // ---------- Video Downloader ----------

  // Auto-detected platform label under the URL field ("Detected: TikTok").
  function downloadPlatformForUrl(value) {
    var parsed;
    try {
      parsed = new URL(value);
    } catch (err) {
      return '';
    }
    var host = parsed.hostname.toLowerCase();
    if (host.indexOf('tiktok') !== -1) { return 'TikTok'; }
    if (host.indexOf('facebook') !== -1 || host === 'fb.watch' || host === 'www.fb.watch') { return 'Facebook'; }
    if (host.indexOf('youtube') !== -1 || host === 'youtu.be') { return 'YouTube'; }
    return '';
  }

  downloadUrlInput.addEventListener('input', function () {
    var label = downloadPlatformForUrl(downloadUrlInput.value.trim());
    setText(downloadPlatform, label ? 'Detected: ' + label : '');
  });

  function isSupportedDownloadUrl(value) {
    var parsed;
    try {
      parsed = new URL(value);
    } catch (err) {
      return false;
    }
    if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') { return false; }
    return DOWNLOAD_SUPPORTED_HOSTS.indexOf(parsed.hostname.toLowerCase()) !== -1;
  }

  downloadForm.addEventListener('submit', async function (event) {
    event.preventDefault();
    var url = downloadUrlInput.value.trim();
    if (!url) {
      setText(downloadError, 'Paste a video link first.');
      return;
    }
    if (!isSupportedDownloadUrl(url)) {
      setText(downloadError, 'That doesn’t look like a YouTube, TikTok or Facebook video link. Paste a link from youtube.com, youtu.be, tiktok.com, facebook.com or fb.watch.');
      return;
    }
    var limit = parseInt(downloadLimitInput.value, 10);
    if (!isFinite(limit) || limit < 0) { limit = 0; }
    if (limit > MAX_DOWNLOAD_ITEMS) {
      limit = MAX_DOWNLOAD_ITEMS;
      downloadLimitInput.value = String(MAX_DOWNLOAD_ITEMS);
    }
    setText(downloadError, '');
    setText(downloadStatus, 'Downloading… this can take a while for a long video.');
    hide(downloadResult);
    downloadBtn.disabled = true;
    setText(downloadBtn, 'Downloading…');
    try {
      var data = await fetchJson('/api/download', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url: url, limit: limit })
      });
      setText(downloadStatus, '');
      setText(downloadFilename, data.filename || 'video.mp4');
      var metaParts = [];
      if (data.size) { metaParts.push(formatBytes(data.size)); }
      if (data.platform) { metaParts.push(data.platform); }
      if (data.items && data.items > 1) { metaParts.push(data.items + ' videos (zip)'); }
      if (data.expires_in) { metaParts.push('link expires in ' + Math.round(data.expires_in / 60) + ' min'); }
      setText(downloadMeta, metaParts.join(' · '));
      downloadLink.href = apiUrl(data.download_url);
      downloadLink.setAttribute('download', data.filename || 'video.mp4');
      show(downloadResult);
      // Start the browser download. The link is public (no token needed)
      // and one-time: it is deleted after it is served.
      var anchor = document.createElement('a');
      anchor.href = apiUrl(data.download_url);
      anchor.setAttribute('download', data.filename || 'video.mp4');
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
    } catch (err) {
      setText(downloadError, err.message);
      setText(downloadStatus, '');
    } finally {
      downloadBtn.disabled = false;
      setText(downloadBtn, 'Download');
    }
  });

  // ---------- Fetch form ----------

  function isSupportedVideoUrl(value) {
    var parsed;
    try {
      parsed = new URL(value);
    } catch (err) {
      return false;
    }
    if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') { return false; }
    return SUPPORTED_HOSTS.indexOf(parsed.hostname.toLowerCase()) !== -1;
  }

  fetchForm.addEventListener('submit', async function (event) {
    event.preventDefault();
    var url = urlInput.value.trim();
    if (!url) {
      setText(fetchError, 'Paste a video link first.');
      return;
    }
    if (!isSupportedVideoUrl(url)) {
      setText(fetchError, 'That doesn’t look like a YouTube or Facebook video link. Paste a link from youtube.com, youtu.be, facebook.com or fb.watch.');
      return;
    }
    setText(fetchError, '');
    fetchBtn.disabled = true;
    setText(fetchBtn, 'Fetching…');
    try {
      var job = await fetchJson('/api/jobs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url: url })
      });
      await loadPreviousJobs();
      openJob(job.id);
    } catch (err) {
      // Shows the API's detail, e.g. "unsupported site".
      setText(fetchError, err.message);
    } finally {
      fetchBtn.disabled = false;
      setText(fetchBtn, 'Fetch video');
    }
  });

  // ---------- Init ----------

  // Gate the app on the token: with a stored token, load the app; without
  // one, show the login view and load nothing (every /api/* call except
  // /api/health needs the token).
  if (getToken()) {
    showAppView();
    loadPreviousJobs();
    loadSocialLinks();
  } else {
    showLoginView();
  }
  updateClipLength();
})();
