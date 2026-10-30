/* Knowledge Engine dashboard.
 *
 * Classic script, no build step. It is loaded from <head> (not deferred) so
 * the stored or OS colour scheme is applied before the first paint; Alpine
 * itself is deferred and picks the `dashboard` component up on `alpine:init`.
 *
 * Untrusted content (search snippets, board messages, registry rows) is
 * always rendered as text via `x-text`, never as raw HTML.
 */
(() => {
  'use strict';

  const THEME_KEY = 'ke-theme';
  const BOARD_KEY_KEY = 'ke-board-key';
  const WELCOME_KEY = 'ke-welcome-dismissed';

  // The dashboard is mounted at <prefix>/ui/, so the API root is one level
  // up from the page. This keeps the UI working behind a path prefix.
  const API = new URL('..', location.href);

  // localStorage can throw (private mode, blocked storage); degrade quietly.
  const storage = {
    get(key) {
      try { return localStorage.getItem(key); } catch { return null; }
    },
    set(key, value) {
      try { localStorage.setItem(key, value); } catch { /* ignore */ }
    },
    remove(key) {
      try { localStorage.removeItem(key); } catch { /* ignore */ }
    },
  };

  // ── Theme (applied before first paint) ──────────────────────────
  const darkQuery = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;

  function systemTheme() {
    return darkQuery && darkQuery.matches ? 'dark' : 'light';
  }

  function storedTheme() {
    const t = storage.get(THEME_KEY);
    return t === 'light' || t === 'dark' ? t : null;
  }

  function applyTheme(theme) {
    document.documentElement.dataset.theme = theme;
  }

  applyTheme(storedTheme() || systemTheme());

  // ── HTTP helpers ────────────────────────────────────────────────
  function apiUrl(path, params) {
    const u = new URL(String(path).replace(/^\/+/, ''), API);
    if (params) u.search = params.toString();
    return u;
  }

  function apiFetch(path, options = {}, params) {
    return fetch(apiUrl(path, params), options);
  }

  function boardHeaders(extra) {
    const headers = Object.assign({}, extra || {});
    const key = (storage.get(BOARD_KEY_KEY) || '').trim();
    if (key) headers['X-Board-Key'] = key;
    return headers;
  }

  // Every /board request carries the stored key (when one is set) so admin
  // actions work once the server requires X-Board-Key.
  function boardFetch(path, options = {}, params) {
    return fetch(apiUrl(path, params), Object.assign({}, options, {
      headers: boardHeaders(options.headers),
    }));
  }

  const JSON_HEADERS = { 'Content-Type': 'application/json' };

  // ── Scoped knowledge (V2): constants and pure helpers ───────────
  // Values of the ResearchType contract (knowledge.contracts).
  const RESEARCH_TYPES = [
    'reference', 'paper', 'article', 'transcript', 'code', 'note', 'dataset', 'synthesis', 'other',
  ];

  // Labels for the evidence VerificationState values. They describe how the
  // evidence resolved, never whether a claim is true.
  const VERIFICATION = {
    verified: { label: 'Verified', text: 'This is the exact revision that was requested; its content is shown.' },
    stale: { label: 'Stale', text: 'A newer revision exists. The requested (older) revision is shown, never the latest one.' },
    unavailable: { label: 'Unavailable', text: 'The content is gone, was recorded digest-only, or failed its digest check. Nothing is shown.' },
    ambiguous: { label: 'Ambiguous', text: 'The locator matches more than one position. Nothing is shown.' },
    unsupported: { label: 'Unsupported', text: 'This revision or locator cannot be served (for example older content that was never recorded). Nothing is shown; the latest content is never substituted.' },
  };

  // Module lifecycle states (modules.dispatcher) and how they are shown.
  const MODULE_STATES = {
    ready: { label: 'Ready', icon: '●' },
    starting: { label: 'Starting', icon: '◐' },
    degraded: { label: 'Degraded', icon: '⚠' },
    failed: { label: 'Failed', icon: '✕' },
    stopped: { label: 'Stopped', icon: '■' },
    disabled: { label: 'Disabled', icon: '○' },
    registered: { label: 'Registered', icon: '○' },
  };

  // Split an FTS excerpt into text and <mark>ed parts. The excerpt comes
  // from user corpora, so it is never parsed as HTML: only the literal
  // <mark>/</mark> delimiters the index inserts are recognised, and every
  // part is rendered with x-text.
  function excerptParts(excerpt) {
    const parts = [];
    let marked = false;
    for (const piece of String(excerpt || '').split(/(<mark>|<\/mark>)/)) {
      if (piece === '<mark>') { marked = true; continue; }
      if (piece === '</mark>') { marked = false; continue; }
      if (piece) parts.push({ text: piece, mark: marked });
    }
    return parts;
  }

  function locatorSummary(loc) {
    if (!loc) return 'whole record';
    switch (loc.kind) {
      case 'text': {
        const range = `bytes [${loc.start_byte}, ${loc.end_byte})`;
        return loc.heading ? `${range} · ${loc.heading}` : range;
      }
      case 'pdf': return `page ${loc.page}`;
      case 'transcript': {
        const span = `${loc.start_ms}–${loc.end_ms} ms`;
        return loc.speaker ? `${span} · ${loc.speaker}` : span;
      }
      case 'code': {
        let out = loc.path;
        if (loc.start_line != null) out += `:${loc.start_line}-${loc.end_line}`;
        if (loc.symbol) out += ` · ${loc.symbol}`;
        if (loc.commit) out += ` @ ${String(loc.commit).slice(0, 12)}`;
        else if (loc.snapshot_digest) out += ` @ snapshot ${String(loc.snapshot_digest).slice(0, 19)}`;
        return out;
      }
      case 'unsupported': return `unsupported: ${loc.reason}`;
      default: return String(loc.kind || 'locator');
    }
  }

  // A typed engine error body ({detail, type, error}) as one line.
  async function errorText(response) {
    let body = null;
    try { body = await response.json(); } catch { /* not JSON */ }
    if (body && body.error && body.error.code) return `${body.error.code}: ${body.error.message}`;
    if (body && body.detail) {
      const detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail);
      return body.type ? `${body.type}: ${detail}` : detail;
    }
    return `HTTP ${response.status}`;
  }

  const utf8 = new TextEncoder();
  function byteLength(text) { return utf8.encode(text).length; }

  // ── Alpine component ────────────────────────────────────────────
  document.addEventListener('alpine:init', () => {
    Alpine.data('dashboard', () => ({
      // ── navigation + theme ──
      tabs: ['search', 'registry', 'board', 'knowledge', 'modules', 'config'],
      tab: 'search',
      theme: document.documentElement.dataset.theme || 'light',

      // ── search + registry state ──
      query: '', results: [], searchStatus: '', entries: [], filter: 'all',
      filters: [
        { value: 'all', label: 'All' },
        { value: 'library', label: 'Libraries' },
        { value: 'skill', label: 'Skills' },
        { value: 'tool', label: 'Tools' },
      ],
      counts: { libraries: 0, skills: 0, tools: 0 },
      showWelcome: false,

      // ── board state ──
      board: {
        available: false,
        totalMessages: 0,
        channels: [],
        messageTypes: [],
        channelFilter: '',
        typeFilter: '',
        searchQuery: '',
        messages: [],
        digestData: null,
        postError: '',
        draft: {
          sender_node_id: '',
          channel: 'ops',
          message_type: '',
          visibility_scope: 'all',
          subject: '',
          body: '',
          task_id: '',
          correlation_id: '',
          requires_ack: false,
        },
      },

      // ── scoped knowledge state (POST /knowledge/search, open, citations) ──
      researchTypeOptions: RESEARCH_TYPES,
      kn: {
        loaded: false,
        unavailable: '',
        workspaces: [],
        projects: [],
        sources: [],
        collections: [],
        workspace: '',
        project: '',
        includeWorkspace: false,
        sourceIds: [],
        collectionIds: [],
        researchTypes: [],
        tags: '',
        mode: 'fts',
        limit: 10,
        query: '',
        busy: false,
        error: '',
        status: '',
        envelope: null,
        opened: null,
        openError: '',
        cite: null,
        citeError: '',
        citeStatus: '',
      },

      // ── modules state (GET /modules, board admin key) ──
      mods: { state: 'idle', list: [], jobs: [], error: '', probing: '', status: '' },

      // ── config state ──
      boardKey: '',
      boardKeyStatus: '',
      config: {
        engine_port: 9210,
        standalone_port: 11437,
        sweep_interval_s: 60,
        stale_blocker_hours: 2,
        digest_interval_minutes: 60,
        default_ttl_hours: 168,
        max_messages_before_prune: 5000,
        sweeper_enabled: 1,
        require_key_for_post: 0,
        channelsInput: '',
        saveStatus: '',
      },
      keys: {
        list: [],
        draft: { display_name: '', resource_type: 'board', resource_id: '*', permission: 'write' },
        lastRaw: null,
      },
      providers: {
        list: [],
        defaultEnvVars: {},
        draft: { provider: 'anthropic', env_var: '', display_name: '' },
      },

      init() {
        this.boardKey = storage.get(BOARD_KEY_KEY) || '';
        // Keys + providers are loaded lazily the first time the Config tab opens.
        this.$watch('tab', (t) => {
          if (t === 'config') {
            this.loadKeys();
            this.loadProviders();
          }
          if (t === 'knowledge' && !this.kn.loaded) this.loadKnowledge();
          if (t === 'modules') this.loadModules();
        });
        // Follow the OS while the user has not picked a theme explicitly.
        if (darkQuery && darkQuery.addEventListener) {
          darkQuery.addEventListener('change', () => {
            if (!storedTheme()) this.setTheme(systemTheme(), false);
          });
        }
        this.load();
      },

      // ── Theme ──
      setTheme(theme, persist = true) {
        this.theme = theme;
        applyTheme(theme);
        if (persist) storage.set(THEME_KEY, theme);
      },

      toggleTheme() {
        this.setTheme(this.theme === 'dark' ? 'light' : 'dark');
      },

      // ── Tabs ──
      selectTab(name) {
        if (this.tabs.includes(name)) this.tab = name;
      },

      focusTab(name) {
        this.$nextTick(() => {
          const el = this.$root.querySelector('#tab-' + name);
          if (el) el.focus();
        });
      },

      onTabKeydown(event) {
        const i = this.tabs.indexOf(this.tab);
        const n = this.tabs.length;
        let next;
        switch (event.key) {
          case 'ArrowRight': next = (i + 1) % n; break;
          case 'ArrowLeft': next = (i - 1 + n) % n; break;
          case 'Home': next = 0; break;
          case 'End': next = n - 1; break;
          default: return;
        }
        event.preventDefault();
        this.selectTab(this.tabs[next]);
        this.focusTab(this.tabs[next]);
      },

      // ── Board key (X-Board-Key) ──
      saveBoardKey() {
        const key = (this.boardKey || '').trim();
        if (key) {
          storage.set(BOARD_KEY_KEY, key);
          this.boardKeyStatus = 'key saved in this browser';
        } else {
          storage.remove(BOARD_KEY_KEY);
          this.boardKeyStatus = 'key cleared';
        }
        this.boardKey = key;
        setTimeout(() => { this.boardKeyStatus = ''; }, 2500);
      },

      clearBoardKey() {
        this.boardKey = '';
        this.saveBoardKey();
      },

      // ── Load ──
      async load() {
        this.showWelcome = storage.get(WELCOME_KEY) !== '1';
        try {
          const info = await apiFetch('info').then((r) => r.json()).catch(() => null);
          if (info && info.counts) this.counts = info.counts;
        } catch (e) { /* engine unreachable; counts stay at 0 */ }
        try {
          const r = await apiFetch('registry').then((r) => r.json());
          this.entries = (Array.isArray(r) ? r : (r.entries || []))
            .sort((a, b) => a.name.localeCompare(b.name));
        } catch (e) { /* registry unreachable; table stays empty */ }
        await this.loadBoardMetadata();
      },

      dismissWelcome() {
        this.showWelcome = false;
        storage.set(WELCOME_KEY, '1');
      },

      filteredEntries() {
        return this.filter === 'all' ? this.entries : this.entries.filter((e) => e.kind === this.filter);
      },

      // ── Search / registry ──
      async search() {
        const q = this.query.trim();
        if (!q) return;
        const params = new URLSearchParams({ q, limit: '15' });
        const r = await apiFetch('search', {}, params).then((r) => r.json());
        this.results = r.results || [];
        this.searchStatus = `${this.results.length} result${this.results.length === 1 ? '' : 's'} for ${q}`;
      },

      async toggle(id, enabled) {
        await apiFetch(`registry/${encodeURIComponent(id)}/toggle`, {
          method: 'PATCH',
          headers: JSON_HEADERS,
          body: JSON.stringify({ enabled }),
        });
        const e = this.entries.find((x) => x.id === id);
        if (e) e.enabled = enabled;
      },

      async reindex() {
        await apiFetch('search/reindex', { method: 'POST' });
        alert('Reindex triggered.');
      },

      // ── Board ──
      async loadBoardMetadata() {
        try {
          const st = await boardFetch('board/status').then((r) => r.json());
          this.board.available = true;
          this.board.totalMessages = st.messages_total || 0;
          this.board.channels = (st.config && st.config.channels) || [];
          if (!this.board.draft.channel && this.board.channels.length) {
            this.board.draft.channel = this.board.channels[0];
          }
          const types = await boardFetch('board/message_types').then((r) => r.json());
          this.board.messageTypes = types.message_types || [];
          // Hydrate config
          const cfg = await boardFetch('board/config').then((r) => r.json());
          Object.assign(this.config, {
            engine_port: cfg.engine_port,
            standalone_port: cfg.standalone_port,
            sweep_interval_s: cfg.sweep_interval_s,
            stale_blocker_hours: cfg.stale_blocker_hours,
            digest_interval_minutes: cfg.digest_interval_minutes,
            default_ttl_hours: cfg.default_ttl_hours,
            max_messages_before_prune: cfg.max_messages_before_prune,
            sweeper_enabled: cfg.sweeper_enabled,
            require_key_for_post: cfg.require_key_for_post,
          });
          this.config.channelsInput = (cfg.channels || []).join(', ');
          await this.reloadBoard();
        } catch (e) {
          this.board.available = false;
        }
      },

      async reloadBoard() {
        const params = new URLSearchParams();
        if (this.board.channelFilter) params.set('channel', this.board.channelFilter);
        if (this.board.typeFilter) params.set('message_type', this.board.typeFilter);
        params.set('limit', '50');
        try {
          const msgs = await boardFetch('board/messages', {}, params).then((r) => r.json());
          this.board.messages = Array.isArray(msgs) ? msgs : [];
        } catch (e) {
          this.board.messages = [];
        }
      },

      async searchBoard() {
        const q = this.board.searchQuery.trim();
        if (!q) return this.reloadBoard();
        const params = new URLSearchParams({ q, limit: '50' });
        if (this.board.channelFilter) params.set('channel', this.board.channelFilter);
        const hits = await boardFetch('board/search', {}, params).then((r) => r.json());
        this.board.messages = Array.isArray(hits) ? hits : [];
      },

      async loadDigest() {
        const params = new URLSearchParams();
        if (this.board.channelFilter) params.set('channel', this.board.channelFilter);
        this.board.digestData = await boardFetch('board/digest', {}, params).then((r) => r.json());
      },

      async sweepNow() {
        try {
          const r = await boardFetch('board/sweep', { method: 'POST' });
          if (!r.ok) throw new Error(`HTTP ${r.status}`);
          await this.reloadBoard();
        } catch (e) {
          alert('Sweep failed (may require an admin key — set one in the Config tab).');
        }
      },

      async postMessage() {
        this.board.postError = '';
        try {
          const r = await boardFetch('board/messages', {
            method: 'POST',
            headers: JSON_HEADERS,
            body: JSON.stringify(this.board.draft),
          });
          if (!r.ok) {
            this.board.postError = `HTTP ${r.status}: ${await r.text()}`;
            return;
          }
          this.board.draft.subject = '';
          this.board.draft.body = '';
          await this.reloadBoard();
        } catch (e) {
          this.board.postError = String(e);
        }
      },

      hasAck(m) {
        const a = m.ack_by;
        if (Array.isArray(a)) return a.length > 0;
        if (typeof a === 'string') return a !== '' && a !== '[]';
        return false;
      },

      async ackMessage(m) {
        const acker = prompt('Ack as (sender identity):', this.board.draft.sender_node_id || '');
        if (!acker) return;
        await boardFetch(`board/messages/${encodeURIComponent(m.message_id)}/ack`, {
          method: 'POST',
          headers: JSON_HEADERS,
          body: JSON.stringify({ from: acker }),
        });
        await this.reloadBoard();
      },

      // ── Scoped knowledge ──
      async loadKnowledge() {
        this.kn.loaded = true;
        this.kn.unavailable = '';
        let r;
        try {
          r = await apiFetch('knowledge/workspaces');
        } catch (e) {
          this.kn.unavailable = 'The engine is not reachable.';
          return;
        }
        if (r.status === 404) {
          this.kn.unavailable = 'Scoped knowledge is switched off on this server (KE_KNOWLEDGE_ENABLED=0).';
          return;
        }
        if (!r.ok) {
          this.kn.unavailable = `The knowledge store is not available (${await errorText(r)}).`;
          return;
        }
        const data = await r.json();
        this.kn.workspaces = data.workspaces || [];
        if (!this.kn.workspaces.length) {
          this.kn.unavailable = 'No workspace yet. Run `knowledge-engine knowledge migrate --apply` to register the corpus.';
          return;
        }
        const preferred = this.kn.workspaces.find((w) => w.workspace_id === 'default');
        this.kn.workspace = (preferred || this.kn.workspaces[0]).workspace_id;
        await this.loadWorkspaceScope();
      },

      async loadWorkspaceScope() {
        const ws = encodeURIComponent(this.kn.workspace);
        Object.assign(this.kn, {
          project: '', includeWorkspace: false, sourceIds: [], collectionIds: [],
          projects: [], sources: [], collections: [], envelope: null, opened: null, cite: null,
        });
        const get = async (path, key) => {
          try {
            const r = await apiFetch(path);
            return r.ok ? ((await r.json())[key] || []) : [];
          } catch (e) {
            return [];
          }
        };
        const [projects, sources, collections] = await Promise.all([
          get(`knowledge/workspaces/${ws}/projects`, 'projects'),
          get(`knowledge/workspaces/${ws}/sources`, 'sources'),
          get(`knowledge/workspaces/${ws}/collections`, 'collections'),
        ]);
        this.kn.projects = projects;
        this.kn.sources = sources;
        this.kn.collections = collections;
      },

      projectName(projectId) {
        if (!projectId) return 'workspace-wide';
        const p = this.kn.projects.find((x) => x.project_id === projectId);
        return p ? p.display_name : projectId;
      },

      collectionName(collectionId) {
        const c = this.kn.collections.find((x) => x.collection_id === collectionId);
        return c ? c.display_name : collectionId;
      },

      sourceName(sourceId) {
        const s = this.kn.sources.find((x) => x.source_id === sourceId);
        return s ? `${s.display_name} (${s.source_id})` : sourceId;
      },

      // The query context sent to the broker. A filter with nothing ticked is
      // omitted, so the scope profile fills it (ADR 0006); tags are ANDed.
      knowledgeContext(forSearch) {
        const ctx = { workspace_id: this.kn.workspace };
        if (this.kn.project) {
          ctx.project_id = this.kn.project;
          if (this.kn.includeWorkspace) ctx.include_workspace = true;
        }
        if (!forSearch) return ctx;
        if (this.kn.sourceIds.length) ctx.source_ids = [...this.kn.sourceIds];
        if (this.kn.collectionIds.length) ctx.collection_ids = [...this.kn.collectionIds];
        if (this.kn.researchTypes.length) ctx.research_types = [...this.kn.researchTypes];
        const tags = this.kn.tags.split(',').map((t) => t.trim()).filter(Boolean);
        if (tags.length) ctx.tags = tags;
        ctx.mode = this.kn.mode;
        ctx.limit = Math.min(100, Math.max(1, Number(this.kn.limit) || 10));
        return ctx;
      },

      async knowledgeSearch() {
        const query = this.kn.query.trim();
        if (!query || !this.kn.workspace) return;
        Object.assign(this.kn, { busy: true, error: '', opened: null, cite: null, openError: '' });
        try {
          const r = await apiFetch('knowledge/search', {
            method: 'POST',
            headers: JSON_HEADERS,
            body: JSON.stringify({ query, context: this.knowledgeContext(true) }),
          });
          if (!r.ok) {
            this.kn.envelope = null;
            this.kn.error = await errorText(r);
            this.kn.status = `Search failed: ${this.kn.error}`;
            return;
          }
          const env = await r.json();
          this.kn.envelope = env;
          const n = env.hits.length;
          this.kn.status = `${n} hit${n === 1 ? '' : 's'} for ${query}${env.partial ? ' (partial: some sources failed)' : ''}`;
        } catch (e) {
          this.kn.error = String(e);
          this.kn.status = `Search failed: ${this.kn.error}`;
        } finally {
          this.kn.busy = false;
        }
      },

      excerptParts(excerpt) { return excerptParts(excerpt); },
      locatorSummary(loc) { return locatorSummary(loc); },
      verificationLabel(state) { return (VERIFICATION[state] || { label: state }).label; },
      verificationText(state) { return (VERIFICATION[state] || { text: '' }).text; },

      // GET-equivalent POST /knowledge/open at the hit's exact revision.
      async openHit(hit) {
        Object.assign(this.kn, { openError: '', cite: null, citeError: '' });
        const r = await apiFetch('knowledge/open', {
          method: 'POST',
          headers: JSON_HEADERS,
          body: JSON.stringify({
            context: this.knowledgeContext(false),
            source_id: hit.source_id,
            record_id: hit.record_id,
            revision_id: hit.revision_id || undefined,
          }),
        }).catch((e) => ({ ok: false, status: 0, json: async () => ({ detail: String(e) }) }));
        if (!r.ok) {
          this.kn.opened = null;
          this.kn.openError = await errorText(r);
          return;
        }
        this.kn.opened = { title: hit.title || hit.record_id, via: 'search', result: await r.json() };
        this.focusOpened();
      },

      // POST /knowledge/evidence/open for a cited evidence id.
      async openEvidence(check) {
        Object.assign(this.kn, { openError: '' });
        const body = { context: this.knowledgeContext(false) };
        if (check.evidence_id) {
          body.evidence_id = check.evidence_id;
        } else {
          Object.assign(body, {
            source_id: check.source_id, record_id: check.record_id, revision_id: check.revision_id,
          });
        }
        const r = await apiFetch('knowledge/evidence/open', {
          method: 'POST', headers: JSON_HEADERS, body: JSON.stringify(body),
        }).catch((e) => ({ ok: false, status: 0, json: async () => ({ detail: String(e) }) }));
        if (!r.ok) {
          this.kn.openError = await errorText(r);
          return;
        }
        const data = await r.json();
        this.kn.opened = {
          title: check.evidence_id || check.record_id,
          via: 'citation',
          result: data.result,
          reason: data.reason,
          currentRevision: data.current_revision_id,
          evidenceId: data.evidence_id,
          notice: data.notice,
        };
        this.focusOpened();
      },

      focusOpened() {
        this.$nextTick(() => {
          const el = this.$root.querySelector('#kn-opened-title');
          if (el) el.focus();
        });
      },

      canCite() {
        const o = this.kn.opened;
        if (!o || !o.result) return false;
        const res = o.result;
        return typeof res.content === 'string'
          && (res.verification === 'verified' || res.verification === 'stale')
          && String(res.media_type || '').startsWith('text/');
      },

      // The selected text inside the opened content, as UTF-8 byte offsets.
      selectedSpan() {
        const pre = this.$root.querySelector('#kn-content');
        const sel = window.getSelection ? window.getSelection() : null;
        if (!pre || !sel || sel.rangeCount === 0 || sel.isCollapsed) return null;
        const range = sel.getRangeAt(0);
        if (!pre.contains(range.startContainer) || !pre.contains(range.endContainer)) return null;
        const before = document.createRange();
        before.selectNodeContents(pre);
        before.setEnd(range.startContainer, range.startOffset);
        const content = this.kn.opened.result.content;
        const startChar = before.toString().length;
        const quote = range.toString();
        if (!quote || content.slice(startChar, startChar + quote.length) !== quote) return null;
        const startByte = byteLength(content.slice(0, startChar));
        return { quote, start_byte: startByte, end_byte: startByte + byteLength(quote) };
      },

      async citeOpened(useSelection) {
        if (!this.canCite()) return;
        const res = this.kn.opened.result;
        const evidence = {
          source_id: res.source_id, record_id: res.record_id, revision_id: res.revision_id,
        };
        let claim;
        if (useSelection) {
          const span = this.selectedSpan();
          if (!span) {
            this.kn.citeError = 'Select some text in the content above first.';
            return;
          }
          claim = {
            claim_id: 'q1', kind: 'quotation', quote: span.quote,
            evidence: [{ ...evidence, locator: { kind: 'text', start_byte: span.start_byte, end_byte: span.end_byte } }],
          };
        } else {
          claim = {
            claim_id: 'd1', kind: 'paraphrase', text: this.kn.opened.title,
            evidence: [{ ...evidence, locator: { kind: 'text', start_byte: 0, end_byte: byteLength(res.content) } }],
          };
        }
        Object.assign(this.kn, { citeError: '', cite: null, citeStatus: '' });
        const r = await apiFetch('knowledge/citations/export', {
          method: 'POST',
          headers: JSON_HEADERS,
          body: JSON.stringify({ context: this.knowledgeContext(false), format: 'both', claims: [claim] }),
        }).catch((e) => ({ ok: false, status: 0, json: async () => ({ detail: String(e) }) }));
        if (!r.ok) {
          this.kn.citeError = await errorText(r);
          return;
        }
        this.kn.cite = await r.json();
        this.kn.citeStatus = `Citation ${this.kn.cite.claims.map((c) => c.status).join(', ')}`;
      },

      cslText() {
        return this.kn.cite && this.kn.cite.csl_json ? JSON.stringify(this.kn.cite.csl_json, null, 2) : '';
      },

      async copyText(text) {
        try {
          await navigator.clipboard.writeText(text);
          this.kn.citeStatus = 'Copied to the clipboard.';
        } catch (e) {
          this.kn.citeStatus = 'Copy failed; select the text and copy it manually.';
        }
      },

      // ── Modules (status only; /modules needs a board admin key) ──
      async loadModules() {
        this.mods.state = 'loading';
        this.mods.error = '';
        let r;
        try {
          r = await boardFetch('modules');
        } catch (e) {
          Object.assign(this.mods, { state: 'error', error: String(e), list: [] });
          return;
        }
        if (r.status === 401) { Object.assign(this.mods, { state: 'needs-key', list: [] }); return; }
        if (r.status === 403) { Object.assign(this.mods, { state: 'denied', list: [] }); return; }
        if (r.status === 404 || r.status === 503) {
          Object.assign(this.mods, { state: 'unavailable', list: [], error: await errorText(r) });
          return;
        }
        if (!r.ok) { Object.assign(this.mods, { state: 'error', error: await errorText(r), list: [] }); return; }
        this.mods.list = ((await r.json()).modules || [])
          .sort((a, b) => a.module_id.localeCompare(b.module_id));
        this.mods.state = 'ok';
        try {
          const j = await boardFetch('modules/jobs');
          this.mods.jobs = j.ok ? ((await j.json()).jobs || []) : [];
        } catch (e) {
          this.mods.jobs = [];
        }
        const degraded = this.mods.list.filter((m) => m.state === 'degraded' || m.state === 'failed').length;
        this.mods.status = `${this.mods.list.length} module${this.mods.list.length === 1 ? '' : 's'}`
          + (degraded ? `, ${degraded} degraded or failed` : '');
      },

      moduleStateLabel(state) { return (MODULE_STATES[state] || { label: state }).label; },
      moduleStateIcon(state) { return (MODULE_STATES[state] || { icon: '?' }).icon; },

      // The bounded health probe (POST /modules/{id}/health); enabled modules only.
      async probeModule(m) {
        this.mods.probing = m.module_id;
        let message;
        try {
          const r = await boardFetch(`modules/${encodeURIComponent(m.module_id)}/health`, { method: 'POST' });
          message = r.ok
            ? `Health check of ${m.module_id} finished.`
            : `Health check of ${m.module_id} failed: ${await errorText(r)}`;
        } catch (e) {
          message = `Health check of ${m.module_id} failed: ${e}`;
        } finally {
          this.mods.probing = '';
        }
        await this.loadModules();
        this.mods.status = message;
      },

      // ── Config / Keys ──
      async saveConfig() {
        const patch = {
          engine_port: Number(this.config.engine_port),
          standalone_port: Number(this.config.standalone_port),
          sweep_interval_s: Number(this.config.sweep_interval_s),
          stale_blocker_hours: Number(this.config.stale_blocker_hours),
          digest_interval_minutes: Number(this.config.digest_interval_minutes),
          default_ttl_hours: Number(this.config.default_ttl_hours),
          max_messages_before_prune: Number(this.config.max_messages_before_prune),
          sweeper_enabled: this.config.sweeper_enabled ? 1 : 0,
          require_key_for_post: this.config.require_key_for_post ? 1 : 0,
          channels: this.config.channelsInput.split(',')
            .map((s) => s.trim()).filter(Boolean),
        };
        const r = await boardFetch('board/config', {
          method: 'PATCH',
          headers: JSON_HEADERS,
          body: JSON.stringify(patch),
        });
        if (r.ok) {
          this.config.saveStatus = 'saved';
          setTimeout(() => { this.config.saveStatus = ''; }, 2500);
          await this.loadBoardMetadata();
        } else {
          this.config.saveStatus = `HTTP ${r.status}`;
        }
      },

      async loadKeys() {
        try {
          const r = await boardFetch('board/keys');
          this.keys.list = r.ok ? await r.json() : [];
        } catch (e) {
          this.keys.list = [];
        }
      },

      async createKey() {
        if (!this.keys.draft.display_name) return;
        const payload = {
          display_name: this.keys.draft.display_name,
          permissions: [{
            resource_type: this.keys.draft.resource_type,
            resource_id: this.keys.draft.resource_id || '*',
            permission: this.keys.draft.permission,
          }],
        };
        const r = await boardFetch('board/keys', {
          method: 'POST',
          headers: JSON_HEADERS,
          body: JSON.stringify(payload),
        });
        if (r.ok) {
          const k = await r.json();
          this.keys.lastRaw = k.raw_key;
          this.keys.draft.display_name = '';
          await this.loadKeys();
        } else {
          alert(`Create failed: HTTP ${r.status} ${await r.text()}`);
        }
      },

      async revokeKey(keyId) {
        if (!confirm(`Revoke key ${keyId}?`)) return;
        await boardFetch(`board/keys/${encodeURIComponent(keyId)}`, { method: 'DELETE' });
        await this.loadKeys();
      },

      // ── Provider credentials (env-var bindings; never store the secret) ──
      async loadProviders() {
        try {
          const r = await boardFetch('board/providers');
          if (r.ok) {
            const data = await r.json();
            this.providers.list = data.providers || [];
            this.providers.defaultEnvVars = data.default_env_vars || {};
          } else {
            this.providers.list = [];
          }
        } catch (e) {
          this.providers.list = [];
        }
      },

      prefillEnvVar() {
        // Suggest the env var the engine already reads for this provider.
        const def = this.providers.defaultEnvVars[this.providers.draft.provider];
        if (def !== undefined) this.providers.draft.env_var = def;
      },

      async registerProvider() {
        const d = this.providers.draft;
        if (!d.provider) return;
        const r = await boardFetch('board/providers', {
          method: 'POST',
          headers: JSON_HEADERS,
          body: JSON.stringify({
            provider: d.provider,
            env_var: d.env_var,
            display_name: d.display_name,
          }),
        });
        if (r.ok) {
          this.providers.draft.display_name = '';
          await this.loadProviders();
        } else {
          alert(`Add failed: HTTP ${r.status} ${await r.text()}`);
        }
      },

      async verifyProvider(keyId) {
        await boardFetch(`board/providers/${encodeURIComponent(keyId)}/verify`, { method: 'POST' });
        await this.loadProviders();
      },

      async deleteProvider(keyId) {
        if (!confirm('Remove this provider key binding?')) return;
        await boardFetch(`board/providers/${encodeURIComponent(keyId)}`, { method: 'DELETE' });
        await this.loadProviders();
      },
    }));
  });
})();
