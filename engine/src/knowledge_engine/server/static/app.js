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

  // ── Alpine component ────────────────────────────────────────────
  document.addEventListener('alpine:init', () => {
    Alpine.data('dashboard', () => ({
      // ── navigation + theme ──
      tabs: ['search', 'registry', 'board', 'config'],
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
